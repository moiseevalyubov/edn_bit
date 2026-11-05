"""CRM operations: durable send attempt, conservative retries, separate statuses."""
from datetime import datetime, timedelta
import json
import logging

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.models import Channel, CrmMessageProvider, CrmOutgoingMessage, MessageDeliveryTask, Portal
from app.services.crm_bitrix import CrmBitrixError, update_message_status
from app.services.crm_outreach import set_desired_status
from app.services.crm_locks import operation_lock as _status_lock
from app.services.maxbot import send_message_max
from app.services.task_types import SEND_CRM_TO_EDNA, UPDATE_CRM_STATUS

logger = logging.getLogger(__name__)
RETRY_SCHEDULE = (1, 5, 30, 120, 600, 1800)


def _retry(task, code: str) -> bool:
    task.retry_count += 1
    task.last_error = code
    task.updated_at = datetime.utcnow()
    if task.retry_count > len(RETRY_SCHEDULE):
        task.status = "dead"
        return False
    task.status = "pending"
    task.next_attempt_at = datetime.utcnow() + timedelta(seconds=RETRY_SCHEDULE[task.retry_count - 1])
    return True


def _load(db, task_id, *, lock_message=True):
    task = db.get(MessageDeliveryTask, task_id)
    if task is None or task.status != "processing":
        return task, None
    try:
        payload = json.loads(task.payload)
        message_id = payload.get("crm_message_id") if isinstance(payload, dict) else None
        if type(message_id) is not int or message_id < 1:
            raise ValueError("Invalid CRM message ID")
    except (ValueError, TypeError):
        task.status, task.last_error = "dead", "invalid_crm_task"
        return task, None
    query = db.query(CrmOutgoingMessage).filter_by(id=message_id, channel_id=task.channel_id)
    message = (query.with_for_update() if lock_message else query).first()
    if message is None:
        task.status, task.last_error = "dead", "crm_message_missing"
    return task, message


async def execute_crm_task(task_id: int, task_type: str, session_factory) -> None:
    # Outside the legacy retry handler: a DB failure after HTTP must never cause
    # an untracked second POST. Stale recovery will mark that attempt uncertain.
    try:
        if task_type == SEND_CRM_TO_EDNA:
            await _send(task_id, session_factory)
        elif task_type == UPDATE_CRM_STATUS:
            await _status(task_id, session_factory)
        else:
            raise ValueError("Unsupported CRM executor")
    except SQLAlchemyError:
        # The outer worker logs exceptions. Suppress SQL and bound personal data.
        raise RuntimeError("CRM storage operation failed; stale recovery required") from None


