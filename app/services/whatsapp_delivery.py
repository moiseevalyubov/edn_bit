"""Durable WhatsApp operations using the existing queue and schema.

No WhatsApp credentials or raw webhook/event bodies are stored in task payloads.
API assumptions WA-A02..A06: docs/WHATSAPP_MESSENGER_PROGRESS.md.
"""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import logging
import uuid
from urllib.parse import urlsplit, parse_qsl

from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models import Channel, Message, MessageDeliveryTask, Portal
from app.services import whatsapp as wa
from app.services.crm_locks import operation_lock
from app.services.sanitize import sanitize_name, sanitize_text

logger = logging.getLogger(__name__)
RETRY = (1, 5, 30, 120, 600, 1800)
# WA-A02: attachments await real incoming samples. Never drop the appeal.
UNSUPPORTED = "Клиент отправил вложение. Его отображение в WhatsApp пока не поддерживается. Попросите клиента написать текстом."


def _json(data):
    return json.dumps(data, ensure_ascii=False)


def _task(db, channel_id, kind, key, payload):
    existing = db.query(MessageDeliveryTask).filter_by(dedup_key=key).first()
    if existing:
        if kind in {wa.RECEIVE, wa.SEND}:
            # Another request committed between the caller's lookup and flush.
            # Roll back its newly inserted Message rather than keep an orphan.
            raise wa.WhatsAppError("whatsapp_commit_conflict", retryable=True)
        return existing
    task = MessageDeliveryTask(channel_id=channel_id, task_type=kind,
                               direction="incoming" if kind == wa.RECEIVE else "outgoing",
                               dedup_key=key, payload=_json(payload), status="pending")
    db.add(task)
    return task


def _commit(db):
    try:
        db.commit()
    except IntegrityError:
        # Roll back the entire batch. A retry validates the winner's content too;
        # accepting a partial concurrent batch here could silently lose its tail.
        db.rollback()
        raise wa.WhatsAppError("whatsapp_commit_conflict", retryable=True) from None


def _flush(db):
    try:
        db.flush()
    except IntegrityError:
        # The dedup index may reject a concurrent receipt before commit, too.
        db.rollback()
        raise wa.WhatsAppError("whatsapp_commit_conflict", retryable=True) from None


