import json
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Channel, Message, Portal
from app.services.bitrix import send_delivery_status
from app.services.maxbot import send_media, send_message

logger = logging.getLogger(__name__)
router = APIRouter()


def _detect_media_type(content_type: str, filename: str) -> str:
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return "IMAGE"
    if ct.startswith("video/"):
        return "VIDEO"
    if ct.startswith("audio/"):
        return "AUDIO"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in {"jpg", "jpeg", "png", "gif", "webp", "bmp"}:
        return "IMAGE"
    if ext in {"mp4", "avi", "mov", "mkv", "webm"}:
        return "VIDEO"
    if ext in {"mp3", "ogg", "wav", "aac", "m4a"}:
        return "AUDIO"
    return "DOCUMENT"


def strip_bbcode(text: str) -> str:
    text = re.sub(r"\[b\].*?\[/b\]\s*", "", text)
    text = re.sub(r"\[br\]", "\n", text)
    text = re.sub(r"\[[^\]]+\]", "", text)
    return text.strip()


def _deep_set(obj: dict, parts: list, value: str) -> None:
    key = parts[0]
    if len(parts) == 1:
        obj[key] = value
        return
    next_key = parts[1]
    if key not in obj:
        obj[key] = [] if next_key.isdigit() else {}
    child = obj[key]
    if isinstance(child, list):
        idx = int(next_key)
        while len(child) <= idx:
            child.append({})
        _deep_set(child[idx], parts[2:], value)
    else:
        _deep_set(child, parts[1:], value)


def _parse_php_form(flat: dict) -> dict:
    """Convert PHP-style flat form params to nested dict.

    e.g. {'data[MESSAGES][0][chat][id]': ['abc']} → {'data': {'MESSAGES': [{'chat': {'id': 'abc'}}]}}
    """
    result = {}
    for raw_key, values in flat.items():
        value = values[0] if isinstance(values, list) else values
        parts = re.findall(r"[^\[\]]+", raw_key)
        _deep_set(result, parts, value)
    return result


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
    body = await request.body()
    content_type = request.headers.get("content-type", "")

    if "application/x-www-form-urlencoded" in content_type:
        flat = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        data = _parse_php_form(flat)
        if not data.get("event"):
            # Bitrix24 opens the app in iframe — redirect to settings UI
            return RedirectResponse("/settings", status_code=303)
        logger.info("Handler received form-encoded event: %s", data.get("event"))
    elif "multipart/form-data" in content_type:
        return RedirectResponse("/settings", status_code=303)
    else:
        logger.info("Handler received: %s", body[:500])
        try:
            data = json.loads(body)
        except Exception:
            logger.error("Handler: failed to parse body (content-type=%s): %s", content_type, body[:200])
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
        await _handle_outgoing_message(data, portal, db)

    return JSONResponse({"status": "ok"})


async def _handle_outgoing_message(data: dict, portal: Portal, db: Session) -> None:
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

        # Extract file attachment from Bitrix24 payload (files array)
        files = msg.get("message", {}).get("files", [])
        file_info = files[0] if files else None

        if files:
            logger.info("File attachments: %s", files)

        if not text and not file_info:
            logger.warning("Skipping msg: no text and no attachment (raw=%r)", raw_text[:200])
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

        if file_info:
            bitrix_url = file_info.get("downloadLink") or file_info.get("link", "")
            file_name = file_info.get("name", "")
            mime = file_info.get("mime", "")

            if not bitrix_url or not file_name:
                logger.warning("File attachment missing link or name, skipping: %s", file_info)
                continue

            # Wrap in proxy URL so edna sees a proper file extension
            file_url = f"{settings.app_base_url}/file/{quote(file_name)}?dl={quote(bitrix_url)}"
            logger.info("Proxy URL for edna: %s", file_url)

            max_type = _detect_media_type(mime, file_name)
            caption = text if text else None  # text becomes caption for media

            try:
                result = await send_media(
                    api_key=channel.api_key,
                    sender=channel.sender,
                    max_id=chat_id,
                    content_type=max_type,
                    url=file_url,
                    name=file_name,
                    caption=caption,
                )
                logger.info("edna media response: %s", result)
                db.add(Message(
                    channel_id=channel.id,
                    direction="outgoing",
                    text=caption or file_name,
                    content_type=max_type,
                    bitrix_chat_id=str(im_chat_id) if im_chat_id else None,
                    subscriber_identifier=chat_id,
                    sent_at=datetime.utcnow(),
                    raw_payload=str(data)[:2000],
                ))
                db.commit()
                if im_chat_id and im_message_id and line_id:
                    try:
                        await send_delivery_status(portal=portal, db=db, line_id=int(line_id),
                            bitrix_chat_id=int(im_chat_id), bitrix_message_id=int(im_message_id), chat_id=chat_id)
                    except Exception as e:
                        logger.warning("Delivery status error: %s", e)
            except Exception as e:
                logger.error("Failed to send media to MAX Bot: %s", e)
            continue  # done with this message, skip the text branch below

        logger.info("Sending to edna: sender=%s, max_id=%s, text=%r", channel.sender, chat_id, text[:100])
        try:
            result = await send_message(
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
                    await send_delivery_status(
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
