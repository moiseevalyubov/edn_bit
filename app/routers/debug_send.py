"""ВРЕМЕННЫЙ служебный эндпоинт для опыта с дублями исходящих (2026-08-12).

Что выяснено к этому моменту: одно обращение нашего сервера к edna порождает
**два** сообщения у клиента, а точно такой же запрос из Postman — одно
(15 отправок подряд, ни одного дубля). Тело, ключ, канал и адрес одинаковые,
код отправляет ровно один раз — проверено по логам и по коду.

Значит различие либо в **виде** нашего запроса (заголовки, время жизни
соединения), либо в **маршруте** — наш сервер стоит в Москве, Postman у
пользователя. Из Postman это не проверить: нужно слать с того же сервера, но
по-разному. Отсюда варианты ниже.

Как пользоваться (токен — тот же, что у входящих и статусов этого канала):

    POST /debug/edna/{webhook_token}
    {"to": "194089586", "text": "vA-1", "variant": "a"}

Прогнать каждый вариант раз по пять, сообщения **не читать** минуту-две,
затем сравнить в логах, сколько разных requestId пришло на /status.

- `a` — как в бою: новый клиент на каждый запрос, минимум заголовков.
- `b` — тот же клиент, но заголовки как у Postman.
- `c` — общий клиент с постоянным соединением (keep-alive), не закрываем.

Удалить вместе с `_log_exchange`, когда причина найдётся.
"""

import json
import logging
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Channel, is_max_bot_channel
from app.services.maxbot import MAXBOT_API_URL, MAX_API_URL

logger = logging.getLogger(__name__)
router = APIRouter()

# Заголовки, которые Postman добавляет сам. Нас интересует, есть ли среди них
# тот, из-за которого edna ведёт себя иначе.
_POSTMAN_HEADERS = {
    "User-Agent": "PostmanRuntime/7.39.0",
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}

_shared_client: httpx.AsyncClient | None = None


def _get_shared_client() -> httpx.AsyncClient:
    """Вариант `c`: одно соединение на все запросы, как держит его Postman."""
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(timeout=10)
    return _shared_client


@router.post("/debug/edna/{webhook_token}")
async def debug_send(webhook_token: str, request: Request, db: Session = Depends(get_db)):
    channel = db.query(Channel).filter_by(webhook_token=webhook_token, is_active=True).first()
    if not channel:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    try:
        data = json.loads(await request.body())
    except Exception:
        return JSONResponse({"error": "bad_json"}, status_code=400)

    to = str(data.get("to") or "")
    text = str(data.get("text") or "debug")
    variant = str(data.get("variant") or "a").lower()
    if not to:
        return JSONResponse({"error": "no_to"}, status_code=400)

    if is_max_bot_channel(channel):
        url = MAXBOT_API_URL
        payload = {"sender": channel.sender, "maxId": to, "content": {"type": "TEXT", "text": text}}
    else:
        url = MAX_API_URL
        payload = {
            "from": channel.sender,
            "to": {"value": to, "type": "MAX_ID"},
            "content": {"type": "TEXT", "text": text},
        }

    headers = {"Content-Type": "application/json", "X-API-KEY": channel.api_key}
    if variant == "b":
        headers = {**_POSTMAN_HEADERS, **headers, "Postman-Token": str(uuid.uuid4())}

    started = time.monotonic()
    if variant == "c":
        response = await _get_shared_client().post(url, headers=headers, json=payload)
    else:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload, timeout=10)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    logger.info(
        "DEBUG вариант %s: %s %s за %d мс → %s",
        variant, url, payload, elapsed_ms, response.text[:300],
    )
    return JSONResponse({
        "variant": variant,
        "status_code": response.status_code,
        "elapsed_ms": elapsed_ms,
        "edna": response.text[:300],
    })
