"""Вебхук edna о смене статуса сообщения (`statusCallbackUrl`).

Пока эндпоинт только принимает и логирует тело: до тех пор, пока адрес не прописан
в edna, сюда ничего не приходит, и на работу приложения он не влияет.

Что известно из документации edna (docs-pulse, раздел callback/get-status):
тело содержит `requestId`, `messageId`, `subject`, `subjectId`, `status`, `statusAt`,
`error`; статусы — SENT / DELIVERED / READ / UNDELIVERED / CANCELLED / EXPIRED / FAILED.
При ответе не 200 edna повторяет попытку три раза (через 4 с, 128 с и 2048 с) — то есть
второй шанс придёт очень нескоро, поэтому отвечаем 200 всегда, когда токен верный.

Открытый вопрос, ради которого этот лог и заведён: совпадает ли `messageId` отсюда
с `outMessageId` из ответа на отправку. Матчить статус с сообщением больше нечем.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Channel
from app.services.rate_limiter import rate_limiter

logger = logging.getLogger(__name__)
router = APIRouter()


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
    return JSONResponse({"status": "ok"})
