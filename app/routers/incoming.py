import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Channel, Message, Portal
from app.services.bitrix import send_message_to_bitrix

logger = logging.getLogger(__name__)
router = APIRouter()


@router.api_route("/incoming", methods=["GET", "HEAD"])
async def incoming_verify():
    return JSONResponse({"status": "ok"})


@router.post("/incoming")
async def incoming(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    # TODO: диагностика — логируем полное тело для всех типов, потом вернуть body[:500]
    logger.info("Incoming MAX Bot webhook: %s", body)

    try:
        data = json.loads(body)
    except Exception:
        logger.error("Incoming: failed to parse JSON body")
        return JSONResponse({"status": "ok"})

    # Find channel by sender (subject in webhook = sender identifier)
    sender = data.get("subject")
    if not sender:
        logger.warning("Incoming: missing subject field")
        return JSONResponse({"status": "ok"})

    channel = db.query(Channel).filter_by(sender=sender, is_active=True).first()
    if not channel:
        logger.warning("Incoming: no active channel for sender %s", sender)
        return JSONResponse({"status": "ok"})

    portal: Portal = channel.portal
    if not portal:
        logger.warning("Incoming: channel %s has no portal", channel.id)
        return JSONResponse({"status": "ok"})

    msg_content = data.get("messageContent", {})
    # TODO: диагностика — логируем не-TEXT вместо пропуска, потом вернуть ранний выход
    if msg_content.get("type") != "TEXT":
        logger.info("Incoming: non-TEXT message type=%s full_payload=%s", msg_content.get("type"), body)
        return JSONResponse({"status": "ok"})

    text = msg_content.get("text") or ""
    if not text:
        return JSONResponse({"status": "ok"})

    subscriber = data.get("subscriber", {})
    subscriber_id = str(subscriber.get("id", ""))
    subscriber_identifier = str(subscriber.get("identifier", ""))

    user_info = data.get("userInfo", {})
    user_name = user_info.get("userName") or user_info.get("firstName") or subscriber_identifier

    msg_id = str(data.get("id", ""))
    chat_id = subscriber_identifier  # use identifier as chat.id

    try:
        await send_message_to_bitrix(
            portal=portal,
            db=db,
            chat_id=chat_id,
            user_id=subscriber_id or subscriber_identifier,
            user_name=user_name,
            text=text,
            msg_id=msg_id,
        )

        db.add(
            Message(
                channel_id=channel.id,
                direction="incoming",
                text=text,
                content_type="TEXT",
                max_message_id=msg_id,
                subscriber_identifier=subscriber_identifier,
                sent_at=datetime.utcnow(),
                raw_payload=json.dumps(data, ensure_ascii=False)[:2000],
            )
        )
        db.commit()

    except Exception as e:
        logger.error("Failed to forward to Bitrix24: %s", e)

    return JSONResponse({"status": "ok"})
