"""WHATSAPP_MESSENGER text adapter.

API assumptions WA-A01..A06: docs/WHATSAPP_MESSENGER_PROGRESS.md.
Independent from Telegram so its accepted behavior remains unchanged.
"""
import hashlib
import logging
import re
import sys
from urllib.parse import urlsplit

import httpx
from app.config import settings
from app.services.token import get_valid_token
from app.services.log_paths import payload_for_log

logger = logging.getLogger(__name__)
CHANNEL_TYPE = "WHATSAPP_MESSENGER"
CONNECTOR = "edna_whatsapp_messenger"
# WA-A01: exact path confirmed by owner on 2026-11-20; live delivery pending.
API_URL = "https://app.edna.ru/api/v1/out-messages/wa-messenger"
# WA-A04: application limit copied from Telegram, not an edna guarantee.
MAX_TEXT_LENGTH = 4096
RECEIVE, SEND, STATUS = "wa_messenger_receive", "wa_messenger_send", "wa_messenger_status"
NOTICE, RENAME = "wa_messenger_notice", "wa_messenger_rename"
TASK_TYPES = frozenset({RECEIVE, SEND, STATUS, NOTICE, RENAME})
FAILURES = frozenset({"FAILED", "UNDELIVERED", "CANCELLED", "EXPIRED"})


def log_payload(direction, data, channel_id=None):
    logger.info("WhatsApp %s payload (channel=%s): %s", direction, channel_id, payload_for_log(data))


class WhatsAppError(RuntimeError):
    """Only safe machine codes, never remote response bodies or credentials."""
    def __init__(self, code, *, retryable=False, uncertain=False, operation=None, remote_code=None):
        self.retryable, self.uncertain = retryable, uncertain
        self.code, self.operation, self.remote_code = code, operation, remote_code
        super().__init__(code)


def log_exception(message, *args):
    # Preserve the traceback, replace SQL/HTTP exception text that may contain binds.
    trace = sys.exc_info()[2]
    safe = WhatsAppError("whatsapp_operation_error")
    logger.exception(message, *args, exc_info=(WhatsAppError, safe, trace))


def scalar(value, *, maximum=128):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise WhatsAppError("invalid_identifier")
    value = str(value)
    if not value or len(value) > maximum:
        raise WhatsAppError("invalid_identifier")
    return value


def check_subject(channel, data):
    # A missing optional field is allowed; a contradictory one never is.
    if "subject" in data and data["subject"] != channel.sender:
        raise WhatsAppError("subject_mismatch")
    if "subjectId" in data and str(data["subjectId"]) != str(channel.subject_id):
        raise WhatsAppError("subject_mismatch")


