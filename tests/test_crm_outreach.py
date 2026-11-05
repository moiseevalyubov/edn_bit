"""Behavioral checks with isolated DBs and explicit in-process HTTP transports."""
import os
import unittest

if os.environ.get("CODEX_CRM_ISOLATED_TESTS") != "1":
    raise unittest.SkipTest("Use tests/run_isolated.py")

from datetime import datetime, timedelta
import json
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from fastapi import FastAPI
import httpx
from sqlalchemy.exc import SQLAlchemyError

from test_crm_foundation import DatabaseCase
from app.config import settings
from app.database import get_db
from app.models import Channel, CrmMessageProvider, CrmMessageStatusEvent, CrmOutgoingMessage, Message, MessageDeliveryTask, Portal
from app.routers import crm_messages, status as status_router
from app.services import crm_bitrix, crm_worker, delivery_worker, maxbot
from app.services.crm_bitrix import CrmBitrixError
from app.services.crm_outreach import CrmRequestError, accept_crm_message, record_edna_status
from app.services.task_types import SEND_CRM_TO_EDNA, UPDATE_CRM_STATUS


class OutreachCase(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.flag = patch.object(settings, "crm_outreach_enabled", True)
        self.flag.start()
        self.addCleanup(self.flag.stop)
        with self.Session() as db:
            portal = db.get(Portal, self.portal_id)
            portal.app_token, portal.crm_outreach_enabled = "synthetic-app-token", True
            channel = db.get(Channel, self.channel_id)
            channel.webhook_token = "synthetic-webhook"
            db.add(CrmMessageProvider(portal_id=portal.id, channel_id=channel.id, provider_code="edna_max_test",
                                      desired_state="present", sync_state="ready", applied_revision=1))
            db.commit()

    def data(self, message_id="external-id"):
        return dict(module_id="crm", type="SMS", code="edna_max_test", message_id=message_id,
                    message_to="+7 (999) 000-00-01", message_body="Test CRM text",
                    bindings=[{"OWNER_TYPE_ID": "2", "OWNER_ID": "48"}],
                    auth={"member_id": "test-portal", "application_token": "synthetic-app-token",
                          "access_token": "DO-NOT-PERSIST-OPERATOR-TOKEN", "user_id": "7"})

    def receive(self, data=None):
        with self.Session() as db:
            message = accept_crm_message(db, data or self.data())
            db.commit()
            return message.id

    def fact(self, message_id, status="READ", **extra):
        with self.Session() as db:
            message = db.get(CrmOutgoingMessage, message_id)
            event = {"requestId": message.edna_out_message_id, "status": status,
                     "statusAt": "2026-11-05T01:00:00Z", **extra}
            handled = record_edna_status(db, db.get(Channel, message.channel_id), event)
            db.commit()
            return handled

    def snapshot(self, message_id):
        with self.Session() as db:
            message = db.get(CrmOutgoingMessage, message_id)
            return {column.name: getattr(message, column.name) for column in message.__table__.columns}

    def tasks(self, kind=None):
        with self.Session() as db:
            query = db.query(MessageDeliveryTask)
            if kind:
                query = query.filter_by(task_type=kind)
            return [(row.id, row.status, row.retry_count, row.last_error) for row in query.order_by(MessageDeliveryTask.id)]

    def due(self, kind):
        with self.Session() as db:
            db.query(MessageDeliveryTask).filter_by(task_type=kind, status="pending").update(
                {"next_attempt_at": datetime.utcnow() - timedelta(seconds=1)})
            db.commit()


class ReceiptTests(OutreachCase):
    def test_receipt_is_durable_and_deduplicates_without_operator_auth(self):
        first = self.receive()
        data = self.data()
        data["auth"]["access_token"] = "different-user-token"
        self.assertEqual(first, self.receive(data))
        self.assertEqual(len(self.tasks()), 2)
        message = self.snapshot(first)
        self.assertEqual(message["recipient_phone"], "+79990000001")
        self.assertEqual(message["actor_user_id"], "7")
        with self.Session() as db:
            payloads = " ".join(row.payload for row in db.query(MessageDeliveryTask))
            self.assertNotIn("token", payloads)
            self.assertNotIn("7999", payloads)
            self.assertNotIn("Test CRM text", payloads)
            self.assertEqual(db.get(Portal, self.portal_id).access_token, "synthetic-access")

    def test_same_text_with_another_id_is_a_new_message(self):
        self.receive()
        self.receive(self.data("second-id"))
        self.assertEqual(len(self.tasks(SEND_CRM_TO_EDNA)), 2)

    def test_wrong_or_missing_trusted_token_and_foreign_provider_are_rejected(self):
        for change in ("wrong_token", "missing_stored_token", "foreign_provider"):
            with self.subTest(change=change), self.Session() as db:
                data = self.data()
                if change == "wrong_token":
                    data["auth"]["application_token"] = "wrong"
                elif change == "missing_stored_token":
                    db.get(Portal, self.portal_id).app_token = None
                else:
                    data["code"] = "foreign-code"
                with self.assertRaises(CrmRequestError) as error:
                    accept_crm_message(db, data)
                self.assertEqual(error.exception.status_code, 403)
                db.rollback()
        self.assertEqual(self.tasks(), [])

    def test_disabled_feature_rejects_new_but_acknowledges_known_duplicate(self):
        message_id = self.receive()
        with patch.object(settings, "crm_outreach_enabled", False):
            self.assertEqual(self.receive(), message_id)
            with self.assertRaises(CrmRequestError):
                self.receive(self.data("new-id"))
        with self.Session() as db:
            db.get(Channel, self.channel_id).is_active = False
            db.commit()
        self.assertEqual(self.receive(), message_id)

    def test_conflicting_duplicate_is_rejected(self):
        self.receive()
        changed = self.data()
        changed["message_body"] = "Different body"
        with self.assertRaises(CrmRequestError) as error:
            self.receive(changed)
        self.assertEqual(error.exception.status_code, 409)

    def test_invalid_phone_and_text_record_failure_without_send(self):
        for field, value in (("message_to", "not-a-phone"), ("message_body", "x" * 4001)):
            with self.subTest(field=field):
                data = self.data(field)
                data[field] = value
                message_id = self.receive(data)
                self.assertEqual(self.snapshot(message_id)["send_state"], "failed")
        self.assertEqual(self.tasks(SEND_CRM_TO_EDNA), [])
        self.assertEqual(len(self.tasks(UPDATE_CRM_STATUS)), 2)

    def test_max_bot_is_never_sent_via_max(self):
        with self.Session() as db:
            db.get(Channel, self.channel_id).channel_type = "MAX_BOT"
            db.commit()
        message_id = self.receive()
        self.assertEqual(self.snapshot(message_id)["last_error"], "unsupported_channel")
        self.assertEqual(self.tasks(SEND_CRM_TO_EDNA), [])

    def test_real_read_is_monotonic_deduplicated_and_maps_to_delivered(self):
        message_id = self.receive()
        self.fact(message_id, "READ")
        self.fact(message_id, "READ")
        self.fact(message_id, "SENT")
        self.fact(message_id, "FAILED")
        message = self.snapshot(message_id)
        self.assertEqual(message["send_state"], "read")
        self.assertEqual(message["desired_bitrix_status"], "delivered")
        self.assertEqual(message["desired_revision"], 2)
        with self.Session() as db:
            self.assertEqual(db.query(CrmMessageStatusEvent).count(), 3)
        self.assertEqual(len(self.tasks(UPDATE_CRM_STATUS)), 2)

    def test_status_scoped_to_channel_and_subject(self):
        message_id = self.receive()
        with self.assertRaises(CrmRequestError):
            self.fact(message_id, subject="foreign-sender")
        self.assertEqual(self.snapshot(message_id)["send_state"], "queued")
        self.fact(message_id, "FUTURE_STATUS")
        self.assertEqual(self.snapshot(message_id)["send_state"], "queued")


class WorkerTests(OutreachCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.worker_session = patch.object(delivery_worker, "SessionLocal", self.Session)
        self.worker_session.start()
        self.addCleanup(self.worker_session.stop)

    async def process(self):
        return await delivery_worker._process_one_task()

    async def test_exact_phone_out_id_and_no_delivered_on_http_acceptance(self):
        message_id = self.receive()
        before = self.snapshot(message_id)
        with patch.object(crm_worker, "send_message_max", AsyncMock(return_value={"outMessageId": before["edna_out_message_id"]})) as send:
            await self.process()
            send.assert_awaited_once_with(api_key="synthetic-key", sender="test-sender", to_value="+79990000001",
                                          to_type="PHONE", text="Test CRM text", out_message_id=before["edna_out_message_id"])
        message = self.snapshot(message_id)
        self.assertEqual(message["send_state"], "accepted")
        self.assertEqual(message["desired_bitrix_status"], "queued")
        self.assertIsNone(message["delivered_at"])

    async def test_read_timeout_is_uncertain_and_never_blindly_retried(self):
        message_id = self.receive()
        with patch.object(crm_worker, "send_message_max", AsyncMock(side_effect=httpx.ReadTimeout("synthetic"))) as send:
            await self.process()
            self.due(SEND_CRM_TO_EDNA)
            self.assertFalse(await self.process())
            send.assert_awaited_once()
        self.assertEqual(self.snapshot(message_id)["send_state"], "uncertain")
        self.assertEqual(self.tasks(SEND_CRM_TO_EDNA)[0][1], "uncertain")
        self.fact(message_id, "DELIVERED")
        self.assertEqual(self.snapshot(message_id)["send_state"], "delivered")

    async def test_connect_failure_retries_without_losing_correlation(self):
        message_id = self.receive()
        before = self.snapshot(message_id)
        with patch.object(crm_worker, "send_message_max", AsyncMock(side_effect=httpx.ConnectError("synthetic"))):
            await self.process()
        self.assertEqual(self.snapshot(message_id)["send_state"], "queued")
        self.assertIsNone(self.snapshot(message_id)["attempt_started_at"])
        self.due(SEND_CRM_TO_EDNA)
        with patch.object(crm_worker, "send_message_max", AsyncMock(return_value={"outMessageId": before["edna_out_message_id"]})) as send:
            await self.process()
            self.assertEqual(send.call_args.kwargs["out_message_id"], before["edna_out_message_id"])

    async def test_mismatched_success_response_remains_uncertain(self):
        message_id = self.receive()
        with patch.object(crm_worker, "send_message_max", AsyncMock(return_value={"outMessageId": "wrong"})):
            await self.process()
        self.assertEqual(self.snapshot(message_id)["send_state"], "uncertain")

    async def test_database_failure_after_http_acceptance_cannot_trigger_resend(self):
        message_id = self.receive()
        response_received = False
        async def send(**kwargs):
            nonlocal response_received
            response_received = True
            return {"outMessageId": kwargs["out_message_id"]}
        original_commit = self.Session.class_.commit
        def commit(session):
            if response_received:
                raise SQLAlchemyError("synthetic-sensitive-sql")
            original_commit(session)
        with patch.object(crm_worker, "send_message_max", side_effect=send) as send_mock, \
             patch.object(self.Session.class_, "commit", new=commit):
            with self.assertRaisesRegex(RuntimeError, "CRM storage operation failed"):
                await self.process()
            send_mock.assert_awaited_once()
        self.assertEqual(self.snapshot(message_id)["send_state"], "sending")
        with self.Session() as db:
            db.query(MessageDeliveryTask).filter_by(task_type=SEND_CRM_TO_EDNA).update(
                {"updated_at": datetime.utcnow() - timedelta(seconds=600)})
            db.commit()
        await delivery_worker._reset_stale_tasks()
        self.assertEqual(self.snapshot(message_id)["send_state"], "uncertain")
        self.assertEqual(self.tasks(SEND_CRM_TO_EDNA)[0][1], "uncertain")

    async def test_attempt_cannot_send_if_pre_http_commit_fails(self):
        message_id = self.receive()
        # Claim is normally committed by the dispatcher before calling executor.
        with self.Session() as db:
            task = db.query(MessageDeliveryTask).filter_by(task_type=SEND_CRM_TO_EDNA).one()
            task.status = "processing"
            db.commit()
            task_id = task.id
        with patch.object(self.Session.class_, "commit", side_effect=SQLAlchemyError("synthetic")), \
             patch.object(crm_worker, "send_message_max", AsyncMock()) as send:
            with self.assertRaises(RuntimeError):
                await crm_worker.execute_crm_task(task_id, SEND_CRM_TO_EDNA, self.Session)
            send.assert_not_awaited()
        self.assertEqual(self.snapshot(message_id)["send_state"], "queued")

    async def test_rejection_is_failed_but_gateway_outcome_remains_unknown(self):
        for code, expected in ((400, "failed"), (429, "uncertain"), (500, "uncertain")):
            with self.subTest(code=code):
                message_id = self.receive(self.data("http-" + str(code)))
                response = httpx.Response(code, request=httpx.Request("POST", "https://edna.invalid"))
                error = httpx.HTTPStatusError("synthetic-sensitive-body", request=response.request, response=response)
                with patch.object(crm_worker, "send_message_max", AsyncMock(side_effect=error)):
                    await self.process()
                message = self.snapshot(message_id)
                self.assertEqual(message["send_state"], expected)
                self.assertNotIn("sensitive", message["last_error"])

    async def test_early_read_is_not_overwritten_by_http_response(self):
        message_id = self.receive()
        async def send(**kwargs):
            self.fact(message_id, "READ")
            return {"outMessageId": kwargs["out_message_id"]}
        with patch.object(crm_worker, "send_message_max", side_effect=send):
            await self.process()
        self.assertEqual(self.snapshot(message_id)["send_state"], "read")

    async def test_channel_disconnect_before_execution_blocks_send(self):
        message_id = self.receive()
        with self.Session() as db:
            db.get(Channel, self.channel_id).is_active = False
            db.commit()
        with patch.object(crm_worker, "send_message_max", AsyncMock()) as send:
            await self.process()
            send.assert_not_awaited()
        self.assertEqual(self.snapshot(message_id)["send_state"], "failed")

    async def test_status_retries_without_resend_and_runs_when_flag_disabled(self):
        message_id = self.receive()
        self.fact(message_id, "READ")
        self.due(UPDATE_CRM_STATUS)
        with patch.object(settings, "crm_outreach_enabled", False), \
             patch.object(crm_worker, "send_message_max", AsyncMock()) as send, \
             patch.object(crm_worker, "update_message_status", AsyncMock(side_effect=CrmBitrixError("ERROR_MESSAGE_NOT_FOUND", True))) as update:
            await self.process()
            self.assertEqual(update.call_args.args[-1], "delivered")
            send.assert_not_awaited()
        self.due(UPDATE_CRM_STATUS)
        with patch.object(settings, "crm_outreach_enabled", False), \
             patch.object(crm_worker, "update_message_status", AsyncMock()) as update:
            await self.process()
            await self.process()
            update.assert_awaited_once()
        self.assertEqual(self.snapshot(message_id)["applied_revision"], 2)
        self.assertEqual(self.tasks(SEND_CRM_TO_EDNA)[0][1], "pending")

    async def test_status_arriving_during_http_is_applied_by_next_job(self):
        message_id = self.receive()
        self.due(UPDATE_CRM_STATUS)
        async def update(*args):
            self.assertEqual(args[-1], "queued")
            self.fact(message_id, "READ")
        with patch.object(settings, "crm_outreach_enabled", False), \
             patch.object(crm_worker, "update_message_status", side_effect=update):
            await self.process()
        state = self.snapshot(message_id)
        self.assertEqual((state["applied_revision"], state["desired_revision"]), (1, 2))
        self.due(UPDATE_CRM_STATUS)
        with patch.object(settings, "crm_outreach_enabled", False), \
             patch.object(crm_worker, "update_message_status", AsyncMock()) as update:
            await self.process()
            self.assertEqual(update.call_args.args[-1], "delivered")


class CallbackTests(OutreachCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.app = FastAPI()
        self.app.include_router(crm_messages.router)
        self.app.include_router(status_router.router)
        def database():
            with self.Session() as db:
                yield db
        self.app.dependency_overrides[get_db] = database
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://test.invalid")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_real_form_shape_accepted_once_without_external_send(self):
        data = self.data()
        form = {key: value for key, value in data.items() if key not in {"auth", "bindings"}}
        form.update({f"auth[{key}]": value for key, value in data["auth"].items()})
        form.update({f"bindings[0][{key}]": value for key, value in data["bindings"][0].items()})
        for _ in range(2):
            response = await self.client.post("/crm/messages/send", content=urlencode(form), headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.tasks(SEND_CRM_TO_EDNA)), 1)

    async def test_invalid_and_oversize_body_are_not_accepted(self):
        for body, status in ((b"{broken", 400), (b"x" * 65537, 413)):
            response = await self.client.post("/crm/messages/send", content=body)
            self.assertEqual(response.status_code, status)
        self.assertEqual(self.tasks(), [])

    async def test_failed_commit_returns_503_and_rolls_back_receipt_and_queue(self):
        with patch.object(self.Session.class_, "commit", side_effect=SQLAlchemyError("synthetic-sensitive-bind")):
            response = await self.client.post("/crm/messages/send", json=self.data())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("sensitive", response.text)
        self.assertEqual(self.tasks(), [])
        with self.Session() as db:
            self.assertEqual(db.query(CrmOutgoingMessage).count(), 0)

    async def test_known_crm_status_after_disconnect_and_unknown_legacy_restriction(self):
        message_id = self.receive()
        with self.Session() as db:
            db.get(Channel, self.channel_id).is_active = False
            db.commit()
        body = {"requestId": self.snapshot(message_id)["edna_out_message_id"], "status": "READ"}
        response = await self.client.post("/status/synthetic-webhook", json=body)
        self.assertEqual(response.status_code, 200)
        body["requestId"] = "legacy-unknown"
        response = await self.client.post("/status/synthetic-webhook", json=body)
        self.assertEqual(response.status_code, 403)

    async def test_status_commit_failure_returns_retryable_response(self):
        message_id = self.receive()
        body = {"requestId": self.snapshot(message_id)["edna_out_message_id"], "status": "READ"}
        with patch.object(self.Session.class_, "commit", side_effect=SQLAlchemyError("synthetic")):
            response = await self.client.post("/status/synthetic-webhook", json=body)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.snapshot(message_id)["send_state"], "queued")

    async def test_legacy_read_still_uses_open_line_delivery_handler(self):
        with self.Session() as db:
            db.add(Message(channel_id=self.channel_id, direction="outgoing", text="legacy", edna_request_id="legacy-id"))
            db.commit()
        with patch.object(status_router, "_mark_read", AsyncMock()) as mark:
            response = await self.client.post("/status/synthetic-webhook", json={"requestId": "legacy-id", "status": "READ"})
            self.assertEqual(response.status_code, 200)
            mark.assert_awaited_once()


class TransportTests(OutreachCase, unittest.IsolatedAsyncioTestCase):
    async def test_max_payload_preserves_legacy_shape_and_adds_opt_in_id(self):
        bodies = []
        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"outMessageId": bodies[-1].get("outMessageId", "edna-generated")})
        original_client = httpx.AsyncClient
        with patch.object(maxbot.httpx, "AsyncClient", side_effect=lambda: original_client(transport=httpx.MockTransport(handler))):
            await maxbot.send_message_max("test-key", "sender", "+79990000001", "PHONE", "test", out_message_id="persisted-id")
            await maxbot.send_message_max("test-key", "sender", "legacy-max-id", "MAX_ID", "legacy")
        self.assertEqual(bodies[0]["outMessageId"], "persisted-id")
        self.assertEqual(bodies[0]["to"]["type"], "PHONE")
        self.assertNotIn("outMessageId", bodies[1])

    async def test_bitrix_http_200_api_error_is_not_success(self):
        original_client = httpx.AsyncClient
        responses = [{"error": "ERROR_MESSAGE_NOT_FOUND", "error_description": "sensitive-do-not-save"}, {"result": True}]
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=responses.pop(0))
        with self.Session() as db, \
             patch.object(crm_bitrix, "get_valid_token", AsyncMock(return_value="synthetic-admin-token")), \
             patch.object(crm_bitrix.httpx, "AsyncClient", side_effect=lambda: original_client(transport=httpx.MockTransport(handler))):
            portal = db.get(Portal, self.portal_id)
            with self.assertRaises(CrmBitrixError) as error:
                await crm_bitrix.update_message_status(portal, db, "test-code", "external-id", "delivered")
            self.assertTrue(error.exception.retryable)
            self.assertNotIn("sensitive", str(error.exception))
            await crm_bitrix.update_message_status(portal, db, "test-code", "external-id", "delivered")
        self.assertEqual(requests[0], {"CODE": "test-code", "MESSAGE_ID": "external-id", "STATUS": "delivered", "auth": "synthetic-admin-token"})
