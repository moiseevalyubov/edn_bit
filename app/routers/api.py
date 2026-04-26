import logging
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Channel, Portal
from app.schemas import ChannelCreate, ChannelResponse, ChannelSaveResponse, OpenLineSet
from app.services.bitrix import activate_connector, create_open_line, get_open_lines

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
def list_open_lines(member_id: str, db: Session = Depends(get_db)):
    portal = get_portal_or_404(member_id, db)
    try:
        lines = get_open_lines(portal, db)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка получения линий из Битрикс24: {e}")
    return {"lines": lines, "current_line_id": portal.open_line_id}


@router.post("/open-lines/create")
def create_line(member_id: str, db: Session = Depends(get_db)):
    portal = get_portal_or_404(member_id, db)
    try:
        line_id = create_open_line(portal, db, "MAX Bot")
        activate_connector(portal, db, line_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка создания линии: {e}")
    portal.open_line_id = line_id
    db.commit()
    return {"line_id": line_id}


@router.post("/portal/open-line")
def set_open_line(body: OpenLineSet, db: Session = Depends(get_db)):
    portal = get_portal_or_404(body.member_id, db)
    try:
        activate_connector(portal, db, body.line_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка активации коннектора: {e}")
    portal.open_line_id = body.line_id
    db.commit()
    return {"success": True}


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
