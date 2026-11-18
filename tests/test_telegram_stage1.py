"""Synthetic Telegram fixtures, not a claim of live edna/CRM acceptance."""
import os
import unittest

if os.environ.get("CODEX_CRM_ISOLATED_TESTS") != "1":
    raise unittest.SkipTest("Use tests/run_isolated.py")

from datetime import datetime, timedelta
import json
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx

from test_crm_foundation import DatabaseCase
from app.config import Settings, settings
from app.database import get_db
from app.models import Channel, Message, MessageDeliveryTask, Portal, bitrix_chat_id_for
from app.routers import api, handler, incoming, status
from app.services import telegram as tg, telegram_delivery as delivery, delivery_worker as worker, maxbot
from app.services.crm_channels import outreach_capability
from app.services.crm_providers import stage_provider


class TelegramCase(DatabaseCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        with self.Session() as db:
            channel = db.get(Channel, self.channel_id)
            channel.channel_type, channel.subject_id, channel.webhook_token = "TELEGRAM", 78, "tg-token"
            portal = db.get(Portal, self.portal_id)
            portal.open_line_id, portal.app_token = "7", "synthetic-app-token"
            portal.token_expires_at = datetime.utcnow() + timedelta(hours=1)
            db.commit()
        self.flag = patch.object(settings, "telegram_enabled", True)
        self.flag.start()
        self.addCleanup(self.flag.stop)
        self.worker_db = patch.object(worker, "SessionLocal", self.Session)
        self.worker_db.start()
        self.addCleanup(self.worker_db.stop)
        self.bitrix = AsyncMock(return_value={"SUCCESS": True})
        self.bitrix_patch = patch.object(tg, "bitrix_call", self.bitrix)
        self.bitrix_patch.start()
        self.addCleanup(self.bitrix_patch.stop)
        self.app = FastAPI()
        for router in (api.router, handler.router, incoming.router, status.router):
            self.app.include_router(router)
        def db_dependency():
            with self.Session() as db:
                yield db
        self.app.dependency_overrides[get_db] = db_dependency
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://app.invalid")

    async def asyncTearDown(self):
        await self.client.aclose()

    def payload(self, message_id="101", address="12345678901234567"):
        return {"id": message_id, "subject": "test-sender", "subjectId": 78,
                "subscriber": {"id": 999, "identifier": address,
                               "identifiers": [{"type": "TELEGRAM_ID", "value": address}]},
                "userInfo": {"firstName": "Test", "lastName": "User"},
                "receivedAt": "2026-11-19T09:00:00Z", "messageContent": {"type": "TEXT", "text": "Hello"}}

    async def receive(self, **kwargs):
        response = await self.client.post("/incoming/tg-token", json=self.payload(**kwargs))
        self.assertEqual(response.status_code, 200, response.text)

    def event(self, message_id="501", text="[b]Operator:[/b] [br]Reply"):
        with self.Session() as db:
            chat = tg.chat_id(db.get(Channel, self.channel_id), "12345678901234567")
        return {"event": "ONIMCONNECTORMESSAGEADD", "auth": {"member_id": "test-portal", "application_token": "synthetic-app-token",
                 "access_token": "DO-NOT-STORE", "client_endpoint": "https://attacker.invalid/rest/"},
                "data": {"CONNECTOR": tg.CONNECTOR, "LINE": "7", "MESSAGES": [{"chat": {"id": chat},
                    "im": {"chat_id": "41", "message_id": message_id}, "message": {"text": text}}]}}

    def rows(self, kind):
        with self.Session() as db:
            return [(t.id, t.status, json.loads(t.payload), t.last_error) for t in db.query(MessageDeliveryTask).filter_by(task_type=kind).order_by(MessageDeliveryTask.id)]

    async def send_queued(self):
        await self.receive()
        self.assertTrue(await worker._process_one_task())
        result = await self.client.post("/handler", json=self.event())
        self.assertEqual(result.status_code, 200, result.text)

    async def test_incoming_is_durable_namespaced_and_keeps_crm_tracker(self):
        await self.receive()
        await self.receive()
        self.assertEqual(len(self.rows(tg.RECEIVE)), 1)
        self.assertEqual(self.bitrix.await_count, 0)
        await worker._process_one_task()
        args = self.bitrix.call_args.args[3]
        self.assertEqual(args["CONNECTOR"], tg.CONNECTOR)
        self.assertEqual(args["LINE"], 7)
        msg = args["MESSAGES"][0]
        self.assertEqual(msg["user"]["id"], "tg:12345678901234567")
        self.assertNotIn("phone", msg["user"])
        self.assertNotIn("disable_crm", msg["message"])
        self.assertNotIn("skip_phone_validate", msg["user"])
        self.assertEqual(msg["message"]["date"], 1795078800)
        self.assertNotEqual(msg["user"]["id"], "999")

    async def test_explicit_phone_only_from_identifiers(self):
        data = self.payload()
        data["subscriber"]["identifiers"].append({"type": "PHONE", "value": "79990000001"})
        result = await self.client.post("/incoming/tg-token", json=data)
        self.assertEqual(result.status_code, 200)
        await worker._process_one_task()
        self.assertEqual(self.bitrix.call_args.args[3]["MESSAGES"][0]["user"]["phone"], "+79990000001")

    async def test_telegram_admin_operations_require_bearer_without_crm_scope(self):
        with patch.object(settings, "crm_outreach_enabled", False):
            for endpoint, body in (("/api/telegram/prepare", {"member_id": "test-portal"}),
                                   ("/api/channels", {"member_id": "test-portal", "sender": "new", "name": "New", "api_key": "synthetic"})):
                result = await self.client.post(endpoint, json=body)
                self.assertEqual(result.status_code, 401, result.text)
        self.bitrix.assert_not_awaited()

    async def test_missing_type_or_edna_id_never_guessed_as_recipient(self):
        for subscriber in ({"id": 123}, {"id": 123, "identifier": "123"},
                           {"id": 123, "identifiers": [{"type": "MAX_ID", "value": "123"}]}):
            data = self.payload()
            data["subscriber"] = subscriber
            self.assertEqual((await self.client.post("/incoming/tg-token", json=data)).status_code, 422)
        self.assertEqual(self.rows(tg.RECEIVE), [])

    async def test_subject_conflict_and_wrong_token_rejected(self):
        for field, value in (("subject", "another-sender"), ("subjectId", 456)):
            data = self.payload()
            data[field] = value
            self.assertEqual((await self.client.post("/incoming/tg-token", json=data)).status_code, 422)
        self.assertEqual((await self.client.post("/incoming/wrong", json=self.payload())).status_code, 403)
        self.assertEqual(self.rows(tg.RECEIVE), [])

    async def test_flag_off_no_new_work_and_no_telegram_crm_provider(self):
        with patch.object(settings, "telegram_enabled", False):
            self.assertFalse(Settings(_env_file=None).telegram_enabled)
            self.assertEqual((await self.client.post("/incoming/tg-token", json=self.payload())).status_code, 422)
        with self.Session() as db:
            channel, portal = db.get(Channel, self.channel_id), db.get(Portal, self.portal_id)
            self.assertIsNone(outreach_capability(channel))
            self.assertIsNone(stage_provider(db, portal, channel))

    async def test_no_line_or_uninstalled_does_not_acknowledge_lost_message(self):
        with self.Session() as db:
            db.get(Portal, self.portal_id).open_line_id = None
            db.commit()
        self.assertEqual((await self.client.post("/incoming/tg-token", json=self.payload())).status_code, 503)
        self.assertEqual(self.rows(tg.RECEIVE), [])

    async def test_unsupported_media_is_visible_without_forwarding_url(self):
        data = self.payload()
        data["messageContent"] = {"type": "IMAGE", "attachment": {"url": "http://127.0.0.1/private"}}
        self.assertEqual((await self.client.post("/incoming/tg-token", json=data)).status_code, 200)
        await worker._process_one_task()
        msg = self.bitrix.call_args.args[3]["MESSAGES"][0]["message"]
        self.assertIn("Telegram", msg["text"])
        self.assertNotIn("files", msg)
        self.assertNotIn("127.0.0.1", str(self.rows(tg.RECEIVE)))

    async def test_reply_deduplicates_and_does_not_store_oauth(self):
        await self.send_queued()
        self.assertEqual((await self.client.post("/handler", json=self.event())).status_code, 200)
        self.assertEqual(len(self.rows(tg.SEND)), 1)
        self.assertNotIn("DO-NOT-STORE", str(self.rows(tg.SEND)))
        self.assertNotIn("attacker.invalid", str(self.rows(tg.SEND)))
        with patch.object(tg, "send_text", AsyncMock(return_value={})) as send:
            await worker._process_one_task()
            self.assertEqual(send.call_args.args[2:4], ("12345678901234567", "Reply"))
            self.assertEqual(send.await_count, 1)
            self.assertFalse(await worker._process_one_task())
        self.assertEqual(self.rows(tg.SEND)[0][1], "sent")

    async def test_reply_id_conflict_cannot_change_text(self):
        await self.send_queued()
        result = await self.client.post("/handler", json=self.event(text="Another message"))
        self.assertEqual(result.status_code, 422)
        self.assertEqual(len(self.rows(tg.SEND)), 1)

    async def test_duplicate_inside_batch_is_one_message_and_failed_batch_rolls_back(self):
        await self.receive()
        event = self.event()
        event["data"]["MESSAGES"] *= 2
        self.assertEqual((await self.client.post("/handler", json=event)).status_code, 200)
        self.assertEqual(len(self.rows(tg.SEND)), 1)
        with self.Session() as db:
            self.assertEqual(db.query(Message).filter_by(direction="outgoing").count(), 1)
        invalid = self.event(message_id="502")
        invalid["data"]["MESSAGES"].append({"chat": {"id": "unknown"}})
        self.assertEqual((await self.client.post("/handler", json=invalid)).status_code, 422)
        self.assertEqual(len(self.rows(tg.SEND)), 1)

    async def test_failed_status_cannot_be_rolled_back_by_late_sent(self):
        await self.send_queued()
        with patch.object(tg, "send_text", AsyncMock()):
            await worker._process_one_task()
        with self.Session() as db:
            request_id = db.query(Message).filter_by(direction="outgoing").one().edna_request_id
        for state in ("FAILED", "SENT"):
            await self.client.post("/status/tg-token", json={"requestId": request_id, "status": state})
            for _ in range(3):
                await worker._process_one_task()
        self.assertEqual(self.rows(tg.SEND)[0][1], "failed")
        self.assertEqual(self.rows(tg.SEND)[0][2]["delivery_status"], "FAILED")

    async def test_new_incoming_after_line_change_uses_new_line_old_status_keeps_old(self):
        await self.send_queued()
        with self.Session() as db:
            db.get(Portal, self.portal_id).open_line_id = "88"
            db.commit()
        await self.receive(message_id="102")
        with patch.object(tg, "send_text", AsyncMock()):
            await worker._process_one_task()
        await worker._process_one_task()
        self.assertEqual(self.bitrix.call_args.args[3]["LINE"], 88)
        with self.Session() as db:
            original = db.query(Message).filter_by(direction="outgoing").one()
            request_id = original.edna_request_id
        await self.client.post("/status/tg-token", json={"requestId": request_id, "status": "READ"})
        await worker._process_one_task()
        self.assertEqual(self.bitrix.call_args.args[3]["LINE"], 7)

    async def test_disabled_work_does_not_starve_legacy_queue(self):
        await self.receive()
        with self.Session() as db:
            db.add(MessageDeliveryTask(channel_id=self.channel_id, task_type="send_to_bitrix", direction="incoming", payload="{}"))
            db.commit()
        with patch.object(settings, "telegram_enabled", False), patch.object(worker, "_execute_incoming", AsyncMock()) as legacy:
            self.assertTrue(await worker._process_one_task())
            legacy.assert_awaited_once()
        self.assertEqual(self.rows(tg.RECEIVE)[0][1], "pending")

    async def test_cross_portal_connector_line_and_app_token_cannot_select_recipient(self):
        await self.receive()
        for change in ("token", "line", "chat", "portal", "missing_stored_token"):
            data = self.event()
            if change == "token": data["auth"]["application_token"] = "wrong"
            if change == "line": data["data"]["LINE"] = "88"
            if change == "chat": data["data"]["MESSAGES"][0]["chat"]["id"] = "other:dialog"
            if change == "portal": data["auth"]["member_id"] = "other-portal"
            if change == "missing_stored_token":
                with self.Session() as db:
                    db.get(Portal, self.portal_id).app_token = None
                    db.commit()
            await self.client.post("/handler", json=data)
            self.assertEqual(self.rows(tg.SEND), [])

    async def test_disconnect_after_enqueue_prevents_network_send(self):
        await self.send_queued()
        with self.Session() as db:
            db.get(Channel, self.channel_id).is_active = False
            db.commit()
        with patch.object(tg, "send_text", AsyncMock()) as send:
            await worker._process_one_task()
            send.assert_not_awaited()
        self.assertEqual(self.rows(tg.SEND)[0][1], "failed")

    async def test_timeout_is_uncertain_and_does_not_blindly_retry(self):
        await self.send_queued()
        with patch.object(tg, "send_text", AsyncMock(side_effect=tg.TelegramError("timeout", uncertain=True))) as send:
            await worker._process_one_task()
            await worker._process_one_task()  # Only the operator notice.
            self.assertFalse(await worker._process_one_task())
            self.assertEqual(send.await_count, 1)
        self.assertEqual(self.rows(tg.SEND)[0][1], "uncertain")
        msg = self.bitrix.call_args.args[3]["MESSAGES"][0]["message"]
        self.assertEqual(msg["disable_crm"], "Y")
        self.assertIn("неизвестен", msg["text"])

    async def test_safe_preconnect_failure_retries_same_operation_id(self):
        await self.send_queued()
        with patch.object(tg, "send_text", AsyncMock(side_effect=tg.TelegramError("not_connected", retryable=True))) as send:
            await worker._process_one_task()
            args = send.call_args.args
        self.assertEqual(self.rows(tg.SEND)[0][1], "pending")
        with self.Session() as db:
            task = db.query(MessageDeliveryTask).filter_by(task_type=tg.SEND).one()
            task.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
        with patch.object(tg, "send_text", AsyncMock()) as send:
            await worker._process_one_task()
            self.assertEqual(send.call_args.args, args)

    async def test_stale_started_send_never_becomes_second_post(self):
        await self.send_queued()
        with self.Session() as db:
            task = db.query(MessageDeliveryTask).filter_by(task_type=tg.SEND).one()
            data = json.loads(task.payload)
            data["attempt_started"] = True
            task.payload, task.status = json.dumps(data), "processing"
            task.updated_at = datetime.utcnow() - timedelta(minutes=10)
            db.commit()
        await worker._reset_stale_tasks()
        self.assertEqual(self.rows(tg.SEND)[0][1], "uncertain")
        self.assertEqual(len(self.rows(tg.NOTICE)), 1)

    async def test_late_read_survives_flag_off_disconnect_and_duplicate_status(self):
        await self.send_queued()
        with self.Session() as db:
            channel = db.get(Channel, self.channel_id)
            channel.is_active = False
            message = db.query(Message).filter_by(direction="outgoing").one()
            request_id = message.edna_request_id
            db.commit()
        with patch.object(settings, "telegram_enabled", False):
            for _ in range(2):
                response = await self.client.post("/status/tg-token", json={"requestId": request_id, "status": "READ"})
                self.assertEqual(response.status_code, 200, response.text)
            # SEND held; status still runs and reconciles an early/late receipt.
            await worker._process_one_task()
        self.assertEqual(len(self.rows(tg.STATUS)), 1)
        self.assertEqual(self.rows(tg.SEND)[0][1], "sent")
        self.assertEqual(self.rows(tg.SEND)[0][2]["delivery_status"], "READ")
        self.assertEqual(self.bitrix.call_args.args[2], "imconnector.send.status.delivery")
        await self.client.post("/status/tg-token", json={"requestId": request_id, "status": "FAILED"})
        await worker._process_one_task()
        self.assertEqual(self.rows(tg.NOTICE), [])

    async def test_bitrix_failure_retries_delivery_without_resending_client(self):
        await self.receive()
        self.bitrix.side_effect = tg.TelegramError("bitrix_offline", retryable=True)
        await worker._process_one_task()
        self.assertEqual(self.rows(tg.RECEIVE)[0][1], "pending")
        self.assertEqual(self.rows(tg.SEND), [])

    async def test_known_max_name_cannot_be_taken_from_telegram(self):
        await self.receive()
        with self.Session() as db:
            channel = Channel(portal_id=self.portal_id, name="MAX", sender="max", api_key="fake", channel_type="MAX")
            db.add(channel)
            db.flush()
            self.assertEqual(incoming._known_user_name(db, channel, "12345678901234567"), (None, None))
            self.assertEqual(bitrix_chat_id_for(channel, "123"), "max:123")
            channel.channel_type = None
            self.assertEqual(bitrix_chat_id_for(channel, "123"), "123")

    async def test_legacy_transports_keep_max_bot_max_and_null_contracts(self):
        payload = {"msg_type": "text", "max_id": "123", "text": "Legacy reply", "line_id": "7"}
        for kind in ("MAX", "MAX_BOT", None):
            with self.Session() as db:
                db.get(Channel, self.channel_id).channel_type = kind
                db.commit()
            with patch.object(worker, "send_message", AsyncMock(return_value={"outMessageId": "bot"})) as bot, \
                 patch.object(worker, "send_message_max", AsyncMock(return_value={"outMessageId": "max"})) as maximum:
                await worker._execute_outgoing(self.channel_id, payload)
            self.assertEqual(maximum.await_count, 1 if kind == "MAX" else 0)
            self.assertEqual(bot.await_count, 0 if kind == "MAX" else 1)
        with self.Session() as db:
            db.get(Channel, self.channel_id).channel_type = "TELEGRAM"
            db.commit()
        with patch.object(worker, "send_message_max", AsyncMock()) as maximum:
            with self.assertRaises(RuntimeError):
                await worker._execute_outgoing(self.channel_id, payload)
            maximum.assert_not_awaited()

    async def test_reconnect_uses_active_sender_but_preserves_dialog(self):
        await self.receive()
        with self.Session() as db:
            old = db.get(Channel, self.channel_id)
            old.is_active = False
            new = Channel(portal_id=self.portal_id, name="Reconnected", sender=old.sender,
                          api_key="new-synthetic", channel_type="TELEGRAM", subject_id=78, is_active=True)
            db.add(new)
            db.commit()
            new_id = new.id
        self.assertEqual((await self.client.post("/handler", json=self.event())).status_code, 200)
        with self.Session() as db:
            self.assertEqual(db.query(Message).filter_by(direction="outgoing").one().channel_id, new_id)

    async def test_disabled_telegram_profile_cannot_change_webhooks(self):
        profile = [{"subject": "new-tg", "subjectId": 89, "type": "TELEGRAM", "active": True}]
        with patch.object(maxbot, "get_channel_profiles", AsyncMock(return_value=profile)), \
             patch.object(maxbot, "set_in_message_callback", AsyncMock()) as callback, \
             patch.object(settings, "telegram_enabled", False):
            with self.assertRaises(maxbot.WebhookSetupError):
                await maxbot.configure_incoming_webhook("synthetic", "new-tg", "https://app.invalid/incoming/synthetic")
            callback.assert_not_awaited()

    async def test_form_creation_prepares_telegram_and_reports_incomplete_status(self):
        profile = [{"subject": "new-tg", "subjectId": 89, "type": "TELEGRAM", "active": True}]
        with patch.object(api, "require_channel_admin", AsyncMock()), \
             patch.object(api, "check_admin_context", AsyncMock(return_value=True)), \
             patch.object(maxbot, "get_channel_profiles", AsyncMock(return_value=profile)), \
             patch.object(maxbot, "set_in_message_callback", AsyncMock(return_value={"code": "ok"})), \
             patch.object(maxbot, "set_status_callback", AsyncMock(return_value={"code": "not-ready"})):
            result = await self.client.post("/api/channels", json={"member_id": "test-portal", "sender": "new-tg", "name": "New Telegram", "api_key": "synthetic"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["channel"]["channel_type"], "TELEGRAM")
        self.assertTrue(result.json()["setup_warnings"])
        self.assertEqual(self.bitrix.call_args.args[2], "imconnector.activate")

    async def test_common_line_activation_preserves_max_and_registers_telegram(self):
        with patch.object(api, "require_channel_admin", AsyncMock()), \
             patch.object(api, "activate_connector", AsyncMock()) as legacy:
            result = await self.client.post("/api/portal/open-line", json={"member_id": "test-portal", "line_id": "99"})
        self.assertEqual(result.status_code, 200, result.text)
        legacy.assert_awaited_once()
        self.assertEqual(self.bitrix.call_args.args[3]["LINE"], 99)
        self.assertEqual(self.bitrix.call_args.args[3]["CONNECTOR"], tg.CONNECTOR)


class TelegramTransportTests(unittest.IsolatedAsyncioTestCase):
    def test_access_logs_hide_path_tokens_but_keep_diagnostics(self):
        import logging
        from app.services.log_paths import PrivatePathFilter, public_path
        for path in ("/incoming/synthetic-secret", "/status/synthetic-secret", "/file/file.pdf?token=synthetic-secret&expires=123"):
            self.assertNotIn("synthetic-secret", public_path(path))
            record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %s',
                                       ("client", "POST", path, "1.1", 200), None)
            PrivatePathFilter().filter(record)
            self.assertNotIn("synthetic-secret", record.getMessage())
            self.assertIn("200", record.getMessage())

    def test_safe_traceback_does_not_dump_sql_or_http_credentials(self):
        private_value = "synthetic-private-token-in-sql-params"
        with self.assertLogs(tg.logger, level="ERROR") as logs:
            try:
                raise RuntimeError(private_value)
            except Exception:
                tg.log_exception("Telegram test failure task=%s", 12)
        self.assertIn("task=12", str(logs.output))
        self.assertNotIn("synthetic-private-token-in-sql-params", str(logs.output))

    async def test_edna_transport_builds_explicit_telegram_request(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={"outMessageId": "saved-id", "telegramId": "123"})
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(tg.httpx, "AsyncClient", return_value=client):
            await tg.send_text("synthetic-key", "sender", "123", "Hello", "saved-id")
        self.assertEqual(str(calls[0].url), tg.API_URL)
        data = json.loads(calls[0].content)
        self.assertEqual(data["to"], {"type": "TELEGRAM_ID", "value": "123"})
        self.assertEqual(data["outMessageId"], "saved-id")
        self.assertNotIn("maxId", data)

    async def test_ambiguous_edna_response_never_becomes_safe_retry(self):
        for status_code, payload in ((200, {"outMessageId": "other"}), (503, {"secret": "must-not-log"}), (200, [])):
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status_code, json=payload)))
            with patch.object(tg.httpx, "AsyncClient", return_value=client):
                with self.assertRaises(tg.TelegramError) as error:
                    await tg.send_text("synthetic", "sender", "123", "Text", "saved")
            self.assertTrue(error.exception.uncertain)
            self.assertFalse(error.exception.retryable)
            self.assertNotIn("must-not-log", str(error.exception))

    async def test_bitrix_http_200_business_failure_is_not_success(self):
        # Use the actual strict wrapper, not the mock used by TelegramCase.
        from types import SimpleNamespace
        portal = SimpleNamespace(client_endpoint="https://portal.invalid/rest/")
        for payload in ({"error": "denied", "error_description": "secret"},
                        {"result": {"SUCCESS": False}}, {"result": {"SUCCESS": True, "DATA": {"RESULT": [{"SUCCESS": False}]}}}):
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
            with patch.object(tg.httpx, "AsyncClient", return_value=client), patch.object(tg, "get_valid_token", AsyncMock(return_value="synthetic")):
                with self.assertRaises(tg.TelegramError):
                    await tg.bitrix_call(portal, None, "imconnector.send.messages", {})
