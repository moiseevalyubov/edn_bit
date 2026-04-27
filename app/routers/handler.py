import json
import logging
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Channel, Message, Portal
from app.services.bitrix import send_delivery_status
from app.services.maxbot import send_message

logger = logging.getLogger(__name__)
router = APIRouter()


def strip_bbcode(text: str) -> str:
    """Remove Bitrix24 BBCode tags from operator text."""
    text = re.sub(r"\[b\].*?\[/b\]\s*", "", text)
    text = re.sub(r"\[br\]", "\n", text)
    text = re.sub(r"\[[^\]]+\]", "", text)
    return text.strip()


def update_portal_tokens(portal: Portal, auth: dict, db: Session) -> None:
    if not auth.get("access_token"):
        return
    portal.access_token = auth["access_token"]
    if auth.get("refresh_token"):
        portal.refresh_token = auth["refresh_token"]
    expires_in = int(auth.get("expires_in", 3600))
    portal.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    if auth.get("client_endpoint"):
        portal.client_endpoint = auth["client_endpoint"]
    db.commit()


@router.get("/handler")
async def handler_page():
    return RedirectResponse("/settings")


@router.post("/handler")
async def handler(request: Request, db: Session = Depends(get_db)):
    content_type = request.headers.get("content-type", "")

    # Bitrix24 opens the app in iframe via POST with form data — redirect to settings UI
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        return RedirectResponse("/settings", status_code=303)

    body = await request.body()
    logger.info("Handler received: %s", body[:500])

    try:
        data = json.loads(body)
    except Exception:
        logger.error("Handler: failed to parse JSON body")
        return JSONResponse({"status": "ok"})

    event = data.get("event", "").upper()
    auth = data.get("auth", {})
    member_id = auth.get("member_id")
    app_token = auth.get("application_token")

    portal = db.query(Portal).filter_by(member_id=member_id).first()
    if not portal:
        logger.warning("Handler: unknown portal %s", member_id)
        return JSONResponse({"status": "ok"})

    # Verify application token
    if portal.app_token and app_token and portal.app_token != app_token:
        logger.warning("Handler: token mismatch for portal %s", member_id)
        return JSONResponse({"status": "ok"})

    # Refresh tokens from event
    update_portal_tokens(portal, auth, db)

    if event == "ONIMCONNECTORMESSAGEADD":
        _handle_outgoing_message(data, portal, db)

    return JSONResponse({"status": "ok"})


def _handle_outgoing_message(data: dict, portal: Portal, db: Session) -> None:
    messages = data.get("data", {}).get("MESSAGES", [])
    line_id = data.get("data", {}).get("LINE")
    logger.info("_handle_outgoing_message: portal=%s, messages_count=%d, line_id=%s",
                portal.member_id, len(messages), line_id)

    for msg in messages:
        chat_id = msg.get("chat", {}).get("id")
        raw_text = msg.get("message", {}).get("text", "") or ""
        text = strip_bbcode(raw_text)
        im_chat_id = msg.get("im", {}).get("chat_id")
        im_message_id = msg.get("im", {}).get("message_id")

        logger.info("Processing msg: chat_id=%r, raw_text=%r, text_after_strip=%r",
                    chat_id, raw_text[:100], text[:100] if text else "")

        if not chat_id:
            logger.warning("Skipping msg: chat_id is empty")
            continue
        if not text:
            logger.warning("Skipping msg: text is empty after BBCode strip (raw=%r)", raw_text[:200])
            continue

        # Find active channel by subscriber_identifier (= chat_id)
        channel = (
            db.query(Channel)
            .filter_by(portal_id=portal.id, is_active=True)
            .join(Message, isouter=True)
            .filter(Message.subscriber_identifier == chat_id)
            .order_by(Message.sent_at.desc())
            .first()
        )

        if not channel:
            # Fallback: use first active channel of this portal
            channel = db.query(Channel).filter_by(portal_id=portal.id, is_active=True).first()
            if channel:
                logger.info("Using fallback channel (id=%d) for chat_id=%s", channel.id, chat_id)

        if not channel:
            logger.warning("No active channel for portal %s, chat %s", portal.member_id, chat_id)
            continue

        logger.info("Sending to edna: sender=%s, max_id=%s, text=%r", channel.sender, chat_id, text[:100])
        try:
            result = send_message(
                api_key=channel.api_key,
                sender=channel.sender,
                max_id=chat_id,
                text=text,
            )
            logger.info("edna response: %s", result)

            db.add(
                Message(
                    channel_id=channel.id,
                    direction="outgoing",
                    text=text,
                    content_type="TEXT",
                    bitrix_chat_id=str(im_chat_id) if im_chat_id else None,
                    subscriber_identifier=chat_id,
                    sent_at=datetime.utcnow(),
                    raw_payload=str(data)[:2000],
                )
            )
            db.commit()

            if im_chat_id and im_message_id and line_id:
                try:
                    send_delivery_status(
                        portal=portal,
                        db=db,
                        line_id=int(line_id),
                        bitrix_chat_id=int(im_chat_id),
                        bitrix_message_id=int(im_message_id),
                        chat_id=chat_id,
                    )
                except Exception as e:
                    logger.warning("Delivery status error: %s", e)

        except Exception as e:
            logger.error("Failed to send to MAX Bot: %s", e)
