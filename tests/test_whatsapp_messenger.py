"""Synthetic WhatsApp API assumptions; live acceptance is still pending."""
import os
import unittest

if os.environ.get("CODEX_CRM_ISOLATED_TESTS") != "1":
    raise unittest.SkipTest("Use tests/run_isolated.py")

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx

from test_crm_foundation import DatabaseCase
from app.config import settings
from app.database import get_db
from app.models import Channel, Message, MessageDeliveryTask, Portal
from app.routers import api, handler, incoming, status
from app.services import whatsapp as wa, whatsapp_delivery as delivery, delivery_worker as worker, maxbot
from app.services.crm_channels import outreach_capability


class WhatsAppFixture(DatabaseCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        with self.Session() as db:
            channel = db.get(Channel, self.channel_id)
            channel.channel_type, channel.subject_id, channel.webhook_token = "WHATSAPP_MESSENGER", 78, "wa-token"
            portal = db.get(Portal, self.portal_id)
            portal.open_line_id, portal.app_token = "7", "synthetic-app-token"
            portal.token_expires_at = datetime.utcnow() + timedelta(hours=1)
            db.commit()
        self.worker_db = patch.object(worker, "SessionLocal", self.Session)
        self.worker_db.start()
        self.addCleanup(self.worker_db.stop)
        self.bitrix = AsyncMock(return_value={"SUCCESS": True})
        self.actual_bitrix_call = wa.bitrix_call
        self.bitrix_patch = patch.object(wa, "bitrix_call", self.bitrix)
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
                               "identifiers": [{"type": "WHATSAPP_MESSENGER_ID", "value": address}]},
                "userInfo": {"firstName": "Test", "lastName": "User"},
                "receivedAt": "2026-11-19T09:00:00Z", "messageContent": {"type": "TEXT", "text": "Hello"}}

    async def receive(self, **kwargs):
        response = await self.client.post("/incoming/wa-token", json=self.payload(**kwargs))
        self.assertEqual(response.status_code, 200, response.text)

    def event(self, message_id="501", text="[b]Operator:[/b] [br]Reply"):
        with self.Session() as db:
            chat = wa.chat_id(db.get(Channel, self.channel_id), "12345678901234567")
        return {"event": "ONIMCONNECTORMESSAGEADD", "auth": {"member_id": "test-portal", "application_token": "synthetic-app-token",
                 "access_token": "DO-NOT-STORE", "client_endpoint": "https://attacker.invalid/rest/"},
                "data": {"CONNECTOR": wa.CONNECTOR, "LINE": "7", "MESSAGES": [{"chat": {"id": chat},
                    "im": {"chat_id": "41", "message_id": message_id}, "message": {"text": text}}]}}

    def rows(self, kind):
        with self.Session() as db:
            return [(t.id, t.status, json.loads(t.payload), t.last_error) for t in db.query(MessageDeliveryTask).filter_by(task_type=kind).order_by(MessageDeliveryTask.id)]

    async def send_queued(self):
        await self.receive()
        self.assertTrue(await worker._process_one_task())
        result = await self.client.post("/handler", json=self.event())
        self.assertEqual(result.status_code, 200, result.text)


