import asyncio
import json
import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Channel, Message, MessageDeliveryTask, Portal, is_max_bot_channel
from app.services.bitrix import (
    send_delivery_status,
    send_file_to_bitrix,
    send_message_to_bitrix,
    send_undelivered_notice,
)
from app.services.file_cache import get as file_cache_get, make_signed_url
from app.services.maxbot import (
    VALID_ID_TYPES,
    send_media,
    send_media_max,
    send_message,
    send_message_max,
)
from app.services.token import PaymentRequiredError

logger = logging.getLogger(__name__)

RETRY_SCHEDULE = [1, 5, 30, 120, 600, 1800]  # seconds between retry attempts

# Тип канала хранится техническим кодом (MAX_BOT), а оператор видит его в тексте
# уведомления о недоставке — показываем человеческое название.
_CHANNEL_LABELS = {"MAX_BOT": "MAX Bot", "MAX": "MAX"}

TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def _classify_error(exc: Exception) -> str:
    """Returns 'transient' (retry) or 'permanent' (fail immediately)."""
    # RISK mitigation: check custom exceptions BEFORE httpx (PaymentRequiredError IS-A RuntimeError)
    if isinstance(exc, PaymentRequiredError):
        return "permanent"
    if isinstance(exc, RuntimeError):
        return "permanent"
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code in TRANSIENT_STATUS_CODES:
            return "transient"
        return "permanent"
    if isinstance(exc, (httpx.RequestError, httpx.TimeoutException)):
        return "transient"
    return "transient"


def _update_status(
    task_id: int,
    status: str,
    *,
    error: str | None = None,
    retry_count: int | None = None,
    next_attempt_at: datetime | None = None,
) -> None:
    db = SessionLocal()
    try:
        task = db.query(MessageDeliveryTask).filter_by(id=task_id).first()
        if task:
            task.status = status
            task.updated_at = datetime.utcnow()
            if error is not None:
                task.last_error = error[:2000]
            if retry_count is not None:
                task.retry_count = retry_count
            if next_attempt_at is not None:
                task.next_attempt_at = next_attempt_at
            db.commit()
    finally:
        db.close()


async def _execute_incoming(channel_id: int, payload: dict) -> None:
    """Send a MAX Bot message to Bitrix24, then save Message row on success."""
    db = SessionLocal()
    try:
        channel = db.query(Channel).filter_by(id=channel_id).first()
        if not channel:
            raise RuntimeError("Channel %d not found" % channel_id)
        portal: Portal = channel.portal  # lazy-loaded in same session

        msg_type = payload["msg_type"]
        if msg_type == "file":
            await send_file_to_bitrix(
                portal=portal,
                db=db,
                chat_id=payload["chat_id"],
                user_id=payload["user_id"],
                user_name=payload["user_name"],
                user_last_name=payload.get("user_last_name"),
                user_phone=payload.get("user_phone"),
                msg_id=payload["msg_id"],
                file_url=payload["file_url"],
                file_name=payload["file_name"],
                caption=payload.get("caption"),
            )
        else:  # text or location
            await send_message_to_bitrix(
                portal=portal,
                db=db,
                chat_id=payload["chat_id"],
                user_id=payload["user_id"],
                user_name=payload["user_name"],
                user_last_name=payload.get("user_last_name"),
                user_phone=payload.get("user_phone"),
                text=payload["text"],
                msg_id=payload["msg_id"],
            )

        db.add(Message(
            channel_id=channel_id,
            direction="incoming",
            text=payload.get("text") or payload.get("caption") or payload.get("file_name", ""),
            content_type=payload.get("content_type", "TEXT"),
            max_message_id=payload.get("msg_id"),
            subscriber_identifier=payload.get("subscriber_identifier"),
            # #16: remembered so outgoing messages to a MAX channel can fill `to.type`
            subscriber_id_type=payload.get("subscriber_id_type"),
            # #2: persist exact client identity sent to Bitrix (user.id / name)
            subscriber_user_id=payload.get("user_id"),
            user_name=payload.get("user_name"),
            user_last_name=payload.get("user_last_name"),
            sent_at=datetime.utcnow(),
            raw_payload=payload.get("raw_payload"),
        ))
        db.commit()
    finally:
        db.close()


