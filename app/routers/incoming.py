import json
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Channel, Message, MessageDeliveryTask, Portal, bitrix_chat_id_for
from app.services.delivery_worker import enqueue_incoming
from app.services.rate_limiter import rate_limiter
from app.services.sanitize import sanitize_name, sanitize_text

logger = logging.getLogger(__name__)
router = APIRouter()


def _identifier_type(subscriber: dict, subscriber_identifier: str) -> str:
    """#16: type of the client's primary identifier — MAX_ID or PHONE.

    Both MAX and MAX Bot send an `identifiers` array, but it may list several
    entries (e.g. MAX_ID plus PHONE when edna has merged the profile) and PHONE
    is not always present. The type we need is the one belonging to the entry
    that matches `subscriber.identifier` — the id we actually reply to.
    Unknown or missing array → MAX_ID, which is what every observed client uses.
    """
    identifiers = subscriber.get("identifiers")
    if isinstance(identifiers, list):
        for item in identifiers:
            if not isinstance(item, dict):
                continue
            if str(item.get("value", "")) == subscriber_identifier:
                id_type = str(item.get("type") or "").upper()
                if id_type:
                    return id_type
    return "MAX_ID"


def _subscriber_phone(subscriber: dict) -> str | None:
    """Номер клиента из массива `identifiers`, если edna его прислала.

    Приходит не всегда — только когда профиль в edna связан с телефоном. Порядок
    в массиве не фиксирован: PHONE встречается и первым, и вторым."""
    identifiers = subscriber.get("identifiers")
    if not isinstance(identifiers, list):
        return None
    for item in identifiers:
        if isinstance(item, dict) and str(item.get("type") or "").upper() == "PHONE":
            value = str(item.get("value") or "").strip()
            if value:
                return value
    return None


def _known_user_name(db: Session, channel: Channel, subscriber_identifier: str) -> tuple[str | None, str | None]:
    """#16: last known real name of this client across all channels of the portal.

    MAX channels always send `userInfo: null`, so the name can only be learned
    from another channel of the same portal. Names equal to the identifier are
    placeholders we stored ourselves — skip them, or we'd "find" the same digits.
    """
    if not subscriber_identifier:
        return None, None
    row = (
        db.query(Message.user_name, Message.user_last_name)
        .join(Channel, Message.channel_id == Channel.id)
        .filter(
            Channel.portal_id == channel.portal_id,
            Message.direction == "incoming",
            Message.subscriber_identifier == subscriber_identifier,
            Message.user_name.isnot(None),
            Message.user_name != subscriber_identifier,
        )
        .order_by(Message.sent_at.desc())
        .first()
    )
    return (row[0], row[1]) if row else (None, None)


