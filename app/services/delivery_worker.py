import asyncio
import json
import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Channel, Message, MessageDeliveryTask, Portal
from app.services.bitrix import send_delivery_status, send_file_to_bitrix, send_message_to_bitrix
from app.services.file_cache import get as file_cache_get, make_signed_url
from app.services.maxbot import send_media, send_message
from app.services.token import PaymentRequiredError

logger = logging.getLogger(__name__)

RETRY_SCHEDULE = [1, 5, 30, 120, 600, 1800]  # seconds between retry attempts

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
            sent_at=datetime.utcnow(),
            raw_payload=payload.get("raw_payload"),
        ))
        db.commit()
    finally:
        db.close()


async def _execute_outgoing(channel_id: int, payload: dict) -> None:
    """Send a Bitrix24 message to edna, then save Message row + delivery status on success."""
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

        chat_id = payload["max_id"]

        if payload["msg_type"] == "media":
            file_url = make_signed_url(payload["file_key"])
            await send_media(
                api_key=channel.api_key,
                sender=channel.sender,
                max_id=chat_id,
                content_type=payload["content_type"],
                url=file_url,
                name=payload["file_name"],
                caption=payload.get("caption"),
            )
        else:
            await send_message(
                api_key=channel.api_key,
                sender=channel.sender,
                max_id=chat_id,
                text=payload["text"],
            )

        db.add(Message(
            channel_id=channel_id,
            direction="outgoing",
            text=payload.get("text") or payload.get("caption") or payload.get("file_name", ""),
            content_type=payload.get("content_type", "TEXT"),
            bitrix_chat_id=str(payload["im_chat_id"]) if payload.get("im_chat_id") else None,
            subscriber_identifier=chat_id,
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
                    chat_id=chat_id,
                )
            except Exception as e:
                logger.warning("Delivery status error (best-effort): %s", e)
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

    except Exception as e:
        error_type = _classify_error(e)
        new_retry_count = retry_count + 1

        if error_type == "permanent" or new_retry_count > len(RETRY_SCHEDULE):
            _update_status(task_id, "dead", error=str(e), retry_count=new_retry_count)
            logger.error("Task %d dead after %d attempts: %s", task_id, new_retry_count, e)
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
    text: str | None = None,
    file_url: str | None = None,
    file_name: str | None = None,
    caption: str | None = None,
) -> MessageDeliveryTask:
    """Enqueue an incoming MAX Bot message for delivery to Bitrix24."""
    payload = {
        "msg_type": msg_type,
        "chat_id": chat_id,
        "user_id": user_id,
        "user_name": user_name,
        "msg_id": msg_id,
        "content_type": content_type,
        "subscriber_identifier": subscriber_identifier,
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
    im_chat_id: str | None = None,
    im_message_id: str | None = None,
    line_id: str | None = None,
    text: str | None = None,
    content_type: str | None = None,
    file_key: str | None = None,
    file_name: str | None = None,
    caption: str | None = None,
) -> MessageDeliveryTask:
    """Enqueue an outgoing Bitrix24 message for delivery to edna/MAX Bot."""
    payload = {
        "msg_type": msg_type,
        "max_id": max_id,
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
