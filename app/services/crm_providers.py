"""Desired CRM providers follow existing channels; callers own local commits."""
from datetime import datetime, timedelta
import json
import logging
from urllib.parse import urlsplit

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.models import Channel, CrmMessageProvider, CrmOutgoingMessage, MessageDeliveryTask, Portal
from app.services.crm_auth import check_admin_context
from app.services.crm_bitrix import CrmBitrixError, call_crm_method
from app.services.crm_channels import outreach_capability
from app.services.crm_locks import operation_lock
from app.services.crm_outreach import set_desired_status
from app.services.task_types import SEND_CRM_TO_EDNA, SYNC_CRM_PROVIDER

logger = logging.getLogger(__name__)
RETRY_SCHEDULE = (1, 5, 30, 120, 600, 1800)


def cancel_queued_messages(db, channel_id):
    rows = db.query(CrmOutgoingMessage).filter_by(channel_id=channel_id, send_state="queued").with_for_update().all()
    for message in rows:
        message.send_state, message.last_error = "failed", "channel_unavailable"
        set_desired_status(db, message, "failed")
    if rows:
        db.query(MessageDeliveryTask).filter_by(channel_id=channel_id, task_type=SEND_CRM_TO_EDNA, status="pending").update(
            {"status": "failed", "last_error": "channel_unavailable", "updated_at": datetime.utcnow()}, synchronize_session=False)


def _enqueue(db, provider):
    db.flush()
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    db.execute(insert(MessageDeliveryTask).values(
        channel_id=provider.channel_id, task_type=SYNC_CRM_PROVIDER, direction="outgoing", status="pending",
        payload=json.dumps({"provider_id": provider.id, "revision": provider.desired_revision}),
        dedup_key=f"crm:provider:{provider.id}:{provider.desired_revision}", next_attempt_at=datetime.utcnow(),
    ).on_conflict_do_nothing())


def stage_provider(db, portal, channel, *, force=False, retry_failed=False):
    """Called under portal/channel locks, in the channel mutation transaction."""
    capability = outreach_capability(channel)
    present = bool(settings.crm_outreach_enabled and portal.crm_outreach_enabled and portal.uninstalled_at is None
                   and channel.is_active and capability)
    provider = db.query(CrmMessageProvider).filter_by(channel_id=channel.id, portal_id=portal.id).with_for_update().first()
    if not present:
        cancel_queued_messages(db, channel.id)
    if provider is None and not present:
        return None
    if provider is None:
        provider = CrmMessageProvider(portal_id=portal.id, channel_id=channel.id,
            provider_code=f"{capability.code_prefix}_{channel.id}", desired_revision=0, applied_revision=0, sync_state="absent")
        db.add(provider)
    state = "present" if present else "absent"
    name = f"{channel.name} в приложении {capability.label}" if capability else provider.desired_name
    handler = provider.desired_handler
    if present:
        base = urlsplit(settings.app_base_url)
        if base.scheme != "https" or not base.hostname or base.username or base.password or base.query or base.fragment:
            raise CrmBitrixError("invalid_app_base_url", False)
        handler = settings.app_base_url.rstrip("/") + "/crm/messages/send"
    changed = (provider.desired_state, provider.desired_name, provider.desired_handler) != (state, name, handler)
    recover = retry_failed and provider.sync_state == "error" and provider.next_attempt_at is None
    if changed or ((force or recover) and provider.sync_state != "pending"):
        provider.desired_state, provider.desired_name, provider.desired_handler = state, name, handler
        provider.desired_revision += 1
        provider.sync_state, provider.last_error = "pending", None
        provider.updated_at = datetime.utcnow()
        provider.next_attempt_at = datetime.utcnow()
        _enqueue(db, provider)
    return provider


def stage_portal(db, portal, *, force=False, retry_failed=False):
    channels = db.query(Channel).filter_by(portal_id=portal.id).order_by(Channel.id).with_for_update().all()
    for channel in channels:
        stage_provider(db, portal, channel, force=force, retry_failed=retry_failed)


def reconcile_local_providers(session_factory):
    """Startup repair/disable; no REST and no implicit enabling of an old portal."""
    with session_factory() as db:
        portals = db.query(Portal).filter(
            (Portal.crm_outreach_enabled.is_(True)) | Portal.id.in_(db.query(CrmMessageProvider.portal_id))
        ).order_by(Portal.id).with_for_update().all()
        for portal in portals:
            stage_portal(db, portal)
        db.commit()


