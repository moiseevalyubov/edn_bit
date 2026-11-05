"""Automatic preparation on opening settings; no user registration switch."""
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Channel, CrmMessageProvider, CrmOutgoingMessage, Portal
from app.services.crm_auth import bearer_token, check_admin_context, require_channel_admin
from app.services.crm_bitrix import CrmBitrixError
from app.services.crm_providers import stage_portal

router = APIRouter(prefix="/api/crm")


class PortalRequest(BaseModel):
    member_id: str


def _portal(member_id, db):
    portal = db.query(Portal).filter_by(member_id=member_id).first()
    if portal is None or portal.uninstalled_at is not None:
        raise HTTPException(404, "Приложение не установлено на этом портале")
    return portal


def diagnostic_snapshot(db, portal):
    providers = db.query(CrmMessageProvider).filter_by(portal_id=portal.id).all()
    uncertain = db.query(CrmOutgoingMessage, Channel.name).join(Channel, Channel.id == CrmOutgoingMessage.channel_id).filter(
        CrmOutgoingMessage.portal_id == portal.id, CrmOutgoingMessage.send_state == "uncertain",
    ).order_by(CrmOutgoingMessage.created_at.desc()).limit(50).all()
    return {
        "providers": [{"channel_id": row.channel_id, "state": row.sync_state, "desired_state": row.desired_state,
                       "error": row.last_error} for row in providers],
        "uncertain": [{"channel": name, "message_id": row.bitrix_message_id, "out_message_id": row.edna_out_message_id,
                       "started_at": row.attempt_started_at, "reason": row.last_error} for row, name in uncertain],
    }


@router.post("/prepare")
async def prepare(body: PortalRequest, db: Session = Depends(get_db), authorization: str | None = Header(default=None)):
    portal = _portal(body.member_id, db)
    if not settings.crm_outreach_enabled:
        return {"enabled": False, "state": "disabled"}
    token = bearer_token(authorization)
    try:
        granted = await check_admin_context(portal, db, token, check_scope=True)
    except CrmBitrixError as exc:
        if exc.retryable:
            raise HTTPException(503, "Не удалось проверить подключение к Битрикс24. Повторите позже") from None
        if exc.code == "expected_app_code_missing":
            raise HTTPException(503, "Отправка из CRM ещё не настроена администратором сервера") from None
        raise HTTPException(403, "Откройте настройки от имени администратора приложения") from None
    state = "ready"
    if not granted:
        state = "scope_required"
    elif not portal.app_token:
        state = "app_token_required"
    else:
        try:
            # UI token is never persisted in place of installation/admin OAuth.
            if not await check_admin_context(portal, db, check_scope=True):
                state = "server_authorization_required"
        except CrmBitrixError as exc:
            if exc.retryable:
                raise HTTPException(503, "Не удалось проверить подключение сервера к Битрикс24. Повторите позже") from None
            state = "server_authorization_required"
    portal = db.query(Portal).filter_by(id=portal.id).populate_existing().with_for_update().one()
    if portal.uninstalled_at is not None:
        raise HTTPException(409, "Приложение было удалено во время проверки")
    portal.crm_outreach_enabled = state == "ready"
    try:
        stage_portal(db, portal, force=True)
    except CrmBitrixError:
        db.rollback()
        raise HTTPException(503, "Администратору сервера нужно проверить настройки отправки из CRM") from None
    db.commit()
    return {"enabled": portal.crm_outreach_enabled, "state": state, **diagnostic_snapshot(db, portal)}


@router.post("/diagnostics")
async def diagnostics(body: PortalRequest, db: Session = Depends(get_db), authorization: str | None = Header(default=None)):
    portal = _portal(body.member_id, db)
    await require_channel_admin(portal, db, authorization, always=True)
    return diagnostic_snapshot(db, portal)