async def bitrix_call(portal, db, method, params):
    endpoint = urlsplit(portal.client_endpoint or "")
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise WhatsAppError("invalid_portal_endpoint")
    try:
        token = await get_valid_token(portal, db)
        async with httpx.AsyncClient() as client:
            response = await client.post(portal.client_endpoint.rstrip("/") + "/" + method,
                                         json={**params, "auth": token}, timeout=10)
    except httpx.HTTPStatusError as exc:
        raise WhatsAppError("bitrix_authorization_http", retryable=exc.response.status_code >= 500 or exc.response.status_code == 429) from None
    except httpx.HTTPError:
        raise WhatsAppError("bitrix_transport_error", retryable=True) from None
    except RuntimeError:
        raise WhatsAppError("bitrix_authorization_unavailable") from None
    try:
        data = response.json()
    except ValueError:
        raise WhatsAppError("bitrix_invalid_response", retryable=True) from None
    result = data.get("result") if isinstance(data, dict) else None
    setup = method in {"imconnector.register", "imconnector.activate"}
    registration = method == "imconnector.register" and isinstance(result, dict)
    remote_error = data.get("error") if isinstance(data, dict) else None
    if registration:
        remote_error = remote_error or result.get("error")
    safe_error = remote_error if isinstance(remote_error, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", remote_error) else "unknown"
    if method == "imconnector.send.status.delivery":
        logger.info("WhatsApp Bitrix READ response: HTTP=%s SUCCESS=%s error_code=%s",
                    response.status_code, result.get("SUCCESS") is True if isinstance(result, dict) else result is True,
                    safe_error if remote_error else "none")
    if method == "imconnector.chat.name.set":
        logger.info("WhatsApp Bitrix chat name response: HTTP=%s SUCCESS=%s error_code=%s",
                    response.status_code, result.get("SUCCESS") is True if isinstance(result, dict) else False,
                    safe_error if remote_error else "none")
    if setup:
        # register wraps its boolean inside result.result; activate returns a
        # direct boolean. Preserve legacy direct true for register as well.
        confirmed = result.get("result") is True if registration else result is True
        confirmed = confirmed and not remote_error and response.is_success
        logger.info("WhatsApp Bitrix setup: method=%s HTTP=%s confirmed=%s result_type=%s error_code=%s",
                    method, response.status_code, confirmed, type(result).__name__, safe_error if remote_error else "none")
        if not isinstance(data, dict) or not confirmed:
            raise WhatsAppError("bitrix_setup_not_confirmed", operation=method, remote_code=safe_error,
                                retryable=response.status_code >= 500 or response.status_code == 429)
        return True
    if not response.is_success or not isinstance(data, dict) or data.get("error") or not result:
        raise WhatsAppError("bitrix_api_error", retryable=response.status_code >= 500 or response.status_code == 429)
    if isinstance(result, dict):
        if result.get("SUCCESS") is not True:
            raise WhatsAppError("bitrix_message_not_accepted", retryable=True)
        details = result.get("DATA", {}).get("RESULT", []) if isinstance(result.get("DATA"), dict) else []
        if any(not isinstance(item, dict) or item.get("SUCCESS") is not True for item in details):
            raise WhatsAppError("bitrix_message_not_accepted", retryable=True)
    return result


async def prepare_connector(portal, db, line_id=None):
    await bitrix_call(portal, db, "imconnector.register", {
        # Group mode bypasses the incoming CRM tracker. WhatsApp customers
        # use one-to-one chats so the line can create/link CRM entities.
        "ID": CONNECTOR, "NAME": "WhatsApp Messenger от edna", "CHAT_GROUP": False,
        "NEED_SYSTEM_MESSAGES": False, "NEWSLETTER": False,
        "EDIT_INTERNAL_MESSAGES": False, "DEL_INTERNAL_MESSAGES": False,
        "ICON": {"DATA_IMAGE": "data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2048%2048'%3E%3Ccircle%20cx='24'%20cy='24'%20r='24'%20fill='%2325d366'/%3E%3Ctext%20x='9'%20y='32'%20font-size='22'%20fill='white'%3EW%3C/text%3E%3C/svg%3E", "COLOR": "#25d366"},
        "PLACEMENT_HANDLER": settings.app_base_url.rstrip("/") + "/settings",
    })
    selected_line = line_id or portal.open_line_id
    if selected_line:
        await bitrix_call(portal, db, "imconnector.activate",
                          {"CONNECTOR": CONNECTOR, "LINE": int(selected_line), "ACTIVE": "1"})


async def send_content(api_key, sender, address, content, out_message_id, *, address_type="WHATSAPP_MESSENGER_ID"):
    if address_type == "WHATSAPP_MESSENGER_ID":
        address = messenger_id(address)
    elif address_type != "PHONE" or not isinstance(address, str) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", address, flags=re.ASCII):
        raise WhatsAppError("invalid_phone")
    payload = {"from": sender, "to": {"type": address_type, "value": address},
               "outMessageId": out_message_id, "content": content}
    log_payload("outgoing request", payload)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(API_URL, headers={"X-API-KEY": api_key}, json=payload, timeout=15)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise WhatsAppError("edna_not_connected", retryable=True) from None
    except httpx.HTTPError:
        raise WhatsAppError("edna_outcome_unknown", uncertain=True) from None
    logger.info("WhatsApp send response: request_id=%s HTTP=%s", out_message_id, response.status_code)
    # WA-A05: 429 is treated as an explicit rejection, as in Telegram.
    # Never retry a POST possibly accepted upstream without an edna guarantee.
    if response.status_code == 429:
        raise WhatsAppError("edna_rate_limited", retryable=True)
    if response.status_code >= 500 or response.status_code == 408:
        raise WhatsAppError("edna_outcome_unknown", uncertain=True)
    if not response.is_success:
        raise WhatsAppError("edna_send_rejected")
    try:
        result = response.json()
        log_payload("outgoing response", result)
    except ValueError:
        raise WhatsAppError("edna_invalid_response", uncertain=True) from None
    if not isinstance(result, dict) or result.get("outMessageId") != out_message_id:
        raise WhatsAppError("edna_response_id_mismatch", uncertain=True)
    recipient_field = "whatsappMessengerId" if address_type == "WHATSAPP_MESSENGER_ID" else "phone"
    recipient = result.get(recipient_field)
    if recipient is not None and (not isinstance(recipient, (str, int)) or isinstance(recipient, bool)
                                  or str(recipient) != address):
        raise WhatsAppError("edna_recipient_mismatch", uncertain=True)
    return result


def is_whatsapp(channel):
    return (channel.channel_type or "").upper() == CHANNEL_TYPE


def messenger_id(value):
    # WA-A03: treat WhatsApp IDs as opaque strings, never as a phone or MAX ID.
    value = scalar(value)
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise WhatsAppError("invalid_whatsapp_id")
    return value


def incoming_identity(data):
    # WA-A02: assumed Telegram-shaped webhook; require an explicit typed ID.
    subscriber = data.get("subscriber")
    identities = subscriber.get("identifiers") if isinstance(subscriber, dict) else None
    if not isinstance(identities, list):
        raise WhatsAppError("whatsapp_identifier_type_required")
    addresses = {messenger_id(i.get("value")) for i in identities
                 if isinstance(i, dict) and i.get("type") == "WHATSAPP_MESSENGER_ID"}
    if len(addresses) != 1:
        raise WhatsAppError("whatsapp_identifier_required")
    phones = set()
    for item in identities:
        if isinstance(item, dict) and item.get("type") == "PHONE":
            value = item.get("value")
            if isinstance(value, str) and re.fullmatch(r"\+?[1-9][0-9]{1,14}", value, flags=re.ASCII):
                phones.add("+" + value.lstrip("+"))
    if len(phones) > 1:
        raise WhatsAppError("ambiguous_phone")
    return addresses.pop(), next(iter(phones), None)


def chat_id(channel, address):
    sender_key = hashlib.sha256(channel.sender.encode("utf-8")).hexdigest()[:32]
    return "wa:%s:%s" % (sender_key, messenger_id(address))


def user_id(channel, address):
    # CHAT_GROUP=False groups by user.id. Isolate different company senders too.
    return chat_id(channel, address)


async def send_text(api_key, sender, address, text, out_message_id):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LENGTH:
        raise WhatsAppError("invalid_text")
    return await send_content(api_key, sender, address, {"type": "TEXT", "text": text}, out_message_id)
