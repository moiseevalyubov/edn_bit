"""Bitrix calls for CRM. Safe errors, including HTTP 200 with an API error."""
import re
from urllib.parse import urlsplit

import httpx

from app.services.token import get_valid_token


class CrmBitrixError(Exception):
    def __init__(self, code: str, retryable: bool):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


async def call_crm_method(portal, db, method: str, params: dict | None = None, *, access_token: str | None = None):
    if method not in {"app.info", "user.admin", "scope", "messageservice.sender.add", "messageservice.sender.update",
                       "messageservice.sender.delete", "messageservice.sender.list", "messageservice.message.status.update"}:
        raise ValueError("Unsupported CRM REST method")
    endpoint = urlsplit(portal.client_endpoint or "")
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise CrmBitrixError("invalid_portal_endpoint", False)
    try:
        token = access_token if access_token is not None else await get_valid_token(portal, db)
    except httpx.RequestError:
        raise CrmBitrixError("token_refresh_unavailable", True) from None
    except httpx.HTTPStatusError as exc:
        raise CrmBitrixError("token_refresh_http_error", exc.response.status_code >= 500 or exc.response.status_code == 429) from None
    except (ValueError, KeyError):
        raise CrmBitrixError("token_refresh_invalid_response", True) from None
    except RuntimeError:
        raise CrmBitrixError("authorization_unavailable", False) from None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                portal.client_endpoint.rstrip("/") + "/" + method,
                json={**(params or {}), "auth": token}, timeout=10,
            )
    except httpx.RequestError:
        raise CrmBitrixError("bitrix_transport_error", True) from None
    try:
        data = response.json()
    except ValueError:
        raise CrmBitrixError("bitrix_invalid_response", True) from None
    error = data.get("error") if isinstance(data, dict) else None
    if error:
        safe_code = error if isinstance(error, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", error) else "bitrix_api_error"
        retryable = safe_code in {
            "QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT", "OVERLOAD_LIMIT", "INTERNAL_SERVER_ERROR",
            "ERROR_UNEXPECTED_ANSWER", "ERROR_MESSAGE_NOT_FOUND",
        } or response.status_code >= 500 or response.status_code == 429
        raise CrmBitrixError(safe_code, retryable)
    if not response.is_success or not isinstance(data, dict) or "result" not in data:
        raise CrmBitrixError("bitrix_unsuccessful_response", response.status_code >= 500 or response.status_code == 429)
    return data["result"]


async def update_message_status(portal, db, code: str, message_id: str, status: str) -> None:
    result = await call_crm_method(portal, db, "messageservice.message.status.update",
                                   {"CODE": code, "MESSAGE_ID": message_id, "STATUS": status})
    if result is not True:
        raise CrmBitrixError("bitrix_unsuccessful_response", False)