def _recipient_id_type(db: Session, channel_id: int, identifier: str) -> str:
    """#16: is this recipient's identifier a MAX_ID or a PHONE?

    Taken from the client's own latest incoming message. Scoped to `channel_id`
    on purpose: MAX and MAX Bot use the SAME identifier for the same person, so
    an unscoped lookup could pick up a row from the other channel.
    Nothing stored (older rows, or the client never wrote first) → MAX_ID, which
    is what every observed client uses. A type edna sends that we don't know how
    to put in the `to` block falls back the same way, so one odd value can't turn
    every reply to that client into a failed delivery."""
    row = (
        db.query(Message.subscriber_id_type)
        .filter(
            Message.channel_id == channel_id,
            Message.direction == "incoming",
            Message.subscriber_identifier == str(identifier),
            Message.subscriber_id_type.isnot(None),
        )
        .order_by(Message.sent_at.desc())
        .first()
    )
    id_type = row[0] if row else None
    if id_type not in VALID_ID_TYPES:
        if id_type:
            logger.warning("Unknown identifier type %r for %s — falling back to MAX_ID",
                           id_type, identifier)
        return "MAX_ID"
    return id_type


async def _execute_outgoing(channel_id: int, payload: dict) -> None:
    """Send a Bitrix24 message to edna, then save Message row + delivery status on success."""
    # Test-only (#2): force a permanent failure to verify the operator's "undelivered"
    # notice on a live portal. Enabled via EDNA_FORCE_FAIL=1 env var; off by default.
    if settings.edna_force_fail == "1":
        raise RuntimeError("EDNA_FORCE_FAIL: simulated permanent send failure")

    # RISK-1 mitigation: file cache may have expired — treat as permanent failure
    if payload.get("msg_type") == "media":
        file_key = payload.get("file_key")
        if not file_key or file_cache_get(file_key) is None:
            raise FileNotFoundError("file_cache_expired:%s" % file_key)

    db = SessionLocal()
    try:
        channel = db.query(Channel).filter_by(id=channel_id).first()
        if not channel:
            raise RuntimeError("Channel %d not found" % channel_id)
        portal: Portal = channel.portal

        # #16: what edna needs — the bare client identifier, never the marked one.
        max_id = payload["max_id"]
        # MAX Bot keeps its own endpoint; every other edna channel type goes through
        # the MAX endpoint, which also needs the type of the recipient's identifier.
        use_max_endpoint = not is_max_bot_channel(channel)
        to_type = _recipient_id_type(db, channel_id, max_id) if use_max_endpoint else None

        if payload["msg_type"] == "media":
            file_url = make_signed_url(payload["file_key"])
            if use_max_endpoint:
                await send_media_max(
                    api_key=channel.api_key,
                    sender=channel.sender,
                    to_value=max_id,
                    to_type=to_type,
                    content_type=payload["content_type"],
                    url=file_url,
                    name=payload["file_name"],
                    caption=payload.get("caption"),
                )
            else:
                await send_media(
                    api_key=channel.api_key,
                    sender=channel.sender,
                    max_id=max_id,
                    content_type=payload["content_type"],
                    url=file_url,
                    name=payload["file_name"],
                    caption=payload.get("caption"),
                )
        else:
            if use_max_endpoint:
                await send_message_max(
                    api_key=channel.api_key,
                    sender=channel.sender,
                    to_value=max_id,
                    to_type=to_type,
                    text=payload["text"],
                )
            else:
                await send_message(
                    api_key=channel.api_key,
                    sender=channel.sender,
                    max_id=max_id,
                    text=payload["text"],
                )

        db.add(Message(
            channel_id=channel_id,
            direction="outgoing",
            text=payload.get("text") or payload.get("caption") or payload.get("file_name", ""),
            content_type=payload.get("content_type", "TEXT"),
            bitrix_chat_id=str(payload["im_chat_id"]) if payload.get("im_chat_id") else None,
            subscriber_identifier=max_id,
            sent_at=datetime.utcnow(),
            raw_payload=payload.get("raw_payload"),
        ))
        db.commit()

        # ADR-8: send delivery status — best-effort, no retry on failure
        im_chat_id = payload.get("im_chat_id")
        im_message_id = payload.get("im_message_id")
        line_id = payload.get("line_id")
        if im_chat_id and im_message_id and line_id:
            try:
                await send_delivery_status(
                    portal=portal,
                    db=db,
                    line_id=int(line_id),
                    bitrix_chat_id=int(im_chat_id),
                    bitrix_message_id=int(im_message_id),
                    # #16: Bitrix knows this dialog by the marked id, not the bare one
                    chat_id=str(payload.get("bitrix_chat_id") or max_id),
                )
            except Exception as e:
                logger.warning("Delivery status error (best-effort): %s", e)
    finally:
        db.close()