def _time(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo:
                return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            pass
    return datetime.utcnow()


def _dialog_title(message):
    if not _has_client_name(message):
        return "WhatsApp " + sanitize_name(message.subscriber_identifier).replace("[", "［").replace("]", "］")
    return " ".join(value for value in (message.user_name, message.user_last_name) if value)


def _has_client_name(message):
    # Generated titles are never stored as a person's name.
    return bool(message.user_name)


def accept_incoming(db, channel, data):
    if not channel.is_active or not channel.portal or channel.portal.uninstalled_at is not None:
        raise wa.WhatsAppError("channel_unavailable")
    if not channel.portal.open_line_id:
        raise wa.WhatsAppError("open_line_required", retryable=True)
    wa.check_subject(channel, data)
    msg_id = wa.scalar(data.get("id"))
    key = "wa:in:%s:%s" % (channel.id, hashlib.sha256(msg_id.encode()).hexdigest())
    if db.query(MessageDeliveryTask).filter_by(dedup_key=key).first():
        return
    address, phone = wa.incoming_identity(data)
    user = data.get("userInfo") if isinstance(data.get("userInfo"), dict) else {}
    previous = db.query(Message).filter_by(channel_id=channel.id, direction="incoming",
                                          subscriber_identifier=address).order_by(Message.id.desc()).first()
    name = sanitize_name(user.get("firstName") or user.get("displayName") or user.get("userName"))
    # WA-A02: only userInfo names; do not invent a WhatsApp username field.
    if name:
        # Do not attach a surname from an older, different display identity.
        last_name, name_source = sanitize_name(user.get("lastName")) or None, "userInfo"
    elif previous and previous.user_name:
        name, last_name, name_source = previous.user_name, previous.user_last_name, "history"
    else:
        name, last_name, name_source = "", None, "identifier"
    name = name.replace("[", "［").replace("]", "］")
    last_name = last_name.replace("[", "［").replace("]", "］") if last_name else None
    content = data.get("messageContent")
    if not isinstance(content, dict):
        raise wa.WhatsAppError("message_content_required")
    raw_text = content.get("text")
    if content.get("type") != "TEXT":
        caption = content.get("caption")
        parts = [part for part in (caption, raw_text) if isinstance(part, str) and part]
        raw_text = "\n".join(dict.fromkeys(parts))
    text = sanitize_text(raw_text).replace("[", "［").replace("]", "］") if isinstance(raw_text, str) else ""
    stored_type = "TEXT"
    if content.get("type") != "TEXT" or not text:
        text = (text + "\n" if text else "") + UNSUPPORTED
    elif len(raw_text) > wa.MAX_TEXT_LENGTH:
        text += "\nТекст сокращён. Попросите клиента прислать продолжение."
    message = Message(channel_id=channel.id, direction="incoming", text=text, content_type=stored_type,
                      max_message_id=msg_id, subscriber_identifier=address, subscriber_id_type="WHATSAPP_MESSENGER_ID",
                      subscriber_user_id=wa.user_id(channel, address), user_name=name or None, user_last_name=last_name,
                      bitrix_chat_id=wa.chat_id(channel, address), line_id=str(channel.portal.open_line_id),
                      sent_at=_time(data.get("receivedAt")))
    db.add(message)
    _flush(db)
    _task(db, channel.id, wa.RECEIVE, key, {"message_id": message.id, "phone": phone})
    if previous is not None and name_source != "identifier" and (name, last_name) != (previous.user_name, previous.user_last_name):
        _task(db, channel.id, wa.RENAME, "wa:rename:%s" % message.id,
              {"message_id": message.id, "receive_key": key})
    saved_channel_id, saved_message_id = channel.id, message.id
    _commit(db)
    _log_incoming((saved_channel_id, saved_message_id, msg_id, name_source))
    return saved_channel_id, saved_message_id, msg_id, name_source


def _log_incoming(saved):
    if saved:
        logger.info("WhatsApp incoming saved: channel=%s message_id=%s external_id=%s name_source=%s", *saved)


def accept_outgoing(db, portal, data):
    if portal.uninstalled_at is not None:
        raise wa.WhatsAppError("channel_unavailable")
    if not portal.app_token:
        raise wa.WhatsAppError("app_token_required")
    event = data.get("data")
    if not isinstance(event, dict) or event.get("CONNECTOR") != wa.CONNECTOR:
        raise wa.WhatsAppError("wrong_connector")
    line = wa.scalar(event.get("LINE"))
    messages = event.get("MESSAGES")
    if not isinstance(messages, list) or not messages or len(messages) > 100:
        raise wa.WhatsAppError("invalid_messages")
    # Batch acceptance is transactional: no inner commits, including validation errors.
    for item in messages:
        if not isinstance(item, dict):
            raise wa.WhatsAppError("invalid_message")
        chat = item.get("chat") if isinstance(item.get("chat"), dict) else {}
        external_chat = wa.scalar(chat.get("id"), maximum=256)
        original = db.query(Message).join(Channel).filter(
            Channel.portal_id == portal.id, Channel.channel_type == wa.CHANNEL_TYPE,
            Message.direction == "incoming", Message.bitrix_chat_id == external_chat,
            Message.line_id == line,
        ).order_by(Message.id.desc()).first()
        if original is None:
            raise wa.WhatsAppError("unknown_whatsapp_dialog")
        channel = db.get(Channel, original.channel_id)
        # Reconnection may produce a new channel row; preserve known WhatsApp identity.
        active = db.query(Channel).filter_by(portal_id=portal.id, sender=channel.sender,
                                             channel_type=wa.CHANNEL_TYPE, is_active=True).all()
        if len(active) != 1:
            raise wa.WhatsAppError("channel_unavailable")
        channel = active[0]
        im = item.get("im") if isinstance(item.get("im"), dict) else {}
        im_id, im_chat = wa.scalar(im.get("message_id")), wa.scalar(im.get("chat_id"))
        if not im_id.isascii() or not im_id.isdecimal() or not im_chat.isascii() or not im_chat.isdecimal():
            raise wa.WhatsAppError("invalid_bitrix_identity")
        # Bitrix message id remains unique across reconnect and rename.
        key = "wa:out:%s:%s" % (portal.id, im_id)
        body = item.get("message") if isinstance(item.get("message"), dict) else {}
        from app.routers.handler import strip_bbcode
        raw = body.get("text")
        text = strip_bbcode(raw) if isinstance(raw, str) else ""
        content_hash = hashlib.sha256(_json([external_chat, line, im_chat, text, bool(body.get("files"))]).encode()).hexdigest()
        files = body.get("files")
        if files:
            if not isinstance(files, list) or any(not isinstance(f, dict) for f in files):
                raise wa.WhatsAppError("invalid_files")
            # Signed source URLs are transient credentials, not durable task data.
            # Their digest also detects a conflicting event with a reused IM id.
            fingerprints = []
            for file in files:
                try:
                    location = urlsplit(file.get("downloadLink") or file.get("link") or "")
                    source_id = [location.hostname, location.path,
                                 [(k, v) for k, v in parse_qsl(location.query) if k.lower() == "file_id"]]
                except (ValueError, TypeError):
                    raise wa.WhatsAppError("invalid_file_url") from None
                fingerprints.append([file.get("id"), file.get("name"), file.get("mime"), source_id])
            content_hash = hashlib.sha256(_json([external_chat, line, im_chat, text, fingerprints]).encode()).hexdigest()
        existing = db.query(MessageDeliveryTask).filter_by(dedup_key=key).first()
        if existing:
            if json.loads(existing.payload).get("content_hash") != content_hash:
                raise wa.WhatsAppError("message_id_conflict")
            continue
        message = Message(channel_id=channel.id, direction="outgoing", text=text, content_type="TEXT",
                          subscriber_identifier=original.subscriber_identifier, subscriber_id_type="WHATSAPP_MESSENGER_ID",
                          subscriber_user_id=original.subscriber_user_id, user_name=original.user_name,
                          user_last_name=original.user_last_name, bitrix_chat_id=im_chat,
                          im_message_id=im_id, line_id=line, edna_request_id=uuid.uuid4().hex)
        db.add(message)
        _flush(db)
        task = _task(db, channel.id, wa.SEND, key, {
            "message_id": message.id, "chat_id": external_chat, "content_hash": content_hash,
            "attempt_started": False,
        })
        if files or not text or len(text) > wa.MAX_TEXT_LENGTH:
            task.status, task.last_error = "failed", "whatsapp_text_only_or_invalid_text"
            enqueue_notice(db, message, "unsupported")
        _flush(db)  # Make duplicates inside this very batch visible with autoflush=False.
    _commit(db)


def accept_status(db, channel, data):
    # WA-A05: real status structure still awaits a live fixture; READ confirmed by owner.
    # Status receipts remain available after disconnection.
    wa.check_subject(channel, data)
    request_id = wa.scalar(data.get("requestId"))
    status = wa.scalar(data.get("status")).upper()
    if status not in {"SENT", "DELIVERED", "READ", *wa.FAILURES}:
        logger.info("WhatsApp status ignored: channel=%s request_id=%s status=%s", channel.id, request_id, status)
        return
    message = db.query(Message).filter_by(channel_id=channel.id, direction="outgoing", edna_request_id=request_id).first()
    if message is None:
        logger.warning("WhatsApp status not matched: channel=%s request_id=%s status=%s", channel.id, request_id, status)
        raise wa.WhatsAppError("whatsapp_request_unknown", retryable=True)
    key = "wa:status:%s:%s" % (message.id, status)
    _task(db, channel.id, wa.STATUS, key,
          {"message_id": message.id, "status": status,
           "date": int(_time(data.get("statusAt")).replace(tzinfo=timezone.utc).timestamp())})
    saved_channel_id, saved_message_id = channel.id, message.id
    _commit(db)
    logger.info("WhatsApp status saved: channel=%s message_id=%s request_id=%s status=%s",
                saved_channel_id, saved_message_id, request_id, status)


def enqueue_notice(db, message, reason):
    _task(db, message.channel_id, wa.NOTICE, "wa:notice:%s:%s" % (message.id, reason),
          {"message_id": message.id, "reason": reason})


def recover_task(db, task):
    payload = json.loads(task.payload)
    if task.task_type == wa.SEND and payload.get("attempt_started"):
        task.status, task.last_error = "uncertain", "whatsapp_send_interrupted"
        message = db.get(Message, payload.get("message_id"))
        if message:
            enqueue_notice(db, message, "uncertain")
    else:
        task.status = "pending"


async def _deliver(db, message, phone=None, *, notice=None):
    channel = db.get(Channel, message.channel_id)
    portal = channel.portal
    if portal.uninstalled_at is not None:
        raise wa.WhatsAppError("portal_uninstalled")
    # An unnamed participant uses the bare WhatsApp ID as an explicit fallback.
    # Keep it in a single field, without the two-word title or a fake surname.
    user = {"id": message.subscriber_user_id,
            "name": sanitize_name(message.subscriber_identifier).replace("[", "［").replace("]", "］")}
    if _has_client_name(message):
        user["name"] = message.user_name
        if message.user_last_name:
            user["last_name"] = message.user_last_name
    if phone:
        user["phone"] = phone  # No skip_phone_validate and no invented phone.
    content = {"id": "wa:%s:%s" % (channel.subject_id, message.max_message_id),
               "date": int(message.sent_at.replace(tzinfo=timezone.utc).timestamp()), "text": message.text}
    if notice:
        content.update(id="wa-notice:%s:%s" % (message.id, notice), disable_crm="Y")
        content["text"] = {
            "uncertain": "Результат отправки в WhatsApp неизвестен. Не отправляйте повторно до проверки в поддержке edna: клиент мог уже получить сообщение.",
            "unsupported": "Сообщение не отправлено в WhatsApp. Сейчас поддерживается только текст до 4096 символов. Отправьте текст без вложений.",
        }.get(notice, "Сообщение не доставлено клиенту в WhatsApp. Обратитесь в поддержку edna.")
    await wa.bitrix_call(portal, db, "imconnector.send.messages", {
        "CONNECTOR": wa.CONNECTOR, "LINE": int(message.line_id),
        "MESSAGES": [{"user": user, "chat": {"id": wa.chat_id(channel, message.subscriber_identifier), "name": _dialog_title(message)},
                      "message": content}],
    })


async def execute_task(task_id, session_factory):
    # Session-level lock survives token-refresh commits and stale-task scans.
    with session_factory() as probe:
        row = probe.get(MessageDeliveryTask, task_id)
        payload = json.loads(row.payload)
        message_id = payload.get("message_id")
        engine = probe.get_bind()
        lock_id, lock_namespace = message_id, 221911270
        if row.task_type == wa.RENAME:
            naming_message = probe.get(Message, message_id)
            if naming_message:
                # Different messages may rename the same dialog. Serialize those
                # requests, including across OAuth-refresh commits and replicas.
                identity = _json([row.channel_id, naming_message.line_id, naming_message.subscriber_identifier])
                lock_id = int(hashlib.sha256(identity.encode()).hexdigest()[:16], 16)
                lock_namespace = 221911271
    with operation_lock(engine, lock_id, namespace=lock_namespace) as acquired:
        with session_factory() as db:
            task = db.get(MessageDeliveryTask, task_id)
            if task.status != "processing":
                return
            if not acquired:
                task.status = "pending"
                task.next_attempt_at = datetime.utcnow() + timedelta(seconds=1)
                db.commit()
                return
            message = db.get(Message, message_id)
            channel = db.get(Channel, task.channel_id)
            try:
                if not message or message.channel_id != task.channel_id or not channel or not wa.is_whatsapp(channel):
                    raise wa.WhatsAppError("invalid_whatsapp_task")
                if task.task_type == wa.RECEIVE:
                    await _deliver(db, message, payload.get("phone"))
                elif task.task_type == wa.RENAME:
                    if not channel.is_active or channel.portal.uninstalled_at is not None:
                        raise wa.WhatsAppError("channel_unavailable")
                    received = db.query(MessageDeliveryTask).filter_by(dedup_key=payload.get("receive_key"),
                        task_type=wa.RECEIVE, channel_id=channel.id).first()
                    if received is None or received.status == "failed":
                        raise wa.WhatsAppError("whatsapp_chat_name_message_failed")
                    if received.status != "sent":
                        raise wa.WhatsAppError("whatsapp_chat_name_waiting_for_message", retryable=True)
                    latest = db.query(Message).filter_by(channel_id=channel.id, direction="incoming",
                        subscriber_identifier=message.subscriber_identifier, line_id=message.line_id).order_by(Message.id.desc()).first()
                    # A queued older rename must not overwrite a newer username.
                    if latest and (latest.user_name, latest.user_last_name) == (message.user_name, message.user_last_name):
                        title = _dialog_title(message)
                        await wa.bitrix_call(channel.portal, db, "imconnector.chat.name.set", {
                            "CONNECTOR": wa.CONNECTOR, "LINE": int(message.line_id),
                            "CHAT_ID": wa.chat_id(channel, message.subscriber_identifier), "NAME": title,
                            "USER_ID": message.subscriber_user_id,
                        })
                        logger.info("WhatsApp chat name updated: channel=%s message_id=%s", channel.id, message.id)
                elif task.task_type == wa.NOTICE:
                    await _deliver(db, message, notice=payload["reason"])
                elif task.task_type == wa.SEND:
                    if payload.get("attempt_started"):
                        raise wa.WhatsAppError("whatsapp_send_interrupted", uncertain=True)
                    if not channel.is_active or channel.portal.uninstalled_at is not None:
                        raise wa.WhatsAppError("channel_unavailable")
                    if settings.edna_force_fail == "1":
                        raise wa.WhatsAppError("forced_send_failure")
                    payload["attempt_started"] = True
                    task.payload = _json(payload)
                    db.commit()  # Durable before the only possibly accepted POST.
                    # Serialize against disconnect/uninstall, in the same portal ->
                    # channel order as management. NO KEY UPDATE permits receipt FK
                    # inserts while the HTTP send is in flight (no callback deadlock).
                    portal = db.query(Portal).filter_by(id=channel.portal_id).populate_existing().with_for_update(key_share=True).one()
                    channel = db.query(Channel).filter_by(id=task.channel_id).populate_existing().with_for_update(key_share=True).one()
                    if not channel.is_active or portal.uninstalled_at is not None:
                        raise wa.WhatsAppError("channel_unavailable")
                    await wa.send_text(channel.api_key, channel.sender, message.subscriber_identifier,
                                       message.text, message.edna_request_id)
                elif task.task_type == wa.STATUS:
                    source = db.query(MessageDeliveryTask).filter_by(task_type=wa.SEND, channel_id=channel.id,
                        dedup_key="wa:out:%s:%s" % (channel.portal_id, message.im_message_id)).first()
                    if source is None:
                        raise wa.WhatsAppError("missing_whatsapp_send")
                    current = json.loads(source.payload)
                    prior = current.get("delivery_status")
                    status = payload["status"]
                    ranks = {"SENT": 1, "DELIVERED": 2, "READ": 3}
                    # A delayed failure cannot override observed delivery/read.
                    apply = prior != "READ" and not (prior == "DELIVERED" and status in wa.FAILURES)
                    apply = apply and not (prior in wa.FAILURES and status == "SENT")
                    apply = apply and (status in wa.FAILURES or ranks.get(status, 0) >= ranks.get(prior, 0))
                    if apply:
                        if status == "READ":
                            params = {
                                "CONNECTOR": wa.CONNECTOR, "LINE": int(message.line_id), "MESSAGES": [{
                                    "im": {"chat_id": int(message.bitrix_chat_id), "message_id": int(message.im_message_id)},
                                    "message": {"id": [message.edna_request_id],
                                                "date": payload.get("date") or int(datetime.now(timezone.utc).timestamp())},
                                    "chat": {"id": wa.chat_id(channel, message.subscriber_identifier)},
                                }],
                            }
                            wa.log_payload("Bitrix READ request", params, channel.id)
                            await wa.bitrix_call(channel.portal, db, "imconnector.send.status.delivery", params)
                            logger.info("WhatsApp READ forwarded to Bitrix: channel=%s message_id=%s request_id=%s",
                                        channel.id, message.id, message.edna_request_id)
                        elif status in wa.FAILURES:
                            enqueue_notice(db, message, "failed")
                        current["delivery_status"] = status
                        source.payload = _json(current)
                        source.status = "failed" if status in wa.FAILURES else "sent"
                task.status, task.last_error = "sent", None
                task.updated_at = datetime.utcnow()
                db.commit()
            except wa.WhatsAppError as exc:
                db.rollback()
                task = db.get(MessageDeliveryTask, task_id)
                payload = json.loads(task.payload)
                task.retry_count += 1
                task.last_error = str(exc)
                if exc.uncertain:
                    task.status = "uncertain"
                elif exc.retryable and task.retry_count <= len(RETRY):
                    task.status = "pending"
                    task.next_attempt_at = datetime.utcnow() + timedelta(seconds=RETRY[task.retry_count - 1])
                    payload["attempt_started"] = False
                    task.payload = _json(payload)
                else:
                    task.status = "failed"
                if task.task_type == wa.SEND and task.status in {"uncertain", "failed"} and message:
                    enqueue_notice(db, message, "uncertain" if task.status == "uncertain" else "failed")
                task.updated_at = datetime.utcnow()
                db.commit()
                logger.warning("WhatsApp task %s: state=%s code=%s", task_id, task.status, str(exc))
            except Exception:
                # Unknown errors after a send may be a crash after upstream acceptance.
                db.rollback()
                task = db.get(MessageDeliveryTask, task_id)
                if task.task_type == wa.SEND:
                    recover_task(db, task)
                else:
                    task.retry_count += 1
                    task.status = "pending" if task.retry_count <= len(RETRY) else "failed"
                    task.next_attempt_at = datetime.utcnow() + timedelta(seconds=60)
                task.last_error = "whatsapp_operation_error"
                task.updated_at = datetime.utcnow()
                db.commit()
                # SQL/transport exceptions may embed secrets; log the safe task id only.
                wa.log_exception("WhatsApp task %s could not finish; state=%s", task_id, task.status)
