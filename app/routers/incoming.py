import json
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Channel, Message, MessageDeliveryTask, Portal
from app.services.delivery_worker import enqueue_incoming
from app.services.rate_limiter import rate_limiter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.api_route("/incoming", methods=["GET", "HEAD"])
@router.api_route("/incoming/{webhook_token}", methods=["GET", "HEAD"])
async def incoming_verify(webhook_token: str = ""):
    return JSONResponse({"status": "ok"})


@router.post("/incoming/{webhook_token}")
async def incoming(webhook_token: str, request: Request, db: Session = Depends(get_db)):
    if not rate_limiter.is_allowed(f"in:{webhook_token}"):
        return JSONResponse({"error": "too_many_requests"}, status_code=429)

    channel = db.query(Channel).filter_by(webhook_token=webhook_token, is_active=True).first()
    if not channel:
        logger.warning("Incoming: invalid webhook token %s", webhook_token[:8] + "...")
        return JSONResponse({"error": "forbidden"}, status_code=403)

    body = await request.body()
    logger.info("Incoming MAX Bot webhook (channel %s): %s", channel.id, body[:500])

    try:
        data = json.loads(body)
    except Exception:
        logger.error("Incoming: failed to parse JSON body")
        return JSONResponse({"error": "bad_request"}, status_code=400)

    portal: Portal = channel.portal
    if not portal:
        logger.warning("Incoming: channel %s has no portal", channel.id)
        return JSONResponse({"status": "ok"})

    msg_content = data.get("messageContent", {})
    msg_type = msg_content.get("type")

    _ATTACHMENT_TYPES = {"IMAGE", "DOCUMENT", "AUDIO", "VIDEO", "VOICE"}

    if msg_type not in ("TEXT", "LOCATION", *_ATTACHMENT_TYPES):
        logger.info("Incoming: unsupported message type=%s, skipping", msg_type)
        return JSONResponse({"status": "ok"})

    subscriber = data.get("subscriber", {})
    subscriber_id = str(subscriber.get("id", ""))
    subscriber_identifier = str(subscriber.get("identifier", ""))

    user_info = data.get("userInfo", {})
    user_name = user_info.get("userName") or user_info.get("firstName") or subscriber_identifier

    msg_id = str(data.get("id", ""))
    chat_id = subscriber_identifier

    if msg_id:
        # Dual dedup: check already-delivered (Message) and already-enqueued (MessageDeliveryTask)
        if db.query(Message).filter_by(
            channel_id=channel.id, direction="incoming", max_message_id=msg_id
        ).first():
            logger.info("Incoming: duplicate message id=%s (already delivered), skipping", msg_id)
            return JSONResponse({"status": "ok"})
        if db.query(MessageDeliveryTask).filter_by(
            channel_id=channel.id, direction="incoming", max_message_id=msg_id
        ).first():
            logger.info("Incoming: duplicate message id=%s (already queued), skipping", msg_id)
            return JSONResponse({"status": "ok"})

    if msg_type in _ATTACHMENT_TYPES:
        attachment = msg_content.get("attachment") or {}
        file_url = attachment.get("url")
        if not file_url:
            logger.warning("Incoming %s: missing attachment.url", msg_type)
            return JSONResponse({"status": "ok"})

        # name is null in edna payload — extract from URL path
        file_name = attachment.get("name") or urlparse(file_url).path.split("/")[-1] or "attachment"
        caption = msg_content.get("caption") or msg_content.get("text") or None

        try:
            enqueue_incoming(
                db=db,
                channel=channel,
                msg_type="file",
                chat_id=chat_id,
                user_id=subscriber_id or subscriber_identifier,
                user_name=user_name,
                msg_id=msg_id,
                content_type=msg_type,
                subscriber_identifier=subscriber_identifier,
                raw_payload=json.dumps(data, ensure_ascii=False)[:2000],
                file_url=file_url,
                file_name=file_name,
                caption=caption,
            )
        except IntegrityError:
            db.rollback()
            logger.info("Incoming: duplicate message id=%s (concurrent), skipping", msg_id)
            return JSONResponse({"status": "ok"})
        except Exception as e:
            logger.error("Incoming: failed to enqueue file message: %s", e)
            return JSONResponse({"error": "service_unavailable"}, status_code=503)

    elif msg_type == "LOCATION":
        loc = msg_content.get("location") or {}
        lat = loc.get("latitude")
        lon = loc.get("longitude")
        if not lat or not lon:
            logger.warning("Incoming LOCATION: missing coordinates")
            return JSONResponse({"status": "ok"})

        maps_url = f"https://yandex.ru/maps/?pt={lon},{lat}&z=16&l=map"
        address = loc.get("address")
        text = f"📍 {address}\n{maps_url}" if address else f"📍 Местоположение: {maps_url}"

        try:
            enqueue_incoming(
                db=db,
                channel=channel,
                msg_type="location",
                chat_id=chat_id,
                user_id=subscriber_id or subscriber_identifier,
                user_name=user_name,
                msg_id=msg_id,
                content_type="LOCATION",
                subscriber_identifier=subscriber_identifier,
                raw_payload=json.dumps(data, ensure_ascii=False)[:2000],
                text=text,
            )
        except IntegrityError:
            db.rollback()
            logger.info("Incoming: duplicate message id=%s (concurrent), skipping", msg_id)
            return JSONResponse({"status": "ok"})
        except Exception as e:
            logger.error("Incoming: failed to enqueue location message: %s", e)
            return JSONResponse({"error": "service_unavailable"}, status_code=503)

    else:  # TEXT
        text = msg_content.get("text") or ""
        if not text:
            return JSONResponse({"status": "ok"})

        try:
            enqueue_incoming(
                db=db,
                channel=channel,
                msg_type="text",
                chat_id=chat_id,
                user_id=subscriber_id or subscriber_identifier,
                user_name=user_name,
                msg_id=msg_id,
                content_type="TEXT",
                subscriber_identifier=subscriber_identifier,
                raw_payload=json.dumps(data, ensure_ascii=False)[:2000],
                text=text,
            )
        except IntegrityError:
            db.rollback()
            logger.info("Incoming: duplicate message id=%s (concurrent), skipping", msg_id)
            return JSONResponse({"status": "ok"})
        except Exception as e:
            logger.error("Incoming: failed to enqueue text message: %s", e)
            return JSONResponse({"error": "service_unavailable"}, status_code=503)

    return JSONResponse({"status": "ok"})
