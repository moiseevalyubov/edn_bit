import ipaddress
import logging
import re
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Channel, Portal
from app.schemas import ChannelCreate, ChannelResponse, ChannelSaveResponse, OpenLineSet
from app.services.bitrix import activate_connector, bind_events, create_open_line, get_open_lines, register_connector

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def get_portal_or_404(member_id: str, db: Session) -> Portal:
    portal = db.query(Portal).filter_by(member_id=member_id).first()
    if not portal:
        raise HTTPException(status_code=404, detail="Портал не найден. Установите приложение.")
    return portal


@router.get("/channels", response_model=List[ChannelResponse])
def list_channels(member_id: str, db: Session = Depends(get_db)):
    portal = get_portal_or_404(member_id, db)
    return db.query(Channel).filter_by(portal_id=portal.id).order_by(Channel.connected_at.desc()).all()


@router.post("/channels", response_model=ChannelSaveResponse)
def create_channel(body: ChannelCreate, db: Session = Depends(get_db)):
    portal = get_portal_or_404(body.member_id, db)

    channel = Channel(
        portal_id=portal.id,
        name=body.name,
        api_key=body.api_key,
        sender=body.sender,
        is_active=True,
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)

    webhook_url = f"{settings.app_base_url}/incoming"
    return ChannelSaveResponse(channel=ChannelResponse.model_validate(channel), webhook_url=webhook_url)


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
        line_id = await create_open_line(portal, db, "MAX Bot")
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
    channel = db.query(Channel).filter_by(id=channel_id, portal_id=portal.id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Канал не найден")

    channel.is_active = False
    channel.disconnected_at = datetime.utcnow()
    db.commit()
    return {"success": True}
