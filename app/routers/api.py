import ipaddress
import logging
import re
import uuid
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Channel, Portal
from app.schemas import ChannelCreate, ChannelResponse, ChannelSaveResponse, OpenLineSet
from app.services.bitrix import activate_connector, bind_events, create_open_line, get_open_lines, register_connector
from app.services.maxbot import WebhookSetupError, configure_incoming_webhook

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def get_portal_or_404(member_id: str, db: Session) -> Portal:
    portal = db.query(Portal).filter_by(member_id=member_id).first()
    if not portal:
        raise HTTPException(status_code=404, detail="Портал не найден. Установите приложение.")
    return portal


def get_channel_for_portal(channel_id: int, portal: Portal, db: Session) -> Channel:
    channel = db.query(Channel).filter_by(id=channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Канал не найден")
    if channel.portal_id != portal.id:
        raise HTTPException(status_code=403, detail="Канал не принадлежит этому порталу")
    return channel


@router.get("/portal/status")
def portal_status(member_id: str, db: Session = Depends(get_db)):
    portal = get_portal_or_404(member_id, db)
    return {"payment_required": portal.payment_required_at is not None}


@router.get("/channels", response_model=List[ChannelResponse])
def list_channels(member_id: str, db: Session = Depends(get_db)):
    portal = get_portal_or_404(member_id, db)
    return db.query(Channel).filter_by(portal_id=portal.id).order_by(Channel.connected_at.desc()).all()


@router.post("/channels", response_model=ChannelSaveResponse)
async def create_channel(body: ChannelCreate, db: Session = Depends(get_db)):
    portal = get_portal_or_404(body.member_id, db)

    duplicate_sender = db.query(Channel).filter_by(
        portal_id=portal.id, sender=body.sender, is_active=True
    ).first()
    if duplicate_sender:
        raise HTTPException(status_code=422, detail={"field": "sender", "message": "Вы уже подключили этот канал"})

    duplicate_name = db.query(Channel).filter(
        Channel.portal_id == portal.id,
        func.lower(Channel.name) == body.name.strip().lower()
    ).first()
    if duplicate_name:
        raise HTTPException(status_code=422, detail={"field": "name", "message": "Название должно быть уникальным"})

    # Build the webhook URL up front. The token is just a random id — nothing is
    # persisted yet, so we can register it in edna BEFORE creating the channel and
    # abort cleanly if the channel turns out to be unusable.
    webhook_token = uuid.uuid4().hex
    webhook_url = f"{settings.app_base_url}/incoming/{webhook_token}"
    # Статусы доставки узнаёт тот же канал — токен один, отличается только путь.
    status_url = f"{settings.app_base_url}/status/{webhook_token}"

    # UX: auto-register the webhook in edna so the user doesn't copy-paste the URL.
    # - fatal failure (wrong Sender ID, invalid key 401, no access to subject 403,
    #   channel not active) → the channel can never work / can't be verified, so we
    #   abort WITHOUT creating it (422).
    # - transient failure (edna/network/proxy/5xx/cold start) → still create the
    #   channel and fall back to manual URL setup.
    auto_configured = False
    auto_error = None
    subject_id = None
    channel_type = None
    try:
        info = await configure_incoming_webhook(
            body.api_key, body.sender, webhook_url, status_url
        )
        subject_id = info["subject_id"]
        channel_type = info.get("type")
        auto_configured = True
    except WebhookSetupError as e:
        if e.fatal:
            logger.info("Channel (sender=%s): auto webhook setup blocked creation: %s", body.sender, e)
            raise HTTPException(status_code=422, detail=f"Автоматическая настройка не удалась: {e}")
        # Keep whatever the failed attempt already learned: the channel type decides
        # which edna endpoint replies go to, and guessing it later is worse than
        # having no webhook (which the user can still set manually).
        subject_id = e.subject_id
        channel_type = e.channel_type
        auto_error = str(e)
        logger.warning(
            "Channel (sender=%s): auto webhook setup failed (transient, type=%s): %s",
            body.sender, channel_type or "unknown", e,
        )
    except Exception as e:
        auto_error = "Не удалось связаться с edna для автоматической настройки. Укажите URL вручную."
        logger.warning(
            "Channel (sender=%s): unexpected auto webhook setup error: %s: %s",
            body.sender, type(e).__name__, e,
        )

    channel = Channel(
        portal_id=portal.id,
        name=body.name,
        api_key=body.api_key,
        sender=body.sender,
        webhook_token=webhook_token,
        subject_id=subject_id,
        channel_type=channel_type,
        is_active=True,
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)

    if auto_configured:
        logger.info("Channel %s: webhook auto-registered in edna (subjectId=%s)", channel.id, subject_id)

    return ChannelSaveResponse(
        channel=ChannelResponse.model_validate(channel),
        webhook_url=webhook_url,
        auto_configured=auto_configured,
        auto_error=auto_error,
    )


@router.get("/open-lines")
async def list_open_lines(member_id: str, db: Session = Depends(get_db)):
    portal = get_portal_or_404(member_id, db)
    try:
        lines = await get_open_lines(portal, db)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка получения линий из Битрикс24: {e}")
    return {"lines": lines, "current_line_id": portal.open_line_id}


@router.post("/open-lines/create")
async def create_line(member_id: str, db: Session = Depends(get_db)):
    portal = get_portal_or_404(member_id, db)
    try:
        line_id = await create_open_line(portal, db, "edna MAX")
        await activate_connector(portal, db, line_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка создания линии: {e}")
    portal.open_line_id = line_id
    db.commit()
    return {"line_id": line_id}


@router.post("/portal/open-line")
async def set_open_line(body: OpenLineSet, db: Session = Depends(get_db)):
    portal = get_portal_or_404(body.member_id, db)
    try:
        await activate_connector(portal, db, body.line_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка активации коннектора: {e}")
    portal.open_line_id = body.line_id
    db.commit()
    return {"success": True}


@router.post("/portal/repair-endpoint")
async def repair_endpoint(body: dict, db: Session = Depends(get_db)):
    member_id = body.get("member_id", "")
    domain = body.get("domain", "")
    if not member_id or not domain:
        raise HTTPException(status_code=400, detail="member_id и domain обязательны")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9\-\.]{0,252}[a-zA-Z0-9]", domain):
        raise HTTPException(status_code=400, detail="Недопустимое значение domain")
    bare = domain.split(":")[0].lower()
    if bare == "localhost" or bare.endswith(".local"):
        raise HTTPException(status_code=400, detail="Недопустимое значение domain")
    try:
        ipaddress.ip_address(bare)
        raise HTTPException(status_code=400, detail="Недопустимое значение domain")
    except ValueError:
        pass
    portal = db.query(Portal).filter_by(member_id=member_id).first()
    if not portal:
        raise HTTPException(status_code=404, detail="Портал не найден")
    if not portal.client_endpoint:
        portal.client_endpoint = f"https://{domain}/rest/"
        db.commit()
        logger.info("Repaired client_endpoint for %s: %s", member_id, portal.client_endpoint)
    try:
        await register_connector(portal, db)
        await bind_events(portal, db)
        logger.info("Re-registered connector and events for %s", member_id)
    except Exception as e:
        logger.warning("Re-registration failed (non-critical): %s", e)
    return {"client_endpoint": portal.client_endpoint}


@router.post("/channels/{channel_id}/disconnect")
def disconnect_channel(channel_id: int, member_id: str, db: Session = Depends(get_db)):
    portal = get_portal_or_404(member_id, db)
    channel = get_channel_for_portal(channel_id, portal, db)
    channel.is_active = False
    channel.disconnected_at = datetime.utcnow()
    db.commit()
    return {"success": True}
