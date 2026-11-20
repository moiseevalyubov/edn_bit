"""Explicit Telegram contract. Incoming fixtures require live edna validation."""
import hashlib
from decimal import Decimal, InvalidOperation
import logging
import re
import sys
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.services.token import get_valid_token
from app.services.log_paths import payload_for_log

logger = logging.getLogger(__name__)


def log_payload(direction, data, channel_id=None):
    logger.info("Telegram %s payload (channel=%s): %s", direction, channel_id, payload_for_log(data))

CONNECTOR = "edna_telegram"
API_URL = "https://app.edna.ru/api/v1/out-messages/telegram"
RECEIVE = "telegram_receive"
SEND = "telegram_send"
STATUS = "telegram_status"
NOTICE = "telegram_notice"
RENAME = "telegram_rename"
TASK_TYPES = frozenset({RECEIVE, SEND, STATUS, NOTICE, RENAME})
FAILURES = frozenset({"FAILED", "UNDELIVERED", "CANCELLED", "EXPIRED"})


def location_text(location):
    """Same Yandex Maps presentation as MAX; only validated numeric coordinates."""
    if not isinstance(location, dict):
        return None
    coordinates = []
    for field, limit in (("latitude", 90), ("longitude", 180)):
        value = location.get(field)
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        raw = str(value)
        if len(raw) > 64 or (isinstance(value, str) and not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", raw)):
            return None
        try:
            number = Decimal(raw)
        except InvalidOperation:
            return None
        if not number.is_finite() or not -limit <= number <= limit:
            return None
        coordinate = format(number, "f")
        coordinates.append(coordinate.rstrip("0").rstrip(".") if "." in coordinate else coordinate)
    latitude, longitude = coordinates
    url = "https://yandex.ru/maps/?pt=%s,%s&z=16&l=map" % (longitude, latitude)
    from app.services.sanitize import sanitize_text
    address = location.get("address")
    address = sanitize_text(address).replace("[", "［").replace("]", "］") if isinstance(address, str) else ""
    return "📍 %s\n%s" % (address, url) if address else "📍 Местоположение: " + url


class TelegramError(RuntimeError):
    """Only safe machine codes, never remote response bodies or credentials."""
    def __init__(self, code, *, retryable=False, uncertain=False, operation=None, remote_code=None):
        self.retryable, self.uncertain = retryable, uncertain
        self.code, self.operation, self.remote_code = code, operation, remote_code
        super().__init__(code)


def log_exception(message, *args):
    # Preserve the traceback, replace SQL/HTTP exception text that may contain binds.
    trace = sys.exc_info()[2]
    safe = TelegramError("telegram_operation_error")
    logger.exception(message, *args, exc_info=(TelegramError, safe, trace))


def is_telegram(channel):
    return (channel.channel_type or "").upper() == "TELEGRAM"


def scalar(value, *, maximum=128):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TelegramError("invalid_identifier")
    value = str(value)
    if not value or len(value) > maximum:
        raise TelegramError("invalid_identifier")
    return value


def telegram_id(value):
    value = scalar(value, maximum=32)
    if not re.fullmatch(r"[1-9][0-9]{0,31}", value, flags=re.ASCII):
        raise TelegramError("invalid_telegram_id")
    return value


def check_subject(channel, data):
    # A missing optional field is allowed; a contradictory one never is.
    if "subject" in data and data["subject"] != channel.sender:
        raise TelegramError("subject_mismatch")
    if "subjectId" in data and str(data["subjectId"]) != str(channel.subject_id):
        raise TelegramError("subject_mismatch")


def incoming_identity(data):
    subscriber = data.get("subscriber")
    if not isinstance(subscriber, dict):
        raise TelegramError("subscriber_required")
    identities = subscriber.get("identifiers")
    # subscriber.id is an edna record id, NOT a Telegram recipient address.
    # No guessed MAX_ID or TELEGRAM_ID fallback on an unverified webhook schema.
    if not isinstance(identities, list):
        raise TelegramError("telegram_identifier_type_required")
    addresses = {telegram_id(i.get("value")) for i in identities
                 if isinstance(i, dict) and i.get("type") == "TELEGRAM_ID"}
    if len(addresses) != 1:
        raise TelegramError("telegram_identifier_required")
    address = addresses.pop()
    phone = None
    for item in identities:
        if isinstance(item, dict) and item.get("type") == "PHONE":
            candidate = str(item.get("value") or "")
            if re.fullmatch(r"\+?[1-9][0-9]{1,14}", candidate, flags=re.ASCII):
                candidate = "+" + candidate.lstrip("+")
                if phone and candidate != phone:
                    raise TelegramError("ambiguous_phone")
                phone = candidate
    return address, phone


def incoming_username(data):
    """Optional display label, never an address or a cross-channel identity."""
    subscriber = data.get("subscriber")
    identifiers = subscriber.get("identifiers") if isinstance(subscriber, dict) else None
    if not isinstance(identifiers, list):
        return None
    usernames = set()
    for item in identifiers:
        if not isinstance(item, dict) or item.get("type") != "TELEGRAM_USERNAME" or not isinstance(item.get("value"), str):
            continue
        value = item["value"].strip().removeprefix("@")
        if value and len(value) <= 254 and re.fullmatch(r"[A-Za-z0-9_]+", value, flags=re.ASCII):
            usernames.add(value)
    return usernames.pop() if len(usernames) == 1 else None


def chat_id(channel, address):
    # Stable across rename/reconnect; punctuation in sender cannot confuse parsing.
    sender_key = hashlib.sha256(channel.sender.encode("utf-8")).hexdigest()[:32]
    return "tg:%s:%s" % (sender_key, telegram_id(address))


async def bitrix_call(portal, db, method, params):
    endpoint = urlsplit(portal.client_endpoint or "")
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise TelegramError("invalid_portal_endpoint")
    try:
        token = await get_valid_token(portal, db)
        async with httpx.AsyncClient() as client:
            response = await client.post(portal.client_endpoint.rstrip("/") + "/" + method,
                                         json={**params, "auth": token}, timeout=10)
    except httpx.HTTPStatusError as exc:
        raise TelegramError("bitrix_authorization_http", retryable=exc.response.status_code >= 500 or exc.response.status_code == 429) from None
    except httpx.HTTPError:
        raise TelegramError("bitrix_transport_error", retryable=True) from None
    except RuntimeError:
        raise TelegramError("bitrix_authorization_unavailable") from None
    try:
        data = response.json()
    except ValueError:
        raise TelegramError("bitrix_invalid_response", retryable=True) from None
    result = data.get("result") if isinstance(data, dict) else None
    setup = method in {"imconnector.register", "imconnector.activate"}
    registration = method == "imconnector.register" and isinstance(result, dict)
    remote_error = data.get("error") if isinstance(data, dict) else None
    if registration:
        remote_error = remote_error or result.get("error")
    safe_error = remote_error if isinstance(remote_error, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", remote_error) else "unknown"
    if method == "imconnector.send.status.delivery":
        logger.info("Telegram Bitrix READ response: HTTP=%s SUCCESS=%s error_code=%s",
                    response.status_code, result.get("SUCCESS") is True if isinstance(result, dict) else result is True,
                    safe_error if remote_error else "none")
    if method == "imconnector.chat.name.set":
        logger.info("Telegram Bitrix chat name response: HTTP=%s SUCCESS=%s error_code=%s",
                    response.status_code, result.get("SUCCESS") is True if isinstance(result, dict) else False,
                    safe_error if remote_error else "none")
    if setup:
        # register wraps its boolean inside result.result; activate returns a
        # direct boolean. Preserve legacy direct true for register as well.
        confirmed = result.get("result") is True if registration else result is True
        confirmed = confirmed and not remote_error and response.is_success
        logger.info("Telegram Bitrix setup: method=%s HTTP=%s confirmed=%s result_type=%s error_code=%s",
                    method, response.status_code, confirmed, type(result).__name__, safe_error if remote_error else "none")
        if not isinstance(data, dict) or not confirmed:
            raise TelegramError("bitrix_setup_not_confirmed", operation=method, remote_code=safe_error,
                                retryable=response.status_code >= 500 or response.status_code == 429)
        return True
    if not response.is_success or not isinstance(data, dict) or data.get("error") or not result:
        raise TelegramError("bitrix_api_error", retryable=response.status_code >= 500 or response.status_code == 429)
    if isinstance(result, dict):
        if result.get("SUCCESS") is not True:
            raise TelegramError("bitrix_message_not_accepted", retryable=True)
        details = result.get("DATA", {}).get("RESULT", []) if isinstance(result.get("DATA"), dict) else []
        if any(not isinstance(item, dict) or item.get("SUCCESS") is not True for item in details):
            raise TelegramError("bitrix_message_not_accepted", retryable=True)
    return result


async def prepare_connector(portal, db, line_id=None):
    if not settings.telegram_enabled:
        return
    await bitrix_call(portal, db, "imconnector.register", {
        "ID": CONNECTOR, "NAME": "Telegram от edna", "CHAT_GROUP": True,
        "NEED_SYSTEM_MESSAGES": False, "NEWSLETTER": False,
        "EDIT_INTERNAL_MESSAGES": False, "DEL_INTERNAL_MESSAGES": False,
        "ICON": {"DATA_IMAGE": "data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2048%2048'%3E%3Ccircle%20cx='24'%20cy='24'%20r='24'%20fill='%2329a9eb'/%3E%3Cpath%20d='M10%2023L37%2012L29%2036L22%2027Z'%20fill='white'/%3E%3C/svg%3E", "COLOR": "#29a9eb"},
        "PLACEMENT_HANDLER": settings.app_base_url.rstrip("/") + "/settings",
    })
    selected_line = line_id or portal.open_line_id
    if selected_line:
        await bitrix_call(portal, db, "imconnector.activate",
                          {"CONNECTOR": CONNECTOR, "LINE": int(selected_line), "ACTIVE": "1"})


async def send_text(api_key, sender, address, text, out_message_id):
    if not isinstance(text, str) or not text.strip():
        raise TelegramError("empty_text")
    return await send_content(api_key, sender, address, {"type": "TEXT", "text": text}, out_message_id)


async def send_media(api_key, sender, address, media_type, url, name, out_message_id, *, text=None):
    if media_type not in {"IMAGE", "DOCUMENT"}:
        raise TelegramError("unsupported_media_type")
    endpoint = urlsplit(url)
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.fragment or len(url) > 1000):
        raise TelegramError("invalid_media_url")
    if not isinstance(name, str) or not name or len(name) > 25 or any(ord(c) < 32 for c in name):
        raise TelegramError("invalid_media_name")
    content = {"type": media_type, "url": url, "name": name}
    if text:
        # Same contract as MAX. Telegram caption delivery requires a live check.
        if not isinstance(text, str) or len(text) > 4096:
            raise TelegramError("invalid_media_text")
        content["text"] = text
    return await send_content(api_key, sender, address, content, out_message_id)


async def send_content(api_key, sender, address, content, out_message_id):
    telegram_id(address)
    payload = {"from": sender, "to": {"type": "TELEGRAM_ID", "value": address},
               "outMessageId": out_message_id, "content": content}
    log_payload("outgoing request", payload)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(API_URL, headers={"X-API-KEY": api_key}, json=payload, timeout=15)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise TelegramError("edna_not_connected", retryable=True) from None
    except httpx.HTTPError:
        raise TelegramError("edna_outcome_unknown", uncertain=True) from None
    logger.info("Telegram send response: request_id=%s HTTP=%s", out_message_id, response.status_code)
    # No retry of a POST possibly accepted upstream. 429 is an explicit rejection.
    if response.status_code == 429:
        raise TelegramError("edna_rate_limited", retryable=True)
    if response.status_code >= 500 or response.status_code == 408:
        raise TelegramError("edna_outcome_unknown", uncertain=True)
    if not response.is_success:
        raise TelegramError("edna_send_rejected")
    try:
        result = response.json()
        log_payload("outgoing response", result)
    except ValueError:
        raise TelegramError("edna_invalid_response", uncertain=True) from None
    if not isinstance(result, dict) or result.get("outMessageId") != out_message_id:
        raise TelegramError("edna_response_id_mismatch", uncertain=True)
    if result.get("telegramId") is not None and str(result["telegramId"]) != address:
        raise TelegramError("edna_recipient_mismatch", uncertain=True)
    return result
