"""Provider lifecycle via the existing form API, with an isolated Bitrix mock."""
import os
import unittest

if os.environ.get("CODEX_CRM_ISOLATED_TESTS") != "1":
    raise unittest.SkipTest("Use tests/run_isolated.py")

from datetime import datetime, timedelta
import json
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx

from test_crm_outreach import OutreachCase
from app.config import settings
from app.database import get_db
from app.models import Channel, CrmMessageProvider, CrmOutgoingMessage, MessageDeliveryTask, Portal
from app.routers import api, crm_management, handler, install, probe22
from app.services import delivery_worker
from app.services.crm_providers import reconcile_local_providers, stage_portal, stage_provider
from app.services.task_types import SYNC_CRM_PROVIDER


class ProviderTests(OutreachCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        with self.Session() as db:
            db.query(CrmMessageProvider).delete()
            portal = db.get(Portal, self.portal_id)
            portal.crm_outreach_enabled = False
            portal.token_expires_at = datetime.utcnow() + timedelta(hours=1)
            db.commit()
        self.app_code = patch.object(settings, "bitrix_expected_app_code", "test.app")
        self.app_code.start()
        self.addCleanup(self.app_code.stop)
        self.saved_client = httpx.AsyncClient
        self.remote = {}
        self.calls = []
        self.granted_scope = True
        self.app = FastAPI()
        for router in (api.router, crm_management.router, install.router, probe22.router):
            self.app.include_router(router)
        def database():
            with self.Session() as db:
                yield db
        self.app.dependency_overrides[get_db] = database
        self.client = self.saved_client(transport=httpx.ASGITransport(app=self.app), base_url="https://app.invalid")
        self.http = patch.object(httpx, "AsyncClient", side_effect=self.mock_client)
        self.http.start()
        self.addCleanup(self.http.stop)
        self.worker_session = patch.object(delivery_worker, "SessionLocal", self.Session)
        self.worker_session.start()
        self.addCleanup(self.worker_session.stop)

    async def asyncTearDown(self):
        await self.client.aclose()

    def mock_client(self, *args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(self.bitrix))
        return self.saved_client(*args, **kwargs)

    def bitrix(self, request):
        self.assertEqual(request.url.host, "portal.invalid")
        method = request.url.path.rsplit("/", 1)[-1]
        data = json.loads(request.content)
        self.calls.append((method, data))
        token = data["auth"]
        if token == "other-portal":
            return httpx.Response(200, json={"error": "INVALID_CREDENTIALS"})
        if method == "app.info":
            result = {"CODE": "other.app" if token == "foreign-app" else "test.app", "INSTALLED": True}
        elif method == "user.admin":
            result = token != "operator"
        elif method == "scope":
            self.assertIs(data.get("full"), False)
            result = ["imopenlines", "messageservice"] if self.granted_scope else ["imopenlines"]
        elif method == "messageservice.sender.list":
            result = list(self.remote)
        elif method == "messageservice.sender.add":
            if data["CODE"] in self.remote:
                return httpx.Response(200, json={"error": "ERROR_SENDER_ALREADY_INSTALLED"})
            self.assertEqual(data["TYPE"], "SMS")
            self.remote[data["CODE"]] = data.copy()
            result = True
        elif method == "messageservice.sender.update":
            if data["CODE"] not in self.remote:
                return httpx.Response(200, json={"error": "ERROR_SENDER_NOT_FOUND"})
            self.remote[data["CODE"]].update(data)
            result = True
        elif method == "messageservice.sender.delete":
            self.remote.pop(data["CODE"], None)
            result = True
        elif method == "messageservice.message.status.update":
            result = True
        else:
            raise AssertionError("Unexpected external method: " + method)
        return httpx.Response(200, json={"result": result})

    def auth(self, token="ui-admin"):
        return {"Authorization": "Bearer " + token}

    async def prepare(self):
        result = await self.client.post("/api/crm/prepare", json={"member_id": "test-portal"}, headers=self.auth())
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    async def sync(self):
        for _ in range(12):
            if not await delivery_worker._process_one_task():
                return
        self.fail("Queue did not become idle")

    def crm_data(self, message_id="external-id"):
        data = self.data(message_id)
        data["code"] = "edna_max_" + str(self.channel_id)
        return data

    async def test_opening_settings_automatically_registers_existing_active_max(self):
        result = await self.prepare()
        self.assertTrue(result["enabled"])
        self.assertEqual(len(self.tasks(SYNC_CRM_PROVIDER)), 1)
        await self.prepare()  # Pending sync does not create another revision/job.
        self.assertEqual(len(self.tasks(SYNC_CRM_PROVIDER)), 1)
        await self.sync()
        code = "edna_max_" + str(self.channel_id)
        self.assertEqual(self.remote[code]["NAME"], "Test MAX в приложении MAX от edna")
        self.assertEqual(self.remote[code]["HANDLER"], "https://app.invalid/crm/messages/send")
        with self.Session() as db:
            self.assertEqual(db.query(CrmMessageProvider).one().sync_state, "ready")
            self.assertEqual(db.get(Portal, self.portal_id).access_token, "synthetic-access")

    async def test_existing_probe_code_is_updated_without_duplicate_add(self):
        code = "edna_max_" + str(self.channel_id)
        self.remote[code] = {"NAME": "probe", "HANDLER": "https://app.invalid/probe22/send",
                             "DESCRIPTION": "Проба #22: сообщение в MAX по номеру телефона"}
        await self.prepare()
        await self.sync()
        self.assertEqual(self.remote[code]["HANDLER"], "https://app.invalid/crm/messages/send")
        self.assertFalse(any(method == "messageservice.sender.add" for method, _ in self.calls))
        self.assertEqual(self.remote[code]["DESCRIPTION"], "Отправка сообщений из CRM через edna")

    async def test_reopening_ready_form_does_not_resync_providers(self):
        await self.prepare()
        await self.sync()
        before = self.tasks(SYNC_CRM_PROVIDER)
        with self.Session() as db:
            revision = db.query(CrmMessageProvider).one().desired_revision
        self.calls.clear()
        result = await self.prepare()
        self.assertTrue(result["enabled"])
        self.assertEqual(result["providers"][0]["state"], "ready")
        self.assertEqual(self.tasks(SYNC_CRM_PROVIDER), before)
        with self.Session() as db:
            self.assertEqual(db.query(CrmMessageProvider).one().desired_revision, revision)
        self.assertFalse(await delivery_worker._process_one_task())
        self.assertFalse(any(method.startswith("messageservice.sender.") for method, _ in self.calls))

    async def test_reopening_during_retry_preserves_backoff_and_recovers_terminal_error(self):
        await self.prepare()
        original = self.bitrix
        def unavailable(request):
            if request.url.path.endswith("messageservice.sender.list"):
                return httpx.Response(503, json={"error": "INTERNAL_SERVER_ERROR"})
            return original(request)
        with patch.object(self, "bitrix", side_effect=unavailable):
            await delivery_worker._process_one_task()
        with self.Session() as db:
            provider = db.query(CrmMessageProvider).one()
            revision, retry_at = provider.desired_revision, provider.next_attempt_at
        await self.prepare()
        with self.Session() as db:
            provider = db.query(CrmMessageProvider).one()
            self.assertEqual(provider.desired_revision, revision)
            self.assertEqual(provider.next_attempt_at, retry_at)
            provider.next_attempt_at = None
            task = db.query(MessageDeliveryTask).filter_by(task_type=SYNC_CRM_PROVIDER).one()
            task.status = "dead"
            db.commit()
        await self.prepare()
        with self.Session() as db:
            self.assertEqual(db.query(CrmMessageProvider).one().desired_revision, revision + 1)
        await self.sync()
        self.assertEqual(len(self.remote), 1)

    async def test_version_one_without_messageservice_keeps_channel_management(self):
        self.granted_scope = False
        with self.Session() as db:
            db.get(Portal, self.portal_id).app_token = None
            db.commit()
        result = await self.prepare()
        self.assertEqual(result["state"], "scope_required")
        with patch.object(api, "configure_incoming_webhook", AsyncMock(return_value={"subject_id": 20, "type": "MAX"})):
            response = await self.client.post("/api/channels", headers=self.auth(), json={
                "member_id": "test-portal", "name": "Legacy second", "api_key": "synthetic-key-2", "sender": "sender-2"})
        self.assertEqual(response.status_code, 200, response.text)
        response = await self.client.patch("/api/channels/" + str(self.channel_id), headers=self.auth(),
                                          json={"member_id": "test-portal", "name": "Legacy MAX"})
        self.assertEqual(response.status_code, 200, response.text)
        response = await self.client.post(f"/api/channels/{self.channel_id}/disconnect?member_id=test-portal",
                                         headers=self.auth())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.tasks(SYNC_CRM_PROVIDER), [])
        self.assertFalse(any(method.startswith("messageservice.") for method, _ in self.calls))

    async def test_feature_disabled_keeps_legacy_endpoints_without_new_authorization(self):
        self.granted_scope = False
        with patch.object(settings, "crm_outreach_enabled", False), patch.object(settings, "bitrix_expected_app_code", ""):
            response = await self.client.post("/api/crm/prepare", json={"member_id": "test-portal"})
            self.assertEqual(response.json()["state"], "disabled")
            response = await self.client.get("/api/channels?member_id=test-portal")
            self.assertEqual(response.status_code, 200)
            response = await self.client.post(f"/api/channels/{self.channel_id}/disconnect?member_id=test-portal")
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.tasks(SYNC_CRM_PROVIDER), [])

    async def test_multiple_channels_have_independent_codes_and_max_bot_stays_out(self):
        with self.Session() as db:
            for name, kind in (("Second MAX", "MAX"), ("Bot", "MAX_BOT"), ("Future", "WHATSAPP")):
                db.add(Channel(portal_id=self.portal_id, name=name, channel_type=kind, api_key="synthetic", sender=name, is_active=True))
            db.commit()
        await self.prepare()
        await self.sync()
        self.assertEqual(len(self.remote), 2)
        self.assertEqual(len({row["NAME"] for row in self.remote.values()}), 2)

    async def test_existing_connect_form_saves_channel_and_sync_job_together(self):
        await self.prepare()
        await self.sync()
        with patch.object(api, "configure_incoming_webhook", AsyncMock(return_value={"subject_id": 20, "type": "MAX"})):
            response = await self.client.post("/api/channels", headers=self.auth(), json={
                "member_id": "test-portal", "name": "Sales", "api_key": "synthetic-key-2", "sender": "sender-2"})
        self.assertEqual(response.status_code, 200, response.text)
        new_id = response.json()["channel"]["id"]
        with self.Session() as db:
            self.assertIsNotNone(db.query(CrmMessageProvider).filter_by(channel_id=new_id).one_or_none())
        await self.sync()
        self.assertIn("edna_max_" + str(new_id), self.remote)

    async def test_rename_updates_name_preserving_code_and_messages_can_still_be_accepted(self):
        await self.prepare()
        await self.sync()
        response = await self.client.patch("/api/channels/" + str(self.channel_id), headers=self.auth(),
                                          json={"member_id": "test-portal", "name": "New name"})
        self.assertEqual(response.status_code, 200, response.text)
        self.receive(self.crm_data())  # Pending NAME sync does not disable a working sender.
        with self.Session() as db:
            # Hold the send: this test verifies lifecycle, not a real edna call.
            db.query(MessageDeliveryTask).filter_by(task_type="send_crm_to_edna").update({"next_attempt_at": datetime.utcnow() + timedelta(hours=1)})
            db.commit()
        await self.sync()
        self.assertEqual(len(self.remote), 1)
        self.assertEqual(next(iter(self.remote.values()))["NAME"], "New name в приложении MAX от edna")

    async def test_disconnect_cancels_unsent_but_preserves_accepted_and_late_read(self):
        await self.prepare()
        await self.sync()
        queued = self.receive(self.crm_data("queued-id"))
        accepted = self.receive(self.crm_data("accepted-id"))
        with self.Session() as db:
            db.get(CrmOutgoingMessage, accepted).send_state = "accepted"
            db.commit()
        response = await self.client.post(f"/api/channels/{self.channel_id}/disconnect?member_id=test-portal", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.snapshot(queued)["send_state"], "failed")
        self.assertEqual(self.snapshot(accepted)["send_state"], "accepted")
        await self.sync()
        self.assertEqual(self.remote, {})
        self.fact(accepted, "READ")
        self.assertEqual(self.snapshot(accepted)["send_state"], "read")

    async def test_foreign_app_operator_and_other_portal_cannot_enable_or_manage(self):
        for token in ("operator", "foreign-app", "other-portal"):
            with self.subTest(token=token):
                response = await self.client.post("/api/crm/prepare", json={"member_id": "test-portal"}, headers=self.auth(token))
                self.assertEqual(response.status_code, 403)
                response = await self.client.post(f"/api/channels/{self.channel_id}/disconnect?member_id=test-portal", headers=self.auth(token))
                self.assertEqual(response.status_code, 403)
        response = await self.client.post(f"/api/channels/{self.channel_id}/disconnect?member_id=test-portal")
        self.assertEqual(response.status_code, 401)
        with self.Session() as db:
            self.assertTrue(db.get(Channel, self.channel_id).is_active)
            self.assertFalse(db.get(Portal, self.portal_id).crm_outreach_enabled)

    async def test_scope_and_trusted_app_token_are_required_without_exposing_secrets(self):
        self.granted_scope = False
        response = await self.prepare()
        self.assertEqual(response["state"], "scope_required")
        self.assertFalse(response["enabled"])
        self.granted_scope = True
        with self.Session() as db:
            db.get(Portal, self.portal_id).app_token = None
            db.commit()
        response = await self.prepare()
        self.assertEqual(response["state"], "app_token_required")
        self.assertFalse(response["enabled"])
        self.assertEqual(self.tasks(), [])

    async def test_missing_expected_app_code_fails_closed(self):
        with patch.object(settings, "bitrix_expected_app_code", ""):
            response = await self.client.post("/api/crm/prepare", json={"member_id": "test-portal"}, headers=self.auth())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.calls, [])

    async def test_operator_oauth_is_not_used_as_background_admin(self):
        with self.Session() as db:
            db.get(Portal, self.portal_id).access_token = "operator"
            db.commit()
        response = await self.prepare()
        self.assertEqual(response["state"], "server_authorization_required")
        self.assertFalse(response["enabled"])
        self.assertEqual(self.tasks(), [])

    async def test_diagnostics_are_admin_only_and_omit_text_phone_and_credentials(self):
        await self.prepare()
        await self.sync()
        message_id = self.receive(self.crm_data())
        with self.Session() as db:
            message = db.get(CrmOutgoingMessage, message_id)
            message.send_state = "uncertain"
            message.attempt_started_at = datetime.utcnow()
            db.commit()
        response = await self.client.post("/api/crm/diagnostics", json={"member_id": "test-portal"}, headers=self.auth("operator"))
        self.assertEqual(response.status_code, 403)
        response = await self.client.post("/api/crm/diagnostics", json={"member_id": "test-portal"}, headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["uncertain"][0]["message_id"], "external-id")
        for forbidden in ("79990000001", "Test CRM text", "synthetic-app-token", "synthetic-access", "synthetic-key"):
            self.assertNotIn(forbidden, response.text)

    async def test_disabled_feature_preserves_legacy_form_and_never_enables_itself(self):
        with patch.object(settings, "crm_outreach_enabled", False):
            response = await self.prepare()
            self.assertEqual(response["state"], "disabled")
            response = await self.client.post(f"/api/channels/{self.channel_id}/disconnect?member_id=test-portal")
            self.assertEqual(response.status_code, 200)
            reconcile_local_providers(self.Session)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.tasks(), [])

    async def test_global_disable_removes_old_provider_but_does_not_reregister_it(self):
        await self.prepare()
        await self.sync()
        with patch.object(settings, "crm_outreach_enabled", False):
            reconcile_local_providers(self.Session)
            await self.sync()
        self.assertEqual(self.remote, {})
        with self.Session() as db:
            self.assertEqual(db.query(CrmMessageProvider).one().sync_state, "absent")

    async def test_uninstall_disables_local_delivery_and_never_calls_remote_with_old_oauth(self):
        await self.prepare()
        await self.sync()
        message_id = self.receive(self.crm_data())
        self.calls.clear()
        with self.Session() as db:
            await handler._handle_app_uninstall(db.get(Portal, self.portal_id), db)
        await self.sync()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.snapshot(message_id)["send_state"], "failed")
        with self.Session() as db:
            self.assertFalse(db.get(Portal, self.portal_id).crm_outreach_enabled)

    async def test_event_cannot_overwrite_admin_oauth_when_crm_is_in_use(self):
        with self.Session() as db:
            portal = db.get(Portal, self.portal_id)
            handler.update_portal_tokens(portal, {"access_token": "operator", "client_endpoint": "https://evil.invalid/rest/"}, db)
            db.refresh(portal)
            self.assertEqual(portal.access_token, "synthetic-access")
            self.assertEqual(portal.client_endpoint, "https://portal.invalid/rest/")

    async def test_install_update_cannot_poison_saved_endpoint_with_another_app_token(self):
        response = await self.client.post("/install", data={"member_id": "test-portal", "AUTH_ID": "foreign-app",
                                                         "client_endpoint": "https://evil.invalid/rest/"})
        self.assertEqual(response.status_code, 403)
        with self.Session() as db:
            self.assertEqual(db.get(Portal, self.portal_id).client_endpoint, "https://portal.invalid/rest/")

    async def test_probe_cannot_fake_delivered_while_real_crm_feature_is_enabled(self):
        with patch.object(settings, "probe_22", "1"):
            response = await self.client.get("/probe22/register?sender=test-sender")
        self.assertEqual(response.status_code, 404)

    async def test_managed_portal_cannot_return_to_probe_during_global_disable(self):
        await self.prepare()
        await self.sync()
        count = len(self.calls)
        with patch.object(settings, "crm_outreach_enabled", False), patch.object(settings, "probe_22", "1"), \
             patch.object(probe22, "SessionLocal", self.Session), patch.object(probe22.asyncio, "sleep", AsyncMock()):
            response = await self.client.get("/probe22/register?sender=test-sender")
            self.assertEqual(response.status_code, 404)
            response = await self.client.post("/probe22/send", json=self.crm_data())
            self.assertEqual(response.status_code, 403)
            await probe22._mark_delivered("test-portal", "edna_max_1", "fake-id")
        self.assertEqual(len(self.calls), count)

    async def test_already_installed_race_still_updates_handler(self):
        await self.prepare()
        code = "edna_max_" + str(self.channel_id)
        original = self.bitrix
        def raced(request):
            if request.url.path.endswith("messageservice.sender.list"):
                self.remote[code] = {"HANDLER": "https://app.invalid/probe22/send"}
                return httpx.Response(200, json={"result": []})
            return original(request)
        with patch.object(self, "bitrix", side_effect=raced):
            await self.sync()
        self.assertEqual(self.remote[code]["HANDLER"], "https://app.invalid/crm/messages/send")

    async def test_provider_retry_and_reopen_recover_without_duplicate_senders(self):
        await self.prepare()
        original = self.bitrix
        def unavailable(request):
            if request.url.path.endswith("messageservice.sender.list"):
                return httpx.Response(503, json={"error": "INTERNAL_SERVER_ERROR", "error_description": "do-not-save-sensitive"})
            return original(request)
        with patch.object(self, "bitrix", side_effect=unavailable):
            await delivery_worker._process_one_task()
        with self.Session() as db:
            provider = db.query(CrmMessageProvider).one()
            self.assertEqual(provider.sync_state, "error")
            self.assertNotIn("sensitive", provider.last_error)
            db.query(MessageDeliveryTask).filter_by(task_type=SYNC_CRM_PROVIDER).update({"next_attempt_at": datetime.utcnow() - timedelta(seconds=1)})
            db.commit()
        await self.sync()
        await self.prepare()  # Healthy providers remain ready without another revision.
        await self.sync()
        self.assertEqual(len(self.remote), 1)
        self.assertEqual(sum(method == "messageservice.sender.add" for method, _ in self.calls), 1)

    async def test_name_change_during_external_sync_is_applied_by_next_revision(self):
        await self.prepare()
        original = self.bitrix
        changed = False
        def rename_during_update(request):
            nonlocal changed
            response = original(request)
            if not changed and request.url.path.endswith("messageservice.sender.update"):
                changed = True
                with self.Session() as db:
                    portal = db.get(Portal, self.portal_id)
                    channel = db.get(Channel, self.channel_id)
                    channel.name = "Changed during HTTP"
                    stage_provider(db, portal, channel)
                    db.commit()
            return response
        with patch.object(self, "bitrix", side_effect=rename_during_update):
            await self.sync()
        self.assertEqual(next(iter(self.remote.values()))["NAME"], "Changed during HTTP в приложении MAX от edna")
        with self.Session() as db:
            provider = db.query(CrmMessageProvider).one()
            self.assertEqual(provider.applied_revision, provider.desired_revision)

    async def test_verified_reinstall_requires_fresh_automatic_preparation(self):
        with self.Session() as db:
            portal = db.get(Portal, self.portal_id)
            portal.uninstalled_at = datetime.utcnow()
            db.commit()
        with patch.object(install, "register_connector", AsyncMock()), patch.object(install, "bind_events", AsyncMock()):
            response = await self.client.post("/install", data={"member_id": "test-portal", "AUTH_ID": "ui-admin", "REFRESH_ID": "synthetic-refresh",
                                                             "client_endpoint": "https://ignored.invalid/rest/"})
        self.assertEqual(response.status_code, 200, response.text)
        with self.Session() as db:
            portal = db.get(Portal, self.portal_id)
            self.assertIsNone(portal.uninstalled_at)
            self.assertFalse(portal.crm_outreach_enabled)
            self.assertEqual(portal.client_endpoint, "https://portal.invalid/rest/")
        self.assertTrue((await self.prepare())["enabled"])

    async def test_foreign_channel_cannot_be_renamed_or_disconnected(self):
        with self.Session() as db:
            other = Portal(member_id="another-portal", client_endpoint="https://other.invalid/rest/", access_token="synthetic", refresh_token="synthetic")
            db.add(other)
            db.flush()
            channel = Channel(portal_id=other.id, name="Other", api_key="synthetic", sender="other", is_active=True)
            db.add(channel)
            db.commit()
            other_id = channel.id
        response = await self.client.patch(f"/api/channels/{other_id}", headers=self.auth(), json={"member_id": "test-portal", "name": "Wrong"})
        self.assertEqual(response.status_code, 403)
        response = await self.client.post(f"/api/channels/{other_id}/disconnect?member_id=test-portal", headers=self.auth())
        self.assertEqual(response.status_code, 403)