async def _send(task_id, session_factory):
    with session_factory() as db:
        task, message = _load(db, task_id, lock_message=False)
        if message is None:
            db.commit()
            return
        # Channel before message: matches disconnect/cancellation lock order.
        channel = db.query(Channel).filter_by(id=message.channel_id).with_for_update().first()
        message = db.query(CrmOutgoingMessage).filter_by(id=message.id).populate_existing().with_for_update().one()
        if message.send_state in {"accepted", "sent", "delivered", "read"}:
            task.status = "sent"
            db.commit()
            return
        if message.send_state in {"failed", "cancelled"}:
            task.status = "failed"
            db.commit()
            return
        if message.send_state != "queued" or message.attempt_started_at is not None:
            task.status, message.send_state = "uncertain", "uncertain"
            task.last_error = message.last_error = "send_outcome_unknown"
            db.commit()
            return
        portal = db.get(Portal, message.portal_id)
        provider = db.query(CrmMessageProvider).filter_by(channel_id=message.channel_id, portal_id=message.portal_id).first()
        if not settings.crm_outreach_enabled or (portal and not portal.crm_outreach_enabled):
            task.status = "pending"
            task.next_attempt_at = datetime.utcnow() + timedelta(seconds=30)
            db.commit()
            return
        if not (portal and portal.uninstalled_at is None and channel and channel.portal_id == portal.id
                and channel.is_active and (channel.channel_type or "").upper() == "MAX"
                and provider and provider.desired_state == "present" and provider.provider_code == message.provider_code):
            task.status, message.send_state = "failed", "failed"
            task.last_error = message.last_error = "channel_unavailable"
            set_desired_status(db, message, "failed")
            db.commit()
            return
        # All parameters are from the authenticated, persisted channel/receipt.
        args = dict(api_key=channel.api_key, sender=channel.sender, to_value=message.recipient_phone,
                    to_type="PHONE", text=message.text, out_message_id=message.edna_out_message_id)
        message.send_state = "sending"
        message.attempt_started_at = datetime.utcnow()
        message.updated_at = datetime.utcnow()
        db.commit()  # Must succeed BEFORE issuing an HTTP request.

    outcome, error = "accepted", None
    try:
        result = await send_message_max(**args)
        if not isinstance(result, dict) or result.get("outMessageId") != args["out_message_id"]:
            outcome, error = "uncertain", "edna_response_id_mismatch"
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        # No HTTP request body has been transmitted in these cases.
        outcome, error = "retry", "edna_connect_unavailable"
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if 400 <= status < 500 and status not in {408, 429}:
            outcome, error = "failed", "edna_request_rejected"
        else:
            # A gateway response does not prove that the upstream did not send.
            outcome, error = "uncertain", "edna_http_outcome_unknown"
    except (httpx.RequestError, ValueError, TypeError):
        outcome, error = "uncertain", "edna_send_outcome_unknown"

    with session_factory() as db:
        task, message = _load(db, task_id)
        if message is None:
            db.commit()
            return
        # A real status can arrive before the sending HTTP response.
        if message.send_state in {"sent", "delivered", "read", "failed"}:
            task.status = "failed" if message.send_state == "failed" else "sent"
        elif outcome == "retry":
            if _retry(task, error):
                message.send_state, message.attempt_started_at = "queued", None
            else:
                message.send_state = "failed"
                set_desired_status(db, message, "failed")
        elif outcome == "accepted":
            message.send_state, message.accepted_at = "accepted", datetime.utcnow()
            task.status = "sent"
            # HTTP acceptance alone is neither SENT nor DELIVERED.
        else:
            task.status = message.send_state = outcome
            if outcome == "failed":
                set_desired_status(db, message, "failed")
        if error and message.send_state not in {"sent", "delivered", "read"}:
            task.last_error = message.last_error = error
        task.updated_at = message.updated_at = datetime.utcnow()
        db.commit()
        logger.info("CRM send task %s completed (message=%s, state=%s)", task.id, message.id, message.send_state)


async def _status(task_id, session_factory):
    with session_factory() as db:
        task, message = _load(db, task_id)
        if message is None:
            db.commit()
            return
        message_id, engine = message.id, db.get_bind()
        db.rollback()  # No row lock while waiting for another status operation.
    with _status_lock(engine, message_id) as acquired:
        with session_factory() as db:
            task, message = _load(db, task_id)
            if message is None:
                db.commit()
                return
            if not acquired:
                task.status = "pending"
                task.next_attempt_at = datetime.utcnow() + timedelta(seconds=1)
                db.commit()
                return
            if message.applied_revision >= message.desired_revision:
                task.status = "sent"
                db.commit()
                return
            portal = db.get(Portal, message.portal_id)
            if portal is None or portal.uninstalled_at is not None:
                task.status, task.last_error = "dead", "portal_uninstalled"
                db.commit()
                return
            revision, status = message.desired_revision, message.desired_bitrix_status
            code, external_id = message.provider_code, message.bitrix_message_id
            # Release the row lock before network/refresh; newer events can be
            # saved immediately. The advisory lock orders HTTP calls, not facts.
            db.commit()
            try:
                await update_message_status(portal, db, code, external_id, status)
            except CrmBitrixError as exc:
                db.rollback()
                task = db.get(MessageDeliveryTask, task_id)
                if exc.retryable:
                    _retry(task, exc.code)
                else:
                    task.status, task.last_error = "dead", exc.code
                db.commit()
                logger.warning("CRM status task %s could not be applied (%s)", task_id, exc.code)
                return
            db.expire_all()
            task, message = _load(db, task_id)
            if message is not None:
                message.applied_revision = max(message.applied_revision, revision)
                task.status, task.last_error = "sent", None
                task.updated_at = datetime.utcnow()
            db.commit()