class WhatsAppCase(WhatsAppFixture):
    async def test_incoming_dedup_phone_name_and_own_connector(self):
        data = self.payload()
        data["subscriber"]["identifiers"].append({"type": "PHONE", "value": "79990000001"})
        for _ in range(2):
            reply = await self.client.post("/incoming/wa-token", json=data)
            self.assertEqual(reply.status_code, 200)
        self.assertEqual(len(self.rows(wa.RECEIVE)), 1)
        await worker._process_one_task()
        method, params = self.bitrix.call_args.args[2:]
        self.assertEqual(method, "imconnector.send.messages")
        self.assertEqual(params["CONNECTOR"], wa.CONNECTOR)
        item = params["MESSAGES"][0]
        self.assertEqual(item["user"]["phone"], "+79990000001")
        self.assertEqual(item["user"]["name"], "Test")
        self.assertTrue(item["user"]["id"].startswith("wa:"))
        self.assertNotEqual(item["user"]["id"], "999")
        self.assertNotIn("disable_crm", item["message"])
        self.assertNotIn("skip_phone_validate", item["user"])

    async def test_typed_identity_and_subject_required_no_max_fallback(self):
        for change in ("missing", "telegram", "subject", "ambiguous"):
            data = self.payload()
            if change == "missing":
                data["subscriber"].pop("identifiers")
            elif change == "telegram":
                data["subscriber"]["identifiers"][0]["type"] = "TELEGRAM_ID"
            elif change == "subject":
                data["subjectId"] = 999
            else:
                data["subscriber"]["identifiers"].append({"type": "WHATSAPP_MESSENGER_ID", "value": "other"})
            reply = await self.client.post("/incoming/wa-token", json=data)
            self.assertEqual(reply.status_code, 422, reply.text)
        self.assertEqual(self.rows(wa.RECEIVE), [])

    async def test_sender_and_messenger_isolation(self):
        from app.services import telegram
        from app.routers.incoming import _known_user_name
        with self.Session() as db:
            channel = db.get(Channel, self.channel_id)
            other = Channel(portal_id=self.portal_id, name="Second", sender="second-sender",
                            api_key="synthetic", channel_type=wa.CHANNEL_TYPE)
            self.assertNotEqual(wa.user_id(channel, "same"), wa.user_id(other, "same"))
            self.assertNotEqual(wa.chat_id(channel, "123"), telegram.chat_id(channel, "123"))
            self.assertNotEqual(wa.CONNECTOR, telegram.CONNECTOR)
            db.add(Message(channel_id=channel.id, direction="incoming", subscriber_identifier="123",
                           user_name="WhatsApp only", content_type="TEXT"))
            db.flush()
            self.assertEqual(_known_user_name(db, channel, "123"), (None, None))
            self.assertIsNone(outreach_capability(channel))

    async def test_reply_dedup_transport_and_credentials_not_persisted(self):
        await self.send_queued()
        reply = await self.client.post("/handler", json=self.event())
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(len(self.rows(wa.SEND)), 1)
        transport = AsyncMock(return_value={"outMessageId": "unused"})
        with patch.object(wa, "send_text", transport):
            await worker._process_one_task()
        self.assertEqual(transport.await_count, 1)
        self.assertEqual(transport.call_args.args[1:4], ("test-sender", "12345678901234567", "Reply"))
        with self.Session() as db:
            message = db.query(Message).filter_by(direction="outgoing").one()
            self.assertEqual(message.edna_request_id, transport.call_args.args[4])
            for task in db.query(MessageDeliveryTask):
                self.assertNotIn("DO-NOT-STORE", task.payload)
                self.assertNotIn("attacker.invalid", task.payload)

    async def test_wrong_connector_line_portal_and_token_cannot_send(self):
        await self.receive()
        for kind in ("line", "portal", "token", "connector"):
            event = self.event()
            if kind == "line":
                event["data"]["LINE"] = "8"
            elif kind == "portal":
                event["auth"]["member_id"] = "other"
            elif kind == "token":
                event["auth"]["application_token"] = "wrong"
            else:
                event["data"]["CONNECTOR"] = "max_bot"
            await self.client.post("/handler", json=event)
        self.assertEqual(self.rows(wa.SEND), [])
        with self.Session() as db:
            self.assertEqual(db.query(MessageDeliveryTask).filter_by(task_type="send_to_edna").count(), 0)

    async def test_fallback_and_unsupported_attachments_are_visible(self):
        data = self.payload()
        data["userInfo"] = None
        data["messageContent"] = {"type": "IMAGE", "caption": "Caption", "attachment": {"url": "https://private.invalid/x"}}
        reply = await self.client.post("/incoming/wa-token", json=data)
        self.assertEqual(reply.status_code, 200)
        await worker._process_one_task()
        item = self.bitrix.call_args.args[3]["MESSAGES"][0]
        self.assertEqual(item["user"]["name"], "12345678901234567")
        self.assertNotIn("last_name", item["user"])
        self.assertIn(delivery.UNSUPPORTED, item["message"]["text"])
        self.assertIn("Caption", item["message"]["text"])
        self.assertNotIn("files", item["message"])
        event = self.event()
        event["data"]["MESSAGES"][0]["message"]["files"] = [{"name": "a.pdf", "downloadLink": "https://portal.invalid/file?SIGN=secret"}]
        await self.client.post("/handler", json=event)
        task = self.rows(wa.SEND)[0]
        self.assertEqual(task[1], "failed")
        self.assertNotIn("SIGN", json.dumps(task[2]))
        await worker._process_one_task()
        notice = self.bitrix.call_args.args[3]["MESSAGES"][0]["message"]
        self.assertEqual(notice["disable_crm"], "Y")
        self.assertIn("только текст", notice["text"])

    async def test_read_only_and_late_failure_cannot_override_it(self):
        await self.send_queued()
        with patch.object(wa, "send_text", AsyncMock()):
            await worker._process_one_task()
        with self.Session() as db:
            message = db.query(Message).filter_by(direction="outgoing").one()
            request_id = message.edna_request_id
        self.bitrix.reset_mock()
        for status_value in ("SENT", "DELIVERED", "READ", "READ", "UNDELIVERED", "SENT"):
            response = await self.client.post("/status/wa-token", json={
                "requestId": request_id, "subject": "test-sender", "subjectId": 78,
                "status": status_value, "statusAt": "2026-11-20T10:00:00Z"})
            self.assertEqual(response.status_code, 200, response.text)
            await worker._process_one_task()
        self.assertEqual(self.bitrix.await_count, 1)
        self.assertEqual(self.bitrix.call_args.args[2], "imconnector.send.status.delivery")
        params = self.bitrix.call_args.args[3]
        self.assertEqual(params["CONNECTOR"], wa.CONNECTOR)
        self.assertEqual(params["MESSAGES"][0]["im"], {"chat_id": 41, "message_id": 501})
        self.assertEqual(params["MESSAGES"][0]["message"]["id"], [request_id])
        self.assertEqual(self.rows(wa.NOTICE), [])
        self.assertEqual(self.rows(wa.SEND)[0][2]["delivery_status"], "READ")

    async def test_disconnect_prevents_post_but_accepts_late_read(self):
        await self.send_queued()
        with self.Session() as db:
            db.get(Channel, self.channel_id).is_active = False
            request_id = db.query(Message).filter_by(direction="outgoing").one().edna_request_id
            db.commit()
        with patch.object(wa, "send_text", AsyncMock()) as send:
            await worker._process_one_task()
        send.assert_not_awaited()
        response = await self.client.post("/status/wa-token", json={"requestId": request_id, "status": "READ"})
        self.assertEqual(response.status_code, 200)
        while await worker._process_one_task():
            pass
        self.assertEqual(self.rows(wa.SEND)[0][2]["delivery_status"], "READ")

    async def test_uncertain_and_stale_send_are_never_blindly_repeated(self):
        await self.send_queued()
        with patch.object(wa, "send_text", AsyncMock(side_effect=wa.WhatsAppError("unknown", uncertain=True))) as send:
            await worker._process_one_task()
            await worker._process_one_task()
        self.assertEqual(send.await_count, 1)
        self.assertEqual(self.rows(wa.SEND)[0][1], "uncertain")
        with self.Session() as db:
            task = db.query(MessageDeliveryTask).filter_by(task_type=wa.SEND).one()
            task.status = "processing"
            task.updated_at = datetime.utcnow() - timedelta(hours=1)
            db.commit()
        await worker._reset_stale_tasks()
        self.assertEqual(self.rows(wa.SEND)[0][1], "uncertain")

    async def test_reconnect_keeps_dialog_and_selects_same_sender(self):
        await self.receive()
        with self.Session() as db:
            old = db.get(Channel, self.channel_id)
            old.is_active = False
            new = Channel(portal_id=self.portal_id, name="Reconnected", api_key="new-synthetic",
                          sender=old.sender, subject_id=78, channel_type=wa.CHANNEL_TYPE, is_active=True)
            db.add(new)
            db.commit()
            new_id = new.id
        response = await self.client.post("/handler", json=self.event())
        self.assertEqual(response.status_code, 200)
        with self.Session() as db:
            outgoing = db.query(Message).filter_by(direction="outgoing").one()
            self.assertEqual(outgoing.channel_id, new_id)
        self.assertEqual(len(self.rows(wa.SEND)), 1)

    async def test_connect_by_sender_without_manual_subject_id(self):
        profile = [{"subject": "new-wa", "subjectId": 89, "type": wa.CHANNEL_TYPE, "active": True}]
        with patch.object(api, "require_channel_admin", AsyncMock()), \
             patch.object(api, "check_admin_context", AsyncMock()), \
             patch.object(maxbot, "get_channel_profiles", AsyncMock(return_value=profile)), \
             patch.object(maxbot, "set_in_message_callback", AsyncMock(return_value={"code": "ok"})) as callback, \
             patch.object(maxbot, "set_status_callback", AsyncMock(return_value={"code": "ok"})), \
             patch.object(wa, "prepare_connector", AsyncMock()) as prepare:
            response = await self.client.post("/api/channels", json={
                "member_id": "test-portal", "sender": "new-wa", "name": "New WA", "api_key": "synthetic"})
        self.assertEqual(response.status_code, 200, response.text)
        prepare.assert_awaited_once()
        self.assertEqual(callback.call_args.args[1], 89)
        with self.Session() as db:
            channel = db.query(Channel).filter_by(sender="new-wa").one()
            self.assertEqual(channel.channel_type, wa.CHANNEL_TYPE)
            self.assertEqual(channel.subject_id, 89)

    async def test_first_channel_requires_admin_before_callbacks(self):
        with self.Session() as db:
            db.get(Channel, self.channel_id).channel_type = "MAX_BOT"
            db.commit()
        profile = [{"subject": "new-wa", "subjectId": 89, "type": wa.CHANNEL_TYPE, "active": True}]
        with patch.object(settings, "crm_outreach_enabled", False), \
             patch.object(maxbot, "get_channel_profiles", AsyncMock(return_value=profile)), \
             patch.object(maxbot, "set_in_message_callback", AsyncMock()) as callback:
            response = await self.client.post("/api/channels", json={
                "member_id": "test-portal", "sender": "new-wa", "name": "New WA", "api_key": "synthetic"})
        self.assertEqual(response.status_code, 401)
        callback.assert_not_awaited()

    async def test_credentials_remain_admin_owned_even_with_crm_disabled(self):
        from app.services.crm_auth import require_channel_admin
        with self.Session() as db:
            portal = db.get(Portal, self.portal_id)
            original = portal.access_token
            with patch.object(settings, "crm_outreach_enabled", False):
                handler.update_portal_tokens(portal, {"access_token": "employee"}, db)
                self.assertEqual(portal.access_token, original)
                with self.assertRaises(Exception) as caught:
                    await require_channel_admin(portal, db, None)
                self.assertEqual(caught.exception.status_code, 401)

    async def test_prepare_and_line_change_restore_whatsapp_connector(self):
        with patch.object(api, "require_channel_admin", AsyncMock()), \
             patch.object(api, "check_admin_context", AsyncMock()), \
             patch.object(api, "activate_connector", AsyncMock()), \
             patch.object(wa, "prepare_connector", AsyncMock()) as prepare:
            reply = await self.client.post("/api/whatsapp/prepare", json={"member_id": "test-portal"})
            self.assertEqual(reply.status_code, 200)
            self.assertTrue(reply.json()["enabled"])
            reply = await self.client.post("/api/portal/open-line", json={"member_id": "test-portal", "line_id": "8"})
            self.assertEqual(reply.status_code, 200)
        self.assertEqual(prepare.await_count, 2)
        self.assertEqual(prepare.call_args.args[2], "8")

    async def test_inactive_profile_does_not_replace_callbacks(self):
        profile = [{"subject": "new-wa", "subjectId": 89, "type": wa.CHANNEL_TYPE, "active": False}]
        with patch.object(api, "require_channel_admin", AsyncMock()), \
             patch.object(maxbot, "get_channel_profiles", AsyncMock(return_value=profile)), \
             patch.object(maxbot, "set_in_message_callback", AsyncMock()) as callback:
            response = await self.client.post("/api/channels", json={
                "member_id": "test-portal", "sender": "new-wa", "name": "New WA", "api_key": "synthetic"})
        self.assertEqual(response.status_code, 422)
        callback.assert_not_awaited()

    async def test_database_failure_does_not_acknowledge_incoming(self):
        from sqlalchemy.exc import OperationalError
        with patch.object(delivery, "_commit", side_effect=OperationalError("synthetic", {}, Exception("synthetic"))):
            response = await self.client.post("/incoming/wa-token", json=self.payload())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.rows(wa.RECEIVE), [])
        await self.receive()
        self.assertEqual(len(self.rows(wa.RECEIVE)), 1)

    async def test_conflicting_reply_and_partial_batch_roll_back(self):
        await self.receive()
        event = self.event()
        extra = self.event(message_id="502")["data"]["MESSAGES"][0]
        extra["chat"]["id"] = "unknown"
        event["data"]["MESSAGES"].append(extra)
        response = await self.client.post("/handler", json=event)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.rows(wa.SEND), [])
        await self.client.post("/handler", json=self.event())
        response = await self.client.post("/handler", json=self.event(text="Changed"))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.rows(wa.SEND)), 1)

    async def test_known_name_change_renames_without_resending_message(self):
        await self.receive()
        await worker._process_one_task()
        data = self.payload(message_id="102")
        data["userInfo"]["firstName"] = "New"
        await self.client.post("/incoming/wa-token", json=data)
        await worker._process_one_task()
        self.bitrix.reset_mock()
        await worker._process_one_task()
        self.assertEqual(self.bitrix.await_count, 1)
        self.assertEqual(self.bitrix.call_args.args[2], "imconnector.chat.name.set")
        params = self.bitrix.call_args.args[3]
        self.assertEqual(params["NAME"], "New User")
        self.assertTrue(params["USER_ID"].startswith("wa:"))

    async def test_second_sender_cannot_steal_reply_or_read(self):
        await self.send_queued()
        with self.Session() as db:
            other = Channel(portal_id=self.portal_id, name="Second", api_key="synthetic", sender="second",
                            channel_type=wa.CHANNEL_TYPE, subject_id=90, webhook_token="second-token", is_active=True)
            db.add(other)
            db.commit()
            request_id = db.query(Message).filter_by(direction="outgoing").one().edna_request_id
        data = self.payload(message_id="102")
        data.update(subject="second", subjectId=90)
        response = await self.client.post("/incoming/second-token", json=data)
        self.assertEqual(response.status_code, 200)
        with patch.object(wa, "send_text", AsyncMock()) as send:
            await worker._process_one_task()
        self.assertEqual(send.call_args.args[1], "test-sender")
        response = await self.client.post("/status/second-token", json={"requestId": request_id, "status": "READ"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.rows(wa.STATUS), [])


class WhatsAppTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_connector_registration_is_personal_and_uses_common_line(self):
        portal = type("Portal", (), {"open_line_id": "7"})()
        with patch.object(wa, "bitrix_call", AsyncMock()) as call:
            await wa.prepare_connector(portal, None)
        registration = call.call_args_list[0].args[3]
        activation = call.call_args_list[1].args[3]
        self.assertEqual(registration["ID"], wa.CONNECTOR)
        self.assertIs(registration["CHAT_GROUP"], False)
        self.assertEqual(activation, {"CONNECTOR": wa.CONNECTOR, "LINE": 7, "ACTIVE": "1"})

    async def test_payload_path_and_returned_recipient_contract(self):
        requests = []
        def respond(request):
            requests.append(request)
            data = json.loads(request.content)
            return httpx.Response(200, json={"outMessageId": data["outMessageId"], "whatsappMessengerId": data["to"]["value"]})
        client_factory = httpx.AsyncClient
        with patch.object(wa.httpx, "AsyncClient", side_effect=lambda: client_factory(transport=httpx.MockTransport(respond))):
            await wa.send_text("synthetic", "sender", "opaque:123@wa", "Hello", "request-1")
        self.assertEqual(str(requests[0].url), "https://app.edna.ru/api/v1/out-messages/wa-messenger")
        body = json.loads(requests[0].content)
        self.assertEqual(body["to"], {"type": "WHATSAPP_MESSENGER_ID", "value": "opaque:123@wa"})
        self.assertEqual(body["from"], "sender")
        self.assertEqual(body["outMessageId"], "request-1")
        self.assertEqual(body["content"], {"type": "TEXT", "text": "Hello"})

    async def test_transport_classifies_unknown_and_explicit_rejection(self):
        client_factory = httpx.AsyncClient
        for code, retryable, uncertain in ((429, True, False), (500, False, True), (400, False, False)):
            with patch.object(wa.httpx, "AsyncClient", side_effect=lambda: client_factory(transport=httpx.MockTransport(lambda r: httpx.Response(code)))):
                with self.assertRaises(wa.WhatsAppError) as caught:
                    await wa.send_text("synthetic", "sender", "123", "Hello", "request-1")
            self.assertEqual((caught.exception.retryable, caught.exception.uncertain), (retryable, uncertain))
