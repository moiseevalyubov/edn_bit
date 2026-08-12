"""Вебхук edna о смене статуса сообщения (`statusCallbackUrl`).

Что приходит (проверено на живых каналах 2026-08-12):

| | MAX Bot | MAX |
|---|---|---|
| сразу после отправки | `SENT` | `SENT` |
| после реального прочтения | — | `READ`, секунд через 15 |

`DELIVERED` не приходит ни в одном из каналов, хотя в документации значится.

**Метку «Просмотрено» в Битриксе ставим только по `READ`.** В открытых линиях
других состояний нет: обработав «доставлено», Битрикс тут же помечает сообщение
прочитанным, то есть выбор всегда между «Просмотрено» и ничем. `SENT` значит лишь
«edna приняла и передала в MAX» — метка по нему была бы неправдой.

Разделять каналы в коде при этом не нужно: мы реагируем на факт прочтения, а не
на тип канала. В MAX Bot прочтения просто не существует, поэтому там метки и не
будет — тишина получается сама собой, а не особым случаем. Если edna когда-нибудь
научится сообщать прочтение и для MAX Bot, метка появится без правок.

edna ждёт от нас 200 и при неудаче повторяет всего три раза — через 4 с, 128 с и
2048 с. Второй шанс приходит очень нескоро, поэтому отвечаем 200 всегда, когда
токен верный, а о проблемах пишем в лог.
"""

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Channel, Message, Portal, bitrix_chat_id_for
from app.services.bitrix import send_delivery_status
from app.services.delivery_worker import notify_outgoing_undelivered
from app.services.rate_limiter import rate_limiter

logger = logging.getLogger(__name__)
router = APIRouter()

# Финальные статусы «клиент этого не получит». UNDELIVERED и FAILED видели живьём
# у MAX Bot; CANCELLED и EXPIRED описаны в документации edna.
FAILED_STATUSES = {"UNDELIVERED", "FAILED", "CANCELLED", "EXPIRED"}


def _ok() -> JSONResponse:
    """Новый ответ на каждый вызов: общий объект копил бы заголовки middleware."""
    return JSONResponse({"status": "ok"})


@router.api_route("/status", methods=["GET", "HEAD"])
@router.api_route("/status/{webhook_token}", methods=["GET", "HEAD"])
async def status_verify(webhook_token: str = ""):
    """edna проверяет доступность URL перед тем, как его сохранить."""
    return JSONResponse({"status": "ok"})


@router.post("/status/{webhook_token}")
async def status_callback(webhook_token: str, request: Request, db: Session = Depends(get_db)):
    if not rate_limiter.is_allowed(f"st:{webhook_token}"):
        return JSONResponse({"error": "too_many_requests"}, status_code=429)

    # SEC-1: тот же токен, что и у входящих сообщений — чужой адрес сюда не достучится.
    channel = db.query(Channel).filter_by(webhook_token=webhook_token, is_active=True).first()
    if not channel:
        logger.warning("Status: invalid webhook token %s", webhook_token[:8] + "...")
        return JSONResponse({"error": "forbidden"}, status_code=403)

    body = await request.body()
    logger.info(
        "Status webhook (channel %s, type %s): %s",
        channel.id, channel.channel_type or "unknown", body[:500],
    )

    try:
        data = json.loads(body)
    except Exception:
        # Повтор такого же тела ничего не изменит — отвечаем 200, чтобы edna не
        # тратила свои три попытки впустую.
        logger.error("Status: не удалось разобрать JSON")
        return _ok()
    if not isinstance(data, dict):
        logger.error("Status: тело не является JSON-объектом")
        return _ok()

    # requestId вебхука = outMessageId из ответа на отправку. Собственный messageId
    # edna к нашим данным не привязан и для сопоставления бесполезен.
    request_id = str(data.get("requestId") or "")
    status_value = str(data.get("status") or "").upper()
    if not request_id:
        logger.warning("Status %s: вебхук без requestId — сопоставить не с чем", status_value or "?")
        return _ok()

    message = (
        db.query(Message)
        .filter_by(channel_id=channel.id, edna_request_id=request_id)
        .first()
    )
    if not message:
        # Обычное дело для сообщений, отправленных до появления этой связки.
        logger.info("Status %s: сообщение requestId=%s не найдено", status_value, request_id)
        return _ok()

    if status_value in FAILED_STATUSES:
        await _notify_undelivered(channel, message, status_value)
        return _ok()

    if status_value != "READ":
        # SENT — промежуточный: сообщение ушло в MAX, но клиент его ещё не открыл.
        logger.info("Status %s для сообщения %s — метка не ставится", status_value, message.id)
        return _ok()

    await _mark_read(db, channel, message)
    return _ok()


async def _mark_read(db: Session, channel: Channel, message: Message) -> None:
    """Поставить «Просмотрено» под сообщением оператора."""
    portal: Portal = channel.portal
    # bitrix_chat_id хранит im_chat_id — название историческое, см. models.Message.
    if not (portal and message.line_id and message.bitrix_chat_id and message.im_message_id):
        logger.warning(
            "Status READ: сообщению %s не хватает данных открытой линии — метка пропущена",
            message.id,
        )
        return
    try:
        await send_delivery_status(
            portal=portal,
            db=db,
            line_id=int(message.line_id),
            bitrix_chat_id=int(message.bitrix_chat_id),
            bitrix_message_id=int(message.im_message_id),
            # Битрикс знает этот диалог по «помеченному» id, а не по голому.
            chat_id=bitrix_chat_id_for(channel, message.subscriber_identifier or ""),
        )
        logger.info("Status READ: сообщение %s помечено просмотренным", message.id)
    except Exception as e:
        # Метка — приятное дополнение; её потеря не повод отвечать edna ошибкой.
        logger.warning("Status READ: не удалось поставить метку: %s: %s", type(e).__name__, e)


async def _notify_undelivered(channel: Channel, message: Message, status_value: str) -> None:
    """Сказать оператору, что сообщение до клиента не дошло.

    Переиспользуем уведомление из задачи #2. Раньше оно срабатывало только когда
    падал НАШ запрос к edna; теперь ловим и случай, когда запрос прошёл, а клиенту
    сообщение так и не доставили — раньше об этом никто не узнавал."""
    payload = {
        "max_id": message.subscriber_identifier,
        "bitrix_chat_id": bitrix_chat_id_for(channel, message.subscriber_identifier or ""),
        "line_id": message.line_id,
        "msg_type": "media" if (message.content_type or "TEXT").upper() != "TEXT" else "text",
    }
    try:
        await notify_outgoing_undelivered(channel.id, payload, f"edna status {status_value}")
    except Exception as e:
        logger.warning("Status %s: уведомление о недоставке не ушло: %s", status_value, e)
