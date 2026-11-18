"""Auth rejection diagnostics must distinguish failures without leaking OAuth data."""
import os
import unittest

if os.environ.get("CODEX_CRM_ISOLATED_TESTS") != "1":
    raise unittest.SkipTest("Use tests/run_isolated.py")

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.config import settings
from app.services import crm_auth
from app.services.crm_bitrix import CrmBitrixError


class AuthDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_install_logs_reason_and_keeps_denial(self):
        portal = SimpleNamespace(id=123, uninstalled_at=None, crm_outreach_enabled=True)
        info = {"CODE": "test.app", "VERSION": 2, "INSTALLED": False,
                "access_token": "SECRET_FROM_RESPONSE", "extra": "PRIVATE_DATA"}
        with patch.object(settings, "bitrix_expected_app_code", "test.app"), \
             patch.object(crm_auth, "call_crm_method", new=AsyncMock(return_value=info)) as call, \
             self.assertLogs(crm_auth.logger, level="WARNING") as logs:
            with self.assertRaises(HTTPException) as raised:
                await crm_auth.require_channel_admin(portal, None, "Bearer SECRET_UI_TOKEN")
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(call.await_count, 1)
        output = "\n".join(logs.output)
        for expected in ("CODE=test.app", "VERSION=2", "INSTALLED=False", "code=wrong_app_context"):
            self.assertIn(expected, output)
        for secret in ("SECRET_FROM_RESPONSE", "PRIVATE_DATA", "SECRET_UI_TOKEN"):
            self.assertNotIn(secret, output)

    async def test_rest_error_logs_only_safe_code_and_preserves_response(self):
        portal = SimpleNamespace(id=123, uninstalled_at=None, crm_outreach_enabled=True)
        with patch.object(settings, "bitrix_expected_app_code", "test.app"), \
             patch.object(crm_auth, "call_crm_method", new=AsyncMock(side_effect=CrmBitrixError("INVALID_CREDENTIALS", False))), \
             self.assertLogs(crm_auth.logger, level="ERROR") as logs:
            with self.assertRaises(HTTPException) as raised:
                await crm_auth.require_channel_admin(portal, None, "Bearer SECRET_UI_TOKEN")
        self.assertEqual(raised.exception.status_code, 403)
        output = "\n".join(logs.output)
        self.assertIn("code=INVALID_CREDENTIALS", output)
        self.assertNotIn("SECRET_UI_TOKEN", output)

    def test_unexpected_field_content_is_not_logged(self):
        self.assertEqual(crm_auth._diagnostic_value("secret\nINJECTED=value"), "<invalid:str>")
        self.assertEqual(crm_auth._diagnostic_value({"auth": "secret"}), "<invalid:dict>")

    async def test_installing_exception_still_checks_admin_and_app_identity(self):
        portal = SimpleNamespace(id=123, uninstalled_at=None, crm_outreach_enabled=True)
        for code, installed, admin, expected_calls, allowed in (
            ("test.app", False, True, 2, True),
            ("other.app", False, True, 1, False),
            ("test.app", False, False, 2, False),
            ("test.app", None, True, 1, False),
            ("test.app", "false", True, 1, False),
            ("test.app", True, True, 2, True),
        ):
            with self.subTest(code=code, installed=installed, admin=admin), \
                 patch.object(settings, "bitrix_expected_app_code", "test.app"), \
                 patch.object(crm_auth, "call_crm_method", new=AsyncMock(side_effect=[
                     {"CODE": code, "INSTALLED": installed}, admin,
                 ])) as call:
                if allowed:
                    await crm_auth.require_channel_admin(portal, None, "Bearer synthetic-admin", allow_installing=True)
                else:
                    with self.assertRaises(HTTPException) as raised:
                        await crm_auth.require_channel_admin(portal, None, "Bearer synthetic-admin", allow_installing=True)
                    self.assertEqual(raised.exception.status_code, 403)
                self.assertEqual(call.await_count, expected_calls)
