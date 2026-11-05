"""Run local checks without loading a project .env or using real credentials.

Usage: .venv/Scripts/python.exe tests/run_isolated.py
Optional PostgreSQL checks use ONLY an explicit CRM_TEST_POSTGRES_URL. They create
and remove a random test schema; the application's DATABASE_URL is never reused.
"""
import base64
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import httpx


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    # Only this explicitly test-specific setting is carried into the test process.
    postgres_url = os.environ.get("CRM_TEST_POSTGRES_URL", "")
    safe_env = {
        "DATABASE_URL": "sqlite://",
        "SECRET_KEY": base64.urlsafe_b64encode(bytes(range(32))).decode("ascii"),
        "APP_BASE_URL": "https://app.invalid",
        "CRM_OUTREACH_ENABLED": "0",
        "CRM_TEST_POSTGRES_URL": postgres_url,
        "CODEX_CRM_ISOLATED_TESTS": "1",
    }
    previous_cwd = Path.cwd()
    sys.path.insert(0, str(project))
    with tempfile.TemporaryDirectory(prefix="crm-foundation-tests-") as directory:
        try:
            os.chdir(directory)  # Settings' relative .env cannot reach the real one.
            with patch.dict(os.environ, safe_env, clear=True):
                # A missed test double must fail instead of sending a real HTTP request.
                original_send = httpx.AsyncClient.send
                async def isolated_send(client, request, *args, **kwargs):
                    if not isinstance(client._transport, (httpx.ASGITransport, httpx.MockTransport)):
                        raise AssertionError("Network disabled in local tests")
                    return await original_send(client, request, *args, **kwargs)
                with patch("httpx.AsyncClient.send", new=isolated_send):
                    suite = unittest.defaultTestLoader.discover(str(project / "tests"))
                    result = unittest.TextTestRunner(verbosity=2).run(suite)
                    return 0 if result.wasSuccessful() else 1
        finally:
            # Windows cannot remove a directory while it is the process cwd.
            os.chdir(previous_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
