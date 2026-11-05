"""CRM receipt and delivery facts. Callers own the transaction/commit."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import re
import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Channel, CrmMessageProvider, CrmMessageStatusEvent, CrmOutgoingMessage, MessageDeliveryTask, Portal
from app.services.task_types import SEND_CRM_TO_EDNA, UPDATE_CRM_STATUS

logger = logging.getLogger(__name__)
MAX_TEXT_LENGTH = 4000


class CrmRequestError(Exception):
    def __init__(self, code: str, status_code: int):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _string(value, limit: int = 255) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise CrmRequestError("invalid_request", 400)
    return value


def normalize_phone(value: str) -> str:
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("invalid_phone")
    value = re.sub(r"[\s()\-]", "", value)
    if not re.fullmatch(r"\+?[1-9][0-9]{6,14}", value, flags=re.ASCII):
        raise ValueError("invalid_phone")
    return "+" + value.lstrip("+")


def enqueue_crm_operation(db: Session, message: CrmOutgoingMessage, kind: str) -> None:
    """Use an operation key, not a transient body/auth fingerprint. No commit."""
    if kind not in {SEND_CRM_TO_EDNA, UPDATE_CRM_STATUS}:
        raise ValueError("Unsupported CRM message operation")
    db.flush()
    revision = message.desired_revision if kind == UPDATE_CRM_STATUS else 0
    key = f"crm:{kind}:{message.id}:{revision}"
    values = dict(
        channel_id=message.channel_id, task_type=kind, direction="outgoing",
        payload=json.dumps({"crm_message_id": message.id, "revision": revision}),
        dedup_key=key, status="pending",
        # Bitrix invokes the handler before its own message transaction finishes.
        next_attempt_at=datetime.utcnow() + timedelta(seconds=3 if kind == UPDATE_CRM_STATUS else 0),
    )
    dialect = db.get_bind().dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert if dialect == "sqlite" else None
    if insert is None:
        raise RuntimeError("Unsupported CRM queue database")
    db.execute(insert(MessageDeliveryTask).values(**values).on_conflict_do_nothing())


def set_desired_status(db: Session, message: CrmOutgoingMessage, status: str) -> None:
    if message.desired_bitrix_status != status:
        message.desired_bitrix_status = status
        message.desired_revision += 1
    message.updated_at = datetime.utcnow()
    enqueue_crm_operation(db, message, UPDATE_CRM_STATUS)


def accept_crm_message(db: Session, data: dict) -> CrmOutgoingMessage:
    """Authenticate, deduplicate and stage receipt + queue in one transaction."""
    auth = data.get("auth")
    if not isinstance(auth, dict):
        raise CrmRequestError("forbidden", 403)
    member_id = _string(auth.get("member_id"))
    token = auth.get("application_token")
    portal = db.query(Portal).filter_by(member_id=member_id).first()
    if not (portal and portal.app_token and isinstance(token, str) and len(token) <= 4096
            and hmac.compare_digest(portal.app_token.encode(), token.encode())
            and portal.uninstalled_at is None):
        raise CrmRequestError("forbidden", 403)
    if data.get("module_id") != "crm" or data.get("type") != "SMS":
        raise CrmRequestError("unsupported_source", 400)
    code, message_id = _string(data.get("code")), _string(data.get("message_id"))
    provider = db.query(CrmMessageProvider).filter_by(portal_id=portal.id, provider_code=code).with_for_update().first()
    if provider is None:
        raise CrmRequestError("unknown_provider", 403)
    channel = db.query(Channel).filter_by(id=provider.channel_id, portal_id=portal.id).first()
    if channel is None:
        raise CrmRequestError("unknown_provider", 403)

    phone = data.get("message_to")
    text = data.get("message_body")
    if not isinstance(phone, str) or len(phone) > 100 or not isinstance(text, str) or len(text) > 16000:
        raise CrmRequestError("invalid_content", 400)
    invalid = None
    try:
        phone = normalize_phone(phone)
    except ValueError:
        invalid = "invalid_phone"
    if not text.strip() or len(text) > MAX_TEXT_LENGTH:
        invalid = "invalid_text"

    existing = db.query(CrmOutgoingMessage).filter_by(
        portal_id=portal.id, provider_code=code, bitrix_message_id=message_id,
    ).first()
    if existing is not None:
        if existing.recipient_phone != phone or existing.text != text:
            raise CrmRequestError("message_id_conflict", 409)
        return existing

    if not settings.crm_outreach_enabled or not portal.crm_outreach_enabled:
        raise CrmRequestError("feature_disabled", 404)
    if not channel.is_active or provider.desired_state != "present":
        invalid = "channel_disabled"
    elif (channel.channel_type or "").upper() != "MAX":
        invalid = "unsupported_channel"
    elif provider.applied_revision < 1:
        invalid = "provider_not_ready"

    bindings = data.get("bindings", [])
    if not isinstance(bindings, list) or len(bindings) > 32:
        raise CrmRequestError("invalid_bindings", 400)
    safe_bindings = []
    for binding in bindings:
        if not isinstance(binding, dict):
            raise CrmRequestError("invalid_bindings", 400)
        clean = {}
        for key in ("OWNER_TYPE_ID", "OWNER_ID"):
            value = str(binding.get(key, ""))
            if not re.fullmatch(r"[1-9][0-9]{0,18}", value, flags=re.ASCII):
                raise CrmRequestError("invalid_bindings", 400)
            clean[key] = value
        safe_bindings.append(clean)
    actor = str(auth.get("user_id", ""))
    message = CrmOutgoingMessage(
        portal_id=portal.id, channel_id=channel.id, provider_code=code,
        bitrix_message_id=message_id, text=text, recipient_phone=phone,
        edna_out_message_id=uuid.uuid4().hex, bindings=json.dumps(safe_bindings),
        actor_user_id=actor if re.fullmatch(r"[1-9][0-9]{0,18}", actor, flags=re.ASCII) else None,
        send_state="failed" if invalid else "queued", last_error=invalid,
        desired_bitrix_status="failed" if invalid else "queued", desired_revision=1,
    )
    db.add(message)
    db.flush()
    if invalid is None:
        enqueue_crm_operation(db, message, SEND_CRM_TO_EDNA)
    enqueue_crm_operation(db, message, UPDATE_CRM_STATUS)
    logger.info("CRM message %s received (channel=%s, state=%s)", message.id, channel.id, message.send_state)
    return message


def record_edna_status(db: Session, channel: Channel, data: dict) -> bool:
    """Return False for legacy/unrelated IDs. Store recognized CRM facts durably."""
    request_id = data.get("requestId")
    if not isinstance(request_id, str) or len(request_id) > 256:
        return False
    message = db.query(CrmOutgoingMessage).filter_by(
        channel_id=channel.id, portal_id=channel.portal_id, edna_out_message_id=request_id,
    ).with_for_update().first()
    if message is None:
        return False
    if data.get("subject") is not None and data["subject"] != channel.sender:
        raise CrmRequestError("status_channel_mismatch", 403)
    if data.get("subjectId") is not None and channel.subject_id is not None and str(data["subjectId"]) != str(channel.subject_id):
        raise CrmRequestError("status_channel_mismatch", 403)
    status = data.get("status")
    if not isinstance(status, str) or not re.fullmatch(r"[A-Za-z_]{1,64}", status, flags=re.ASCII):
        raise CrmRequestError("invalid_status", 400)
    status = status.upper()
    raw_date = data.get("statusAt")
    status_at = None
    if isinstance(raw_date, str) and len(raw_date) <= 64:
        try:
            parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                status_at = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            pass
    error = data.get("error")
    safe_error = error if isinstance(error, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", error, flags=re.ASCII) else None
    fingerprint = hashlib.sha256(json.dumps([request_id, status, str(status_at), safe_error]).encode()).hexdigest()
    if db.query(CrmMessageStatusEvent.id).filter_by(crm_message_id=message.id, fingerprint=fingerprint).first():
        return True
    db.add(CrmMessageStatusEvent(crm_message_id=message.id, status=status, status_at=status_at,
                                 request_id=request_id, error=safe_error, fingerprint=fingerprint))
    at = status_at or datetime.utcnow()
    desired = None
    if status == "READ":
        message.read_at = message.read_at or at
        message.delivered_at = message.delivered_at or at
        message.send_state = "read"
        desired = "delivered"
    elif status == "DELIVERED" and not message.read_at:
        message.delivered_at = message.delivered_at or at
        message.send_state = "delivered"
        desired = "delivered"
    elif status == "SENT" and message.send_state in {"queued", "sending", "uncertain", "accepted", "sent"}:
        message.sent_at = message.sent_at or at
        message.send_state = "sent"
        desired = "sent"
    elif status in {"FAILED", "UNDELIVERED", "CANCELLED", "EXPIRED"}:
        if message.delivered_at or message.read_at:
            logger.warning("Conflicting CRM status %s for message %s; delivery fact preserved", status, message.id)
        else:
            message.send_state = "failed"
            message.last_error = safe_error or status
            desired = "failed" if status == "FAILED" else "undelivered"
    if desired is not None:
        message.last_edna_status = status
        set_desired_status(db, message, desired)
    logger.info("CRM status %s recorded for message %s", status, message.id)
    return True
