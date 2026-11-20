"""Telegram PHONE outreach and existing-ID dialogs without live credentials.

CRM contact merging is a Bitrix responsibility, not simulated as acceptance here.
"""
import os
import unittest

if os.environ.get("CODEX_CRM_ISOLATED_TESTS") != "1":
    raise unittest.SkipTest("Use tests/run_isolated.py")

from datetime import datetime, timedelta
import json
from unittest.mock import ANY, AsyncMock, patch

from fastapi import FastAPI
import httpx
from sqlalchemy.exc import SQLAlchemyError

from test_crm_outreach import OutreachCase
from app.config import settings
from app.database import get_db
from app.models import Channel, CrmMessageProvider, CrmOutgoingMessage, MessageDeliveryTask
from app.routers import crm_messages, handler, incoming, status
from app.services import crm_worker, delivery_worker as worker, telegram as tg
from app.services.crm_outreach import CrmRequestError
from app.services.task_types import SEND_CRM_TO_EDNA, UPDATE_CRM_STATUS


class TelegramCrmTests(OutreachCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.telegram_flag = patch.object(settings, "crm_telegram_outreach_enabled", True)
        self.telegram_flag.start()
        self.addCleanup(self.telegram_flag.stop)
        with self.Session() as db:
            channel = db.get(Channel, self.channel_id)
            channel.channel_type, channel.subject_id = "TELEGRAM", 78
            channel.portal.open_line_id = "7"
            channel.portal.token_expires_at = datetime.utcnow() + timedelta(hours=1)
            provider = db.query(CrmMessageProvider).one()
            provider.provider_code = "edna_telegram_test"
            db.commit()
        self.worker_db = patch.object(worker, "SessionLocal", self.Session)
        self.worker_db.start()
        self.addCleanup(self.worker_db.stop)
        self.bitrix = AsyncMock(return_value={"SUCCESS": True})
        self.bitrix_patch = patch.object(tg, "bitrix_call", self.bitrix)
        self.bitrix_patch.start()
        self.addCleanup(self.bitrix_patch.stop)
        self.app = FastAPI()
        for router in (crm_messages.router, status.router, incoming.router, handler.router):
            self.app.include_router(router)
        def database():
            with self.Session() as db:
                yield db
        self.app.dependency_overrides[get_db] = database
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://app.invalid")

    async def asyncTearDown(self):
        await self.client.aclose()

    def data(self, message_id="external-id"):
        data = super().data(message_id)
        data["code"] = "edna_telegram_test"
        return data

    async def process(self):
        return await worker._process_one_task()

    async def send_crm(self, message_id):
        saved = self.snapshot(message_id)
        with patch.object(tg, "send_text_to_phone", AsyncMock(return_value={"outMessageId": saved["edna_out_message_id"]})) as send, \
             patch.object(crm_worker, "send_message_max", AsyncMock()) as maximum:
            self.assertTrue(await self.process())
            maximum.assert_not_awaited()
        return send

    async def callback(self, message_id, value="READ", **extra):
        return await self.client.post("/status/synthetic-webhook", json={
            "requestId": self.snapshot(message_id)["edna_out_message_id"], "status": value,
            "subject": "test-sender", "subjectId": 78, **extra})

    async def test_deal_receipt_dedup_and_dispatch_keep_selected_phone_and_bindings(self):
        for _ in range(2):
            result = await self.client.post("/crm/messages/send", json=self.data())
            self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(self.tasks(SEND_CRM_TO_EDNA)), 1)
        with self.Session() as db:
            saved = db.query(CrmOutgoingMessage).one()
            message_id, request_id = saved.id, saved.edna_out_message_id
            self.assertEqual(json.loads(saved.bindings), [{"OWNER_TYPE_ID": "2", "OWNER_ID": "48"}])
        send = await self.send_crm(message_id)
        send.assert_awaited_once_with("synthetic-key", "test-sender", "+79990000001", "Test CRM text", request_id)
        self.assertEqual(self.snapshot(message_id)["send_state"], "accepted")
        self.assertEqual(self.snapshot(message_id)["desired_bitrix_status"], "queued")
        self.assertEqual(self.tasks(tg.SEND), [])
        self.bitrix.assert_not_awaited()

    async def test_telegram_4096_boundary_and_invalid_phone_have_no_send(self):
        valid = self.data("limit")
        valid["message_body"] = "x" * 4096
        self.assertEqual(self.snapshot(self.receive(valid))["send_state"], "queued")
        for message_id, field, value in (("too-long", "message_body", "x" * 4097),
                                         ("no-phone", "message_to", ""),
                                         ("tg-id", "message_to", "tg:123")):
            data = self.data(message_id)
            data[field] = value
            self.assertEqual(self.snapshot(self.receive(data))["send_state"], "failed")
        self.assertEqual(len(self.tasks(SEND_CRM_TO_EDNA)), 1)

    async def test_stop_control_blocks_new_send_but_acknowledges_existing_receipt(self):
        message_id = self.receive()
        with patch.object(settings, "crm_telegram_outreach_enabled", False), \
             patch.object(tg, "send_text_to_phone", AsyncMock()) as send:
            self.assertEqual(self.receive(), message_id)
            with self.assertRaises(CrmRequestError):
                self.receive(self.data("new"))
            await self.process()
            send.assert_not_awaited()
            self.assertEqual(self.snapshot(message_id)["send_state"], "queued")
            self.assertEqual((await self.callback(message_id)).status_code, 200)
        self.assertEqual(self.snapshot(message_id)["send_state"], "read")

    async def test_crm_read_and_failures_use_crm_status_not_open_line(self):
        for suffix, event, expected in (("read", "READ", "delivered"),
                                         ("failure", "UNDELIVERED", "undelivered")):
            message_id = self.receive(self.data(suffix))
            for _ in range(2):
                result = await self.callback(message_id, event)
                self.assertEqual(result.status_code, 200, result.text)
            self.due(UPDATE_CRM_STATUS)
            with patch.object(crm_worker, "update_message_status", AsyncMock()) as update:
                while await self.process():
                    pass
                update.assert_awaited_once_with(ANY, ANY,
                                               "edna_telegram_test", suffix, expected)
        self.assertEqual(self.tasks(tg.STATUS), [])
        self.assertEqual(self.tasks(tg.NOTICE), [])
        self.bitrix.assert_not_awaited()

    async def test_wrong_subject_status_cannot_mutate_crm_fact(self):
        message_id = self.receive()
        for extra in ({"subject": "other"}, {"subjectId": 999}):
            self.assertEqual((await self.callback(message_id, **extra)).status_code, 422)
            self.assertEqual(self.snapshot(message_id)["send_state"], "queued")

    async def test_late_status_after_disconnect_and_stop_is_saved(self):
        message_id = self.receive()
        with self.Session() as db:
            db.get(Channel, self.channel_id).is_active = False
            db.commit()
        with patch.object(settings, "crm_telegram_outreach_enabled", False):
            result = await self.callback(message_id)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.snapshot(message_id)["send_state"], "read")

    async def test_crm_status_commit_error_requests_retry_without_open_line_task(self):
        message_id = self.receive()
        with patch.object(self.Session.class_, "commit", side_effect=SQLAlchemyError("private")):
            result = await self.callback(message_id)
        self.assertEqual(result.status_code, 503)
        self.assertNotIn("private", result.text)
        self.assertEqual(self.snapshot(message_id)["send_state"], "queued")
        self.assertEqual(self.tasks(tg.STATUS), [])

    async def test_unknown_outcome_not_resent_after_recovery_but_late_read_resolves_it(self):
        message_id = self.receive()
        with patch.object(tg, "send_text_to_phone", AsyncMock(side_effect=tg.TelegramError("edna_outcome_unknown", uncertain=True))) as send:
            await self.process()
            await worker._reset_stale_tasks()
            self.due(SEND_CRM_TO_EDNA)
            self.assertFalse(await self.process())
            send.assert_awaited_once()
        self.assertEqual(self.snapshot(message_id)["send_state"], "uncertain")
        self.assertEqual((await self.callback(message_id)).status_code, 200)
        self.assertEqual(self.snapshot(message_id)["send_state"], "read")

    async def test_definite_connection_failure_retries_with_same_outgoing_id(self):
        message_id = self.receive()
        request_id = self.snapshot(message_id)["edna_out_message_id"]
        with patch.object(tg, "send_text_to_phone", AsyncMock(side_effect=tg.TelegramError("edna_not_connected", retryable=True))):
            await self.process()
        self.assertEqual(self.snapshot(message_id)["send_state"], "queued")
        self.due(SEND_CRM_TO_EDNA)
        send = await self.send_crm(message_id)
        self.assertEqual(send.call_args.args[-1], request_id)

    async def test_pre_http_commit_failure_cannot_send(self):
        self.receive()
        with self.Session() as db:
            task = db.query(MessageDeliveryTask).filter_by(task_type=SEND_CRM_TO_EDNA).one()
            task.status = "processing"
            db.commit()
            task_id = task.id
        with patch.object(self.Session.class_, "commit", side_effect=SQLAlchemyError("private")), \
             patch.object(tg, "send_text_to_phone", AsyncMock()) as send:
            with self.assertRaises(RuntimeError):
                await crm_worker.execute_crm_task(task_id, SEND_CRM_TO_EDNA, self.Session)
            send.assert_not_awaited()

    async def test_db_failure_after_http_recovers_as_uncertain_without_resend(self):
        message_id = self.receive()
        accepted = False
        async def send(*args):
            nonlocal accepted
            accepted = True
            return {"outMessageId": args[-1]}
        original_commit = self.Session.class_.commit
        def commit(session):
            if accepted:
                raise SQLAlchemyError("private")
            original_commit(session)
        with patch.object(tg, "send_text_to_phone", AsyncMock(side_effect=send)) as sent, \
             patch.object(self.Session.class_, "commit", new=commit):
            with self.assertRaises(RuntimeError):
                await self.process()
            sent.assert_awaited_once()
        with self.Session() as db:
            db.query(MessageDeliveryTask).filter_by(task_type=SEND_CRM_TO_EDNA).update(
                {"updated_at": datetime.utcnow() - timedelta(minutes=10)})
            db.commit()
        await worker._reset_stale_tasks()
        self.assertEqual(self.snapshot(message_id)["send_state"], "uncertain")
        with patch.object(tg, "send_text_to_phone", AsyncMock()) as resend:
            self.due(SEND_CRM_TO_EDNA)
            await self.process()
            resend.assert_not_awaited()

    async def test_same_phone_routes_only_through_each_selected_sender(self):
        data = [self.data("telegram-first")]
        with self.Session() as db:
            for kind, sender in (("TELEGRAM", "tg-second"), ("MAX", "max-first")):
                channel = Channel(portal_id=self.portal_id, name=sender, sender=sender,
                                  api_key=sender + "-key", channel_type=kind)
                db.add(channel)
                db.flush()
                code = "edna_" + kind.lower() + "_" + str(channel.id)
                db.add(CrmMessageProvider(portal_id=self.portal_id, channel_id=channel.id, provider_code=code,
                                         desired_state="present", sync_state="ready", applied_revision=1))
                item = self.data(sender)
                item["code"] = code
                data.append(item)
            db.commit()
        messages = [self.receive(item) for item in data]
        async def telegram_send(*args):
            return {"outMessageId": args[-1]}
        async def max_send(**kwargs):
            return {"outMessageId": kwargs["out_message_id"]}
        with patch.object(tg, "send_text_to_phone", AsyncMock(side_effect=telegram_send)) as telegram, \
             patch.object(crm_worker, "send_message_max", AsyncMock(side_effect=max_send)) as maximum:
            for _ in messages:
                await self.process()
            self.assertEqual([(call.args[0], call.args[1], call.args[2]) for call in telegram.call_args_list],
                             [("synthetic-key", "test-sender", "+79990000001"),
                              ("tg-second-key", "tg-second", "+79990000001")])
            self.assertEqual(maximum.await_count, 1)
            self.assertEqual(maximum.call_args.kwargs["sender"], "max-first")
            self.assertEqual(maximum.call_args.kwargs["to_value"], "+79990000001")
        self.assertEqual(len({self.snapshot(mid)["edna_out_message_id"] for mid in messages}), 3)

    async def test_early_read_beats_http_response_and_keeps_one_send(self):
        message_id = self.receive()
        async def early_read(*args):
            result = await self.callback(message_id)
            self.assertEqual(result.status_code, 200)
            return {"outMessageId": args[-1]}
        with patch.object(tg, "send_text_to_phone", AsyncMock(side_effect=early_read)) as send:
            await self.process()
            send.assert_awaited_once()
        self.assertEqual(self.snapshot(message_id)["send_state"], "read")

    async def test_same_request_id_on_other_channel_cannot_update_source(self):
        message_id = self.receive()
        with self.Session() as db:
            db.add(Channel(portal_id=self.portal_id, name="Other Telegram", sender="other",
                           api_key="synthetic", channel_type="TELEGRAM", subject_id=79, webhook_token="other-tg"))
            db.commit()
        result = await self.client.post("/status/other-tg", json={
            "requestId": self.snapshot(message_id)["edna_out_message_id"], "status": "READ", "subjectId": 79})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(self.snapshot(message_id)["send_state"], "queued")

    async def test_existing_id_dialog_survives_phone_outreach_and_new_phone_fact(self):
        # Contact A and B live in Bitrix. Here verify that the app never replaces
        # B's stable Telegram identity with A's phone or attempts CRM merging.
        data = {"id": "in-1", "subject": "test-sender", "subjectId": 78,
                "subscriber": {"id": 999, "identifier": "123",
                               "identifiers": [{"type": "TELEGRAM_ID", "value": "123"}]},
                "userInfo": None, "messageContent": {"type": "TEXT", "text": "Hello"}}
        self.assertEqual((await self.client.post("/incoming/synthetic-webhook", json=data)).status_code, 200)
        await self.process()
        original = self.bitrix.call_args.args[3]["MESSAGES"][0]
        message_id = self.receive()
        await self.send_crm(message_id)
        data["id"] = "in-2"
        data["subscriber"]["identifiers"].append({"type": "PHONE", "value": "79990000001"})
        self.assertEqual((await self.client.post("/incoming/synthetic-webhook", json=data)).status_code, 200)
        await self.process()
        forwarded = self.bitrix.call_args.args[3]["MESSAGES"][0]
        self.assertEqual(forwarded["user"]["id"], original["user"]["id"])
        self.assertEqual(forwarded["chat"]["id"], original["chat"]["id"])
        self.assertEqual(forwarded["user"]["phone"], "+79990000001")
        self.assertNotIn("disable_crm", forwarded["message"])
        self.assertEqual(json.loads(self.snapshot(message_id)["bindings"]), [{"OWNER_TYPE_ID": "2", "OWNER_ID": "48"}])
        event = {"event": "ONIMCONNECTORMESSAGEADD",
                 "auth": {"member_id": "test-portal", "application_token": "synthetic-app-token"},
                 "data": {"CONNECTOR": tg.CONNECTOR, "LINE": "7", "MESSAGES": [{
                     "chat": {"id": original["chat"]["id"]}, "im": {"chat_id": "41", "message_id": "501"},
                     "message": {"text": "Reply"}}]}}
        self.assertEqual((await self.client.post("/handler", json=event)).status_code, 200)
        with patch.object(tg, "send_text", AsyncMock()) as send, \
             patch.object(tg, "send_text_to_phone", AsyncMock()) as phone:
            await self.process()
            send.assert_awaited_once_with("synthetic-key", "test-sender", "123", "Reply",
                                         ANY)
            phone.assert_not_awaited()
        self.assertTrue(all(call.args[2] == "imconnector.send.messages" for call in self.bitrix.call_args_list))


class TelegramPhoneTransportTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, response=None, error=None, *, phone="+79990000001", text="Hello"):
        calls = []
        def remote(request):
            calls.append(request)
            if error:
                raise error
            return response or httpx.Response(200, json={"outMessageId": "saved", "phone": phone})
        client = httpx.AsyncClient(transport=httpx.MockTransport(remote))
        with patch.object(tg.httpx, "AsyncClient", return_value=client):
            try:
                await tg.send_text_to_phone("synthetic-key", "telegram-sender", phone, text, "saved")
            finally:
                await client.aclose()
        return calls

    async def test_phone_contract_has_explicit_type_and_no_invented_telegram_id(self):
        calls = await self.call()
        self.assertEqual(str(calls[0].url), tg.API_URL)
        self.assertEqual(calls[0].headers["X-API-KEY"], "synthetic-key")
        self.assertEqual(json.loads(calls[0].content), {
            "from": "telegram-sender", "to": {"type": "PHONE", "value": "+79990000001"},
            "outMessageId": "saved", "content": {"type": "TEXT", "text": "Hello"}})

    async def test_bad_phone_or_long_text_rejected_before_http(self):
        for args in ({"phone": "123"}, {"phone": "tg:123"}, {"text": "x" * 4097}):
            with self.subTest(args=str(args)[:30]), self.assertRaises(tg.TelegramError) as caught:
                await self.call(**args)
            self.assertFalse(caught.exception.uncertain)
            self.assertFalse(caught.exception.retryable)

    async def test_rejected_and_uncertain_responses_are_distinguished(self):
        for code, payload, retry, uncertain in (
            (400, {}, False, False), (429, {}, True, False), (503, {}, False, True),
            (408, {}, False, True), (200, {"outMessageId": "wrong"}, False, True),
            (200, {"outMessageId": "saved", "phone": "+79990000002"}, False, True)):
            with self.subTest(code=code, payload=payload), self.assertRaises(tg.TelegramError) as caught:
                await self.call(httpx.Response(code, json=payload))
            self.assertEqual(caught.exception.retryable, retry)
            self.assertEqual(caught.exception.uncertain, uncertain)

    async def test_connection_vs_read_timeout(self):
        for error, retry in ((httpx.ConnectTimeout("private"), True), (httpx.ReadTimeout("private"), False)):
            with self.assertRaises(tg.TelegramError) as caught:
                await self.call(error=error)
            self.assertEqual(caught.exception.retryable, retry)
            self.assertEqual(caught.exception.uncertain, not retry)
            self.assertNotIn("private", str(caught.exception))
