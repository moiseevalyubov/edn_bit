"""An existing portal must finish updating while Bitrix reports INSTALLED=false."""
import os
import unittest

if os.environ.get("CODEX_CRM_ISOLATED_TESTS") != "1":
    raise unittest.SkipTest("Use tests/run_isolated.py")

from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx

from test_crm_outreach import OutreachCase
from app.config import settings
from app.database import get_db
from app.models import Channel, Portal
from app.routers import install
from app.services import crm_auth


class InstallUpdateTests(OutreachCase, unittest.IsolatedAsyncioTestCase):
    async def test_update_finishes_without_reconnecting_existing_channel(self):
        app = FastAPI()
        app.include_router(install.router)

        def database():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = database
        with self.Session() as db:
            channel = db.get(Channel, self.channel_id)
            original_channel = (channel.name, channel.sender, channel.webhook_token, channel.is_active)
            original_endpoint = db.get(Portal, self.portal_id).client_endpoint

        with patch.object(settings, "bitrix_expected_app_code", "test.app"), \
             patch.object(crm_auth, "call_crm_method", new=AsyncMock(side_effect=[
                 {"CODE": "test.app", "VERSION": 2, "INSTALLED": False}, True,
             ])) as call, \
             patch.object(install, "register_connector", new=AsyncMock()), \
             patch.object(install, "bind_events", new=AsyncMock()), \
             patch.object(install, "activate_connector", new=AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
                response = await client.post("/install?DOMAIN=portal.invalid&PROTOCOL=1", data={
                    "member_id": "test-portal", "AUTH_ID": "updated-admin-token",
                    "REFRESH_ID": "updated-refresh", "APPLICATION_TOKEN": "updated-app-token",
                })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("BX24.installFinish()", response.text)
        self.assertEqual([c.args[2] for c in call.await_args_list], ["app.info", "user.admin"])
        self.assertTrue(all(c.kwargs["access_token"] == "updated-admin-token" for c in call.await_args_list))
        with self.Session() as db:
            portal = db.get(Portal, self.portal_id)
            self.assertEqual(portal.access_token, "updated-admin-token")
            self.assertEqual(portal.refresh_token, "updated-refresh")
            self.assertEqual(portal.app_token, "updated-app-token")
            self.assertEqual(portal.client_endpoint, original_endpoint)
            channel = db.get(Channel, self.channel_id)
            self.assertEqual((channel.name, channel.sender, channel.webhook_token, channel.is_active), original_channel)
