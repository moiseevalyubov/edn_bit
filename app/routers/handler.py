import hashlib
import json
import logging
import random
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs

import httpx

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Channel, Message, Portal, SeenEvent
from app.services.delivery_worker import enqueue_outgoing
from app.services.file_cache import store as cache_file
from app.services.rate_limiter import rate_limiter

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


@router.head("/handler")
async def handler_head():
    return Response(status_code=200)


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

    # SEC-7: tolerate non-object payloads without crashing on .get() below
    if not isinstance(data, dict):
        logger.error("Handler: payload is not a JSON object")
        return JSONResponse({"status": "ok"})

    event = data.get("event", "").upper()
    auth = data.get("auth")
    if not isinstance(auth, dict):
        auth = {}
    member_id = auth.get("member_id")
    app_token = auth.get("application_token")

    if member_id and not rate_limiter.is_allowed(f"h:{member_id}"):
        return JSONResponse({"error": "too_many_requests"}, status_code=429)

    portal = db.query(Portal).filter_by(member_id=member_id).first()
    if not portal:
        logger.warning("Handler: unknown portal %s", member_id)
        return JSONResponse({"status": "ok"})

    # SEC-2: always verify application_token when the portal has one stored
    if portal.app_token:
        if not app_token or portal.app_token != app_token:
            logger.warning("Handler: token mismatch for portal %s", member_id)
            return JSONResponse({"status": "ok"})
    else:
        logger.warning("Handler: portal %s has no app_token stored — skipping token check", member_id)

    # SEC-2: anti-replay — reject requests with a fingerprint seen in the last 10 minutes
    fingerprint = hashlib.sha256(body).hexdigest()
    cutoff = datetime.utcnow() - timedelta(minutes=10)
    if db.query(SeenEvent).filter(
        SeenEvent.fingerprint == fingerprint,
        SeenEvent.seen_at >= cutoff,
    ).first():
        logger.warning("Handler: duplicate event rejected (replay) for portal %s", member_id)
        return JSONResponse({"status": "ok"})
    db.add(SeenEvent(fingerprint=fingerprint))
    db.commit()

    # Probabilistic cleanup to keep seen_events table small (~1% of requests)
    if random.random() < 0.01:
        db.query(SeenEvent).filter(SeenEvent.seen_at < cutoff).delete()
        db.commit()

    # Refresh tokens from event
    update_portal_tokens(portal, auth, db)

    # Ignore events for uninstalled portals, except OnAppUninstall itself
    if portal.uninstalled_at is not None and event != "ONAPPUNINSTALL":
        logger.info("Ignoring event %s for uninstalled portal %s", event, member_id)
        return JSONResponse({"status": "ok"})

    if event == "ONIMCONNECTORMESSAGEADD":
        await _handle_outgoing_message(data, portal, db)
    elif event == "ONAPPUNINSTALL":
        await _handle_app_uninstall(portal, db)

    return JSONResponse({"status": "ok"})


async def _handle_app_uninstall(portal: Portal, db: Session) -> None:
    logger.info("App uninstalled from portal %s — marking as uninstalled", portal.member_id)
    deactivated = 0
    for channel in portal.channels:
        if channel.is_active:
            channel.is_active = False
            channel.disconnected_at = datetime.utcnow()
            deactivated += 1
    portal.uninstalled_at = datetime.utcnow()
    db.commit()
    logger.info("Portal %s marked as uninstalled, %d channels deactivated",
                portal.member_id, deactivated)


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

        # #16 (проба): a marked chat id ({sender}:{identifier}) belongs to a
        # non-legacy channel whose outgoing format isn't implemented yet. Skip it
        # rather than let the fallback below pick an arbitrary channel and push a
        # malformed id to edna.
        if ":" in str(chat_id):
            logger.warning(
                "Skipping msg: marked chat_id=%r — outgoing for this channel type "
                "is not implemented yet (#16)", chat_id,
            )
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

            # Download file from Bitrix immediately while the SIGN is fresh
            ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "bin"
            try:
                async with httpx.AsyncClient(follow_redirects=True) as client:
                    bitrix_resp = await client.get(bitrix_url, timeout=30)
                bitrix_resp.raise_for_status()
            except Exception as e:
                logger.error("Failed to download file from Bitrix: %s", e)
                continue

            file_key = cache_file(bitrix_resp.content, bitrix_resp.headers.get("content-type", "application/octet-stream"), ext)
            logger.info("Cached file for edna: key=%s (%d bytes)", file_key, len(bitrix_resp.content))

            max_type = _detect_media_type(mime, file_name)
            caption = text if text else None

            enqueue_outgoing(
                db=db,
                channel=channel,
                msg_type="media",
                max_id=chat_id,
                raw_payload=str(data)[:2000],
                im_chat_id=str(im_chat_id) if im_chat_id else None,
                im_message_id=str(im_message_id) if im_message_id else None,
                line_id=str(line_id) if line_id else None,
                content_type=max_type,
                file_key=file_key,
                file_name=file_name,
                caption=caption,
            )
            logger.info("Enqueued media task for chat_id=%s file=%s", chat_id, file_name)
            continue

        enqueue_outgoing(
            db=db,
            channel=channel,
            msg_type="text",
            max_id=chat_id,
            raw_payload=str(data)[:2000],
            im_chat_id=str(im_chat_id) if im_chat_id else None,
            im_message_id=str(im_message_id) if im_message_id else None,
            line_id=str(line_id) if line_id else None,
            text=text,
        )
        logger.info("Enqueued text task for chat_id=%s", chat_id)