async def _apply_remote(portal, db, provider_code, state, name, handler):
    if not await check_admin_context(portal, db, check_scope=True):
        raise CrmBitrixError("messageservice_scope_required", False)
    codes = await call_crm_method(portal, db, "messageservice.sender.list")
    if not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
        raise CrmBitrixError("invalid_sender_list", True)
    if state == "absent":
        if provider_code in codes:
            try:
                result = await call_crm_method(portal, db, "messageservice.sender.delete", {"CODE": provider_code})
            except CrmBitrixError as exc:
                if exc.code != "ERROR_SENDER_NOT_FOUND":
                    raise
            else:
                if result is not True:
                    raise CrmBitrixError("sender_delete_unsuccessful", True)
        return
    params = {"CODE": provider_code, "NAME": name, "HANDLER": handler,
              "DESCRIPTION": "Отправка сообщений из CRM через edna"}
    if provider_code not in codes:
        try:
            result = await call_crm_method(portal, db, "messageservice.sender.add", {**params, "TYPE": "SMS"})
        except CrmBitrixError as exc:
            if exc.code != "ERROR_SENDER_ALREADY_INSTALLED":
                raise
            # Already present is NOT evidence that NAME/HANDLER are current.
        else:
            if result is not True:
                raise CrmBitrixError("sender_add_unsuccessful", True)
    # Also updates the existing probe CODE, replacing its artificial handler.
    try:
        result = await call_crm_method(portal, db, "messageservice.sender.update", params)
    except CrmBitrixError as exc:
        if exc.code == "ERROR_SENDER_NOT_FOUND":
            raise CrmBitrixError("ERROR_SENDER_NOT_FOUND", True) from None
        raise
    if result is not True:
        raise CrmBitrixError("sender_update_unsuccessful", True)


async def execute_provider_task(task_id, session_factory):
    try:
        await _execute_provider_task(task_id, session_factory)
    except SQLAlchemyError:
        raise RuntimeError("CRM provider storage failed; retry by stale recovery") from None


async def _execute_provider_task(task_id, session_factory):
    with session_factory() as db:
        task = db.get(MessageDeliveryTask, task_id)
        if task is None or task.status != "processing":
            return
        try:
            data = json.loads(task.payload)
            provider_id = data.get("provider_id") if isinstance(data, dict) else None
            if type(provider_id) is not int or provider_id < 1:
                raise ValueError("Invalid provider ID")
        except (ValueError, TypeError):
            task.status, task.last_error = "dead", "invalid_provider_task"
            db.commit()
            return
        engine = db.get_bind()
    with operation_lock(engine, provider_id, namespace=220260503) as acquired:
        with session_factory() as db:
            task = db.get(MessageDeliveryTask, task_id)
            provider = db.get(CrmMessageProvider, provider_id)
            if provider is None or provider.channel_id != task.channel_id:
                task.status, task.last_error = "dead", "provider_missing"
                db.commit()
                return
            if not acquired:
                task.status = "pending"
                task.next_attempt_at = datetime.utcnow() + timedelta(seconds=1)
                db.commit()
                return
            # Same lock order as form mutations: portal -> channel -> provider -> messages.
            portal = db.query(Portal).filter_by(id=provider.portal_id).with_for_update().first()
            channel = db.query(Channel).filter_by(id=provider.channel_id, portal_id=provider.portal_id).with_for_update().first()
            if portal is None or channel is None:
                task.status, task.last_error = "dead", "provider_owner_missing"
                db.commit()
                return
            db.refresh(provider)
            stage_provider(db, portal, channel)
            if portal.uninstalled_at is not None:
                provider.sync_state, provider.last_error = "absent", "portal_uninstalled"
                task.status, task.last_error = "failed", "portal_uninstalled"
                db.commit()
                return  # Never call REST using an uninstalled app's OAuth.
            if provider.applied_revision >= provider.desired_revision:
                task.status = "sent"
                db.commit()
                return
            revision, state = provider.desired_revision, provider.desired_state
            code, name, handler = provider.provider_code, provider.desired_name, provider.desired_handler
            db.commit()  # Do not hold rows during HTTP or OAuth refresh.
            try:
                await _apply_remote(portal, db, code, state, name, handler)
            except CrmBitrixError as exc:
                db.rollback()
                provider = db.query(CrmMessageProvider).filter_by(id=provider_id).with_for_update().one()
                task = db.get(MessageDeliveryTask, task_id)
                task.retry_count += 1
                task.last_error = exc.code
                task.updated_at = datetime.utcnow()
                if exc.retryable and task.retry_count <= len(RETRY_SCHEDULE):
                    task.status = "pending"
                    task.next_attempt_at = datetime.utcnow() + timedelta(seconds=RETRY_SCHEDULE[task.retry_count - 1])
                else:
                    task.status = "dead"
                if provider.desired_revision == revision:
                    provider.sync_state, provider.last_error = "error", exc.code
                    provider.next_attempt_at = task.next_attempt_at if task.status == "pending" else None
                db.commit()
                logger.warning("CRM provider %s sync failed (%s)", provider_id, exc.code)
                return
            db.expire_all()
            provider = db.query(CrmMessageProvider).filter_by(id=provider_id).with_for_update().one()
            task = db.get(MessageDeliveryTask, task_id)
            provider.applied_revision = max(provider.applied_revision, revision)
            if provider.desired_revision == revision:
                provider.sync_state = "ready" if state == "present" else "absent"
                provider.last_error, provider.next_attempt_at = None, None
            task.status, task.last_error, task.updated_at = "sent", None, datetime.utcnow()
            db.commit()
            logger.info("CRM provider %s revision %s applied", provider_id, revision)