async def _notify_outgoing_undelivered(channel_id: int, payload: dict, error: str) -> None:
    """Best-effort: tell the Bitrix operator that an outgoing message wasn't delivered to MAX.

    Called only when an outgoing (Bitrix → edna) task fails terminally (dead / file expired).
    Failure to post the notice itself must never break the worker — hence best-effort.
    """
    db = SessionLocal()
    try:
        channel = db.query(Channel).filter_by(id=channel_id).first()
        if not channel:
            return
        portal: Portal = channel.portal
        max_id = payload.get("max_id")
        # #16: the client's identity is stored against the BARE identifier, but the
        # notice has to be posted into the dialog Bitrix knows — which for a MAX
        # channel is the MARKED id. Sending the bare one would miss the dialog.
        bitrix_chat_id = payload.get("bitrix_chat_id") or max_id
        line_id = payload.get("line_id") or portal.open_line_id
        if not max_id or not line_id:
            logger.warning(
                "Undelivered notice skipped: missing chat_id/line_id (channel=%s)", channel_id
            )
            return

        # Reuse the EXACT client identity from the latest incoming message so the notice
        # lands in the existing dialog instead of spawning a new "Гость" contact.
        orig = (
            db.query(Message)
            .filter_by(channel_id=channel_id, direction="incoming", subscriber_identifier=str(max_id))
            .order_by(Message.sent_at.desc())
            .first()
        )
        if not orig or not orig.subscriber_user_id:
            logger.warning(
                "Undelivered notice skipped: no known client identity for chat %s (channel=%s)",
                max_id, channel_id,
            )
            return

        # Use the channel's edna type (e.g. MAX, WHATSAPP) captured at registration;
        # fall back to "MAX" for older/manual channels with no stored type.
        ch_type = _CHANNEL_LABELS.get(channel.channel_type or "MAX", channel.channel_type or "MAX")
        if payload.get("msg_type") == "media":
            notice = f"⚠️ Файл не доставлен клиенту в {ch_type}. Попробуйте отправить ещё раз."
        else:
            notice = f"⚠️ Сообщение не доставлено клиенту в {ch_type}. Попробуйте отправить ещё раз."

        await send_undelivered_notice(
            portal=portal,
            db=db,
            line_id=int(line_id),
            chat_id=str(bitrix_chat_id),
            user_id=str(orig.subscriber_user_id),
            notice_text=notice,
            user_name=orig.user_name,
            user_last_name=orig.user_last_name,
        )
        logger.info(
            "Undelivered notice sent to operator (channel=%s, chat=%s)", channel_id, bitrix_chat_id
        )
    except Exception as e:
        logger.warning("Undelivered notice failed (best-effort): %s", e)
    finally:
        db.close()