def _remember_identity(
    db: Session,
    channel: Channel,
    msg_id: str,
    subscriber_id: str,
    subscriber_identifier: str,
    subscriber_id_type: str,
    user_name: str,
    data: dict,
    user_last_name: str | None = None,
) -> None:
    """#16: persist the client identity from a service message we don't forward.

    `CONVERSATION_STARTED` ("/start") carries the client's name and is the first
    thing a new client sends. Forwarding it to Bitrix would litter the dialog,
    but dropping it silently costs us the only name a MAX-only client ever has.
    Best-effort: a failure here must not break the webhook."""
    if not subscriber_identifier:
        return
    if msg_id and db.query(Message).filter_by(
        channel_id=channel.id, direction="incoming", max_message_id=msg_id
    ).first():
        return
    try:
        db.add(Message(
            channel_id=channel.id,
            direction="incoming",
            content_type="CONVERSATION_STARTED",
            max_message_id=msg_id or None,
            subscriber_identifier=subscriber_identifier,
            subscriber_id_type=subscriber_id_type,
            subscriber_user_id=subscriber_id or subscriber_identifier,
            user_name=user_name,
            user_last_name=user_last_name,
            raw_payload=json.dumps(data, ensure_ascii=False)[:2000],
        ))
        db.commit()
        logger.info(
            "Incoming: CONVERSATION_STARTED — remembered identity %r for %s (channel %s)",
            user_name, subscriber_identifier, channel.id,
        )
    except Exception as e:
        db.rollback()
        logger.warning("Incoming: failed to remember identity: %s", e)


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
    logger.info(
        "Incoming webhook (channel %s, type %s): %s",
        channel.id, channel.channel_type or "unknown", body[:500],
    )

    try:
        data = json.loads(body)
    except Exception:
        logger.error("Incoming: failed to parse JSON body")
        return JSONResponse({"error": "bad_request"}, status_code=400)

    # SEC-7: reject payloads that aren't a JSON object (would crash .get() below)
    if not isinstance(data, dict):
        logger.error("Incoming: payload is not a JSON object")
        return JSONResponse({"error": "bad_request"}, status_code=400)

    portal: Portal = channel.portal
    if not portal:
        logger.warning("Incoming: channel %s has no portal", channel.id)
        return JSONResponse({"status": "ok"})

    msg_content = data.get("messageContent")
    if not isinstance(msg_content, dict):
        msg_content = {}
    msg_type = msg_content.get("type")

    _ATTACHMENT_TYPES = {"IMAGE", "DOCUMENT", "AUDIO", "VIDEO", "VOICE"}

    subscriber = data.get("subscriber")
    if not isinstance(subscriber, dict):
        subscriber = {}
    subscriber_id = str(subscriber.get("id", ""))
    subscriber_identifier = str(subscriber.get("identifier", ""))
    subscriber_id_type = _identifier_type(subscriber, subscriber_identifier)

    user_info = data.get("userInfo")
    if not isinstance(user_info, dict):
        user_info = {}
    # #16: MAX channels send userInfo=null — reuse the name we already know for
    # this client from any channel of the portal before falling back to digits.
    raw_name = user_info.get("userName") or user_info.get("firstName")
    raw_last_name = user_info.get("lastName")
    if not raw_name:
        raw_name, raw_last_name = _known_user_name(db, channel, subscriber_identifier)
    # SEC-10: strip HTML/JS, SEC-7: cap to 255 chars
    user_name = sanitize_name(raw_name or subscriber_identifier)
    user_last_name = sanitize_name(raw_last_name) if raw_last_name else None
    user_phone = _subscriber_phone(subscriber)

    msg_id = str(data.get("id", ""))
    chat_id = bitrix_chat_id_for(channel, subscriber_identifier)
    logger.info(
        "Incoming: type=%s identifier=%s (%s) → chat_id=%s, user_name=%r",
        msg_type, subscriber_identifier, subscriber_id_type, chat_id, user_name,
    )

    # #16: not forwarded to Bitrix, but it is the only message carrying the name
    # of a client who writes to a MAX channel only.
    if msg_type == "CONVERSATION_STARTED":
        _remember_identity(
            db, channel, msg_id, subscriber_id, subscriber_identifier,
            subscriber_id_type, user_name, data, user_last_name,
        )
        return JSONResponse({"status": "ok"})

    if msg_type not in ("TEXT", "LOCATION", *_ATTACHMENT_TYPES):
        logger.info("Incoming: unsupported message type=%s, skipping", msg_type)
        return JSONResponse({"status": "ok"})

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
        attachment = msg_content.get("attachment")
        if not isinstance(attachment, dict):
            attachment = {}
        file_url = attachment.get("url")
        if not file_url:
            logger.warning("Incoming %s: missing attachment.url", msg_type)
            return JSONResponse({"status": "ok"})

        # name is null in edna payload — extract from URL path
        file_name = attachment.get("name") or urlparse(file_url).path.split("/")[-1] or "attachment"
        # SEC-10/SEC-7: strip HTML/JS and cap caption length
        caption = sanitize_text(msg_content.get("caption") or msg_content.get("text") or "") or None

        try:
            enqueue_incoming(
                db=db,
                channel=channel,
                msg_type="file",
                chat_id=chat_id,
                user_id=subscriber_id or subscriber_identifier,
                user_name=user_name,
                user_last_name=user_last_name,
                user_phone=user_phone,
                msg_id=msg_id,
                content_type=msg_type,
                subscriber_identifier=subscriber_identifier,
                subscriber_id_type=subscriber_id_type,
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
        loc = msg_content.get("location")
        if not isinstance(loc, dict):
            loc = {}
        lat = loc.get("latitude")
        lon = loc.get("longitude")
        if not lat or not lon:
            logger.warning("Incoming LOCATION: missing coordinates")
            return JSONResponse({"status": "ok"})

        maps_url = f"https://yandex.ru/maps/?pt={lon},{lat}&z=16&l=map"
        # SEC-10: address comes from the client — strip HTML/JS before embedding
        address = sanitize_text(loc.get("address") or "") or None
        text = f"📍 {address}\n{maps_url}" if address else f"📍 Местоположение: {maps_url}"

        try:
            enqueue_incoming(
                db=db,
                channel=channel,
                msg_type="location",
                chat_id=chat_id,
                user_id=subscriber_id or subscriber_identifier,
                user_name=user_name,
                user_last_name=user_last_name,
                user_phone=user_phone,
                msg_id=msg_id,
                content_type="LOCATION",
                subscriber_identifier=subscriber_identifier,
                subscriber_id_type=subscriber_id_type,
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
        # SEC-10: strip HTML/JS, SEC-7: cap to 4096 chars
        text = sanitize_text(msg_content.get("text") or "")
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
                user_last_name=user_last_name,
                user_phone=user_phone,
                msg_id=msg_id,
                content_type="TEXT",
                subscriber_identifier=subscriber_identifier,
                subscriber_id_type=subscriber_id_type,
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
