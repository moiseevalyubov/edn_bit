"""ВРЕМЕННО — проба 3 по задаче #22 (review.md, раздел «⭐ #22»). Удалить после пробы.

Регистрирует канал MAX провайдером сообщений Битрикса, чтобы он появился в блоке
«Сообщение» карточки CRM, и записывает в лог всё, что Битрикс присылает при
отправке. Клиенту в MAX ничего не уходит. Сообщению в карточке ставится статус
«доставлено» — чтобы увидеть, как статус выглядит.

Работает только при переменной окружения PROBE_22=1 (задаётся на стенде Amvera);
без неё все адреса отвечают 404, так что у живых клиентов ничего не меняется.
"""
import asyncio
import json
import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.database import SessionLocal
from app.models import Channel, Portal
from app.routers.handler import _parse_php_form
from app.services.bitrix import call_bitrix

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/probe22")

_SECRET_AUTH_KEYS = {"access_token", "refresh_token", "application_token"}


def _require_enabled() -> None:
    if settings.probe_22 != "1":
        raise HTTPException(status_code=404)


def _sender_code(channel: Channel) -> str:
    return f"edna_max_{channel.id}"


def _active_channel(db, sender: str) -> Channel:
    channel = (
        db.query(Channel)
        .filter_by(sender=sender, is_active=True)
        .order_by(Channel.connected_at.desc())
        .first()
    )
    if not channel or not channel.portal:
        raise HTTPException(status_code=404, detail="Активный канал с таким sender не найден")
    return channel


async def _call(portal: Portal, db, method: str, params: dict) -> dict:
    """Вызов Битрикса, который возвращает ошибку текстом, а не падает, — для пробы
    сам текст ошибки и есть результат."""
    try:
        return await call_bitrix(portal, db, method, params)
    except Exception as e:
        response = getattr(e, "response", None)
        return {"error": str(e), "body": response.text[:500] if response is not None else None}


@router.get("/register")
async def register(sender: str):
    _require_enabled()
    db = SessionLocal()
    try:
        channel = _active_channel(db, sender)
        added = await _call(channel.portal, db, "messageservice.sender.add", {
            "CODE": _sender_code(channel),
            "TYPE": "SMS",
            "HANDLER": f"{settings.app_base_url}/probe22/send",
            "NAME": f"MAX от edna — {channel.name}",
            "DESCRIPTION": "Проба #22: сообщение в MAX по номеру телефона",
        })
        listed = await _call(channel.portal, db, "messageservice.sender.list", {})
    finally:
        db.close()
    logger.info("Probe22 register %s: add=%s list=%s", sender, added, listed)
    return {"add": added, "list": listed}


@router.get("/unregister")
async def unregister(sender: str):
    _require_enabled()
    db = SessionLocal()
    try:
        channel = _active_channel(db, sender)
        deleted = await _call(channel.portal, db, "messageservice.sender.delete", {
            "CODE": _sender_code(channel),
        })
        listed = await _call(channel.portal, db, "messageservice.sender.list", {})
    finally:
        db.close()
    logger.info("Probe22 unregister %s: delete=%s list=%s", sender, deleted, listed)
    return {"delete": deleted, "list": listed}


async def _mark_delivered(member_id: str, code: str, message_id: str) -> None:
    # Ждём, пока Битрикс сохранит сообщение у себя: обработчик зовётся до этого.
    await asyncio.sleep(3)
    db = SessionLocal()
    try:
        portal = db.query(Portal).filter_by(member_id=member_id).first()
        if not portal:
            logger.warning("Probe22: портал %s не найден — статус не ставим", member_id)
            return
        result = await _call(portal, db, "messageservice.message.status.update", {
            "CODE": code,
            "MESSAGE_ID": message_id,
            "STATUS": "delivered",
        })
        logger.info("Probe22 status.update %s: %s", message_id, result)
    finally:
        db.close()


@router.post("/send")
async def send(request: Request, background_tasks: BackgroundTasks):
    _require_enabled()
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        data = _parse_php_form(parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True))
    else:
        try:
            data = json.loads(body)
        except Exception:
            data = None
    if not isinstance(data, dict):
        data = {"raw": body.decode("utf-8", errors="replace")[:2000]}

    auth = data.get("auth") if isinstance(data.get("auth"), dict) else {}
    safe = {**data, "auth": {k: ("***" if k in _SECRET_AUTH_KEYS else v) for k, v in auth.items()}}
    logger.info("Probe22 send (%s): %s", content_type, json.dumps(safe, ensure_ascii=False)[:3000])

    member_id = auth.get("member_id")
    db = SessionLocal()
    try:
        portal = db.query(Portal).filter_by(member_id=member_id).first() if member_id else None
        token_ok = bool(portal) and (not portal.app_token or portal.app_token == auth.get("application_token"))
    finally:
        db.close()
    if not token_ok:
        logger.warning("Probe22 send: портал %s не найден или токен приложения не совпал", member_id)
        return JSONResponse({"error": "forbidden"}, status_code=403)

    if data.get("code") and data.get("message_id"):
        background_tasks.add_task(_mark_delivered, member_id, data["code"], data["message_id"])
    return {"status": "ok"}