async def _process_one_task() -> bool:
    """Pick the next due pending task, execute it, update status. Returns True if processed."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        task = (
            db.query(MessageDeliveryTask)
            .filter_by(status="pending")
            .filter(MessageDeliveryTask.next_attempt_at <= now)
            .order_by(MessageDeliveryTask.next_attempt_at)
            .first()
        )
        if not task:
            return False

        # Extract values before closing session (avoids DetachedInstanceError)
        task_id = task.id
        task_type = task.task_type
        payload_str = task.payload
        retry_count = task.retry_count
        channel_id = task.channel_id

        task.status = "processing"
        task.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()

    payload = json.loads(payload_str)

    try:
        if task_type == "send_to_bitrix":
            await _execute_incoming(channel_id, payload)
        else:
            await _execute_outgoing(channel_id, payload)

        _update_status(task_id, "sent")
        logger.info("Task %d sent successfully (type=%s)", task_id, task_type)

    except FileNotFoundError as e:
        # RISK-1: file cache expired — permanent failure, no retry
        _update_status(task_id, "failed", error=str(e))
        logger.error("Task %d failed permanently (file cache expired): %s", task_id, e)
        if task_type == "send_to_edna":
            await _notify_outgoing_undelivered(channel_id, payload, str(e))

    except Exception as e:
        error_type = _classify_error(e)
        new_retry_count = retry_count + 1

        if error_type == "permanent" or new_retry_count > len(RETRY_SCHEDULE):
            _update_status(task_id, "dead", error=str(e), retry_count=new_retry_count)
            logger.error("Task %d dead after %d attempts: %s", task_id, new_retry_count, e)
            if task_type == "send_to_edna":
                await _notify_outgoing_undelivered(channel_id, payload, str(e))
        else:
            delay = RETRY_SCHEDULE[new_retry_count - 1]
            next_attempt = datetime.utcnow() + timedelta(seconds=delay)
            _update_status(
                task_id, "pending",
                error=str(e),
                retry_count=new_retry_count,
                next_attempt_at=next_attempt,
            )
            logger.warning("Task %d retry %d/%d in %ds: %s",
                           task_id, new_retry_count, len(RETRY_SCHEDULE), delay, e)

    return True


async def _reset_stale_tasks() -> None:
    """Reset tasks stuck in 'processing' from a previous app crash."""
    db = SessionLocal()
    try:
        stale = db.query(MessageDeliveryTask).filter_by(status="processing").all()
        for t in stale:
            t.status = "pending"
            t.updated_at = datetime.utcnow()
        if stale:
            db.commit()
            logger.warning("Reset %d stale processing tasks to pending", len(stale))
    finally:
        db.close()


async def _worker_loop() -> None:
    """Background delivery worker with exponential backoff retry."""
    await _reset_stale_tasks()
    logger.info("Delivery worker started")

    while True:
        try:
            processed = await _process_one_task()
            if not processed:
                await asyncio.sleep(2)
        except Exception:
            # RISK-2 mitigation: outer loop must NEVER die — log and continue
            logger.exception("Worker loop error")
            await asyncio.sleep(5)


def start_worker() -> None:
    """Start the background delivery worker. Called from @app.on_event('startup')."""
    asyncio.create_task(_worker_loop())
    logger.info("Delivery worker task created")


def enqueue_incoming(
    db: Session,
    channel: Channel,
    msg_type: str,
    chat_id: str,
    user_id: str,
    user_name: str,
    msg_id: str,
    content_type: str,
    subscriber_identifier: str,
    raw_payload: str,
    subscriber_id_type: str | None = None,
    text: str | None = None,
    file_url: str | None = None,
    file_name: str | None = None,
    caption: str | None = None,
    user_last_name: str | None = None,
    user_phone: str | None = None,
) -> MessageDeliveryTask:
    """Enqueue an incoming MAX Bot message for delivery to Bitrix24."""
    payload = {
        "msg_type": msg_type,
        "chat_id": chat_id,
        "user_id": user_id,
        "user_name": user_name,
        "user_last_name": user_last_name,
        "user_phone": user_phone,
        "msg_id": msg_id,
        "content_type": content_type,
        "subscriber_identifier": subscriber_identifier,
        "subscriber_id_type": subscriber_id_type,
        "raw_payload": raw_payload,
        "text": text,
        "file_url": file_url,
        "file_name": file_name,
        "caption": caption,
    }
    task = MessageDeliveryTask(
        channel_id=channel.id,
        task_type="send_to_bitrix",
        direction="incoming",
        payload=json.dumps(payload, ensure_ascii=False),
        status="pending",
        next_attempt_at=datetime.utcnow(),
        max_message_id=msg_id,
    )
    db.add(task)
    db.commit()
    return task


def enqueue_outgoing(
    db: Session,
    channel: Channel,
    msg_type: str,
    max_id: str,
    raw_payload: str,
    bitrix_chat_id: str | None = None,
    im_chat_id: str | None = None,
    im_message_id: str | None = None,
    line_id: str | None = None,
    text: str | None = None,
    content_type: str | None = None,
    file_key: str | None = None,
    file_name: str | None = None,
    caption: str | None = None,
) -> MessageDeliveryTask:
    """Enqueue an outgoing Bitrix24 message for delivery to edna/MAX Bot.

    #16: `max_id` is the BARE client identifier (what edna expects), while
    `bitrix_chat_id` is the id Bitrix knows this dialog by — for MAX channels it
    carries the channel marker. They are equal for MAX Bot."""
    payload = {
        "msg_type": msg_type,
        "max_id": max_id,
        "bitrix_chat_id": bitrix_chat_id or max_id,
        "im_chat_id": im_chat_id,
        "im_message_id": im_message_id,
        "line_id": line_id,
        "raw_payload": raw_payload,
        "text": text,
        "content_type": content_type,
        "file_key": file_key,
        "file_name": file_name,
        "caption": caption,
    }
    task = MessageDeliveryTask(
        channel_id=channel.id,
        task_type="send_to_edna",
        direction="outgoing",
        payload=json.dumps(payload, ensure_ascii=False),
        status="pending",
        next_attempt_at=datetime.utcnow(),
    )
    db.add(task)
    db.commit()
    return task
