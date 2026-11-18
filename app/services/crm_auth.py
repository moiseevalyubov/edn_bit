"""Verify an app OAuth context on the saved portal endpoint, never on a UI URL."""
import logging
import re

from fastapi import HTTPException

from app.config import settings
from app.services.crm_bitrix import CrmBitrixError, call_crm_method

logger = logging.getLogger(__name__)


def _diagnostic_value(value):
    """Bound and sanitize selected fields; never serialize an OAuth response."""
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        return value
    return "<invalid:%s>" % type(value).__name__


def bearer_token(authorization: str | None) -> str:
    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not value or len(value) > 4096:
        raise HTTPException(401, "Откройте приложение в Битрикс24 для авторизации")
    return value


async def check_admin_context(portal, db, access_token=None, *, check_scope=False, allow_uninstalled=False, allow_installing=False):
    if not settings.bitrix_expected_app_code:
        raise CrmBitrixError("expected_app_code_missing", False)
    if portal.uninstalled_at is not None and not allow_uninstalled:
        raise CrmBitrixError("portal_uninstalled", False)
    info = await call_crm_method(portal, db, "app.info", access_token=access_token)
    # Only the installer may authenticate before BX24.installFinish marks the
    # application installed. Still require a matching app and an administrator.
    installed_ok = isinstance(info, dict) and (
        info.get("INSTALLED") is True
        or (allow_installing and access_token is not None and info.get("INSTALLED") is False)
    )
    if not isinstance(info, dict) or info.get("CODE") != settings.bitrix_expected_app_code or not installed_ok:
        fields = info if isinstance(info, dict) else {}
        logger.warning(
            "CRM app.info context rejected: portal_id=%s CODE=%s VERSION=%s INSTALLED=%s expected_CODE=%s response_type=%s",
            portal.id, _diagnostic_value(fields.get("CODE")),
            _diagnostic_value(fields.get("VERSION")), _diagnostic_value(fields.get("INSTALLED")),
            _diagnostic_value(settings.bitrix_expected_app_code), type(info).__name__,
        )
        raise CrmBitrixError("wrong_app_context", False)
    admin = await call_crm_method(portal, db, "user.admin", access_token=access_token)
    if admin is not True:
        raise CrmBitrixError("admin_required", False)
    if check_scope:
        scopes = await call_crm_method(portal, db, "scope", {"full": False}, access_token=access_token)
        if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
            raise CrmBitrixError("invalid_scope_response", True)
        return "messageservice" in scopes
    return True


async def require_channel_admin(portal, db, authorization: str | None, *, always=False, allow_uninstalled=False, allow_installing=False):
    # Preserve existing installations when the feature has never been enabled.
    if not (always or settings.crm_outreach_enabled or portal.crm_outreach_enabled or settings.telegram_enabled):
        return
    token = bearer_token(authorization)
    try:
        await check_admin_context(portal, db, token, allow_uninstalled=allow_uninstalled, allow_installing=allow_installing)
    except CrmBitrixError as exc:
        logger.exception(
            "CRM channel admin check rejected: portal_id=%s code=%s retryable=%s",
            portal.id, exc.code, exc.retryable,
        )
        if exc.retryable:
            raise HTTPException(503, "Битрикс24 временно недоступен. Повторите действие позже") from None
        if exc.code == "expected_app_code_missing":
            raise HTTPException(503, "Отправка из CRM ещё не настроена администратором сервера") from None
        raise HTTPException(403, "Для управления каналами нужен администратор этого приложения в Битрикс24") from None
