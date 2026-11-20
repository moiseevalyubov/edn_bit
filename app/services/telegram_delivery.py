"""Durable Telegram operations using the existing queue and schema.

No Telegram credentials or raw webhook/event bodies are stored in task payloads.
The incoming contract is intentionally strict until verified with a live channel.
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
from app.services import telegram as tg
from app.services import telegram_files
from app.services.crm_locks import operation_lock
from app.services.sanitize import sanitize_name, sanitize_text

logger = logging.getLogger(__name__)
RETRY = (1, 5, 30, 120, 600, 1800)
UNSUPPORTED = "Клиент отправил вложение. Его отображение в Telegram пока не поддерживается. Попросите клиента написать текстом."
MEDIA_UNAVAILABLE = "Вложение клиента из Telegram не удалось загрузить. Поддерживаются изображения и документы до 64 МБ. Попросите клиента прислать файл заново или другим способом."


def _json(data):
    return json.dumps(data, ensure_ascii=False)


def _task(db, channel_id, kind, key, payload):
    existing = db.query(MessageDeliveryTask).filter_by(dedup_key=key).first()
    if existing:
        if kind in {tg.RECEIVE, tg.SEND}:
            # Another request committed between the caller's lookup and flush.
            # Roll back its newly inserted Message rather than keep an orphan.
            raise tg.TelegramError("telegram_commit_conflict", retryable=True)
        return existing
    task = MessageDeliveryTask(channel_id=channel_id, task_type=kind,
                               direction="incoming" if kind == tg.RECEIVE else "outgoing",
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
        raise tg.TelegramError("telegram_commit_conflict", retryable=True) from None


def _time(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo:
                return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            pass
    return datetime.utcnow()


def accept_incoming(db, channel, data, *, media_jobs=None):
    if not settings.telegram_enabled:
        raise tg.TelegramError("telegram_disabled")
    if not channel.is_active or not channel.portal or channel.portal.uninstalled_at is not None:
        raise tg.TelegramError("channel_unavailable")
    if not channel.portal.open_line_id:
        raise tg.TelegramError("open_line_required", retryable=True)
    tg.check_subject(channel, data)
    msg_id = tg.scalar(data.get("id"))
    key = "tg:in:%s:%s" % (channel.id, hashlib.sha256(msg_id.encode()).hexdigest())
    if db.query(MessageDeliveryTask).filter_by(dedup_key=key).first():
        return
    address, phone = tg.incoming_identity(data)
    user = data.get("userInfo") if isinstance(data.get("userInfo"), dict) else {}
    previous = db.query(Message).filter_by(channel_id=channel.id, direction="incoming",
                                          subscriber_identifier=address).order_by(Message.id.desc()).first()
    name = sanitize_name(user.get("firstName") or user.get("displayName") or user.get("userName"))
    username = tg.incoming_username(data)
    name_source = "userInfo" if name else "TELEGRAM_USERNAME" if username else "history" if previous and previous.user_name else "identifier"
    name = name or username or (previous.user_name if previous else None) or ("Telegram " + address)
    last_name = sanitize_name(user.get("lastName")) or (previous.user_last_name if previous else None)
    name = name.replace("[", "［").replace("]", "］")
    last_name = last_name.replace("[", "［").replace("]", "］") if last_name else None
    content = data.get("messageContent")
    if not isinstance(content, dict):
        raise tg.TelegramError("message_content_required")
    raw_text = content.get("text")
    attachment = content.get("attachment")
    is_media = (media_jobs is not None and content.get("type") in {"IMAGE", "DOCUMENT"}
                and isinstance(attachment, dict) and not content.get("items"))
    if content.get("type") != "TEXT":
        caption = content.get("caption")
        parts = [part for part in (caption, raw_text) if isinstance(part, str) and part]
        raw_text = "\n".join(dict.fromkeys(parts))
    text = sanitize_text(raw_text).replace("[", "［").replace("]", "］") if isinstance(raw_text, str) else ""
    if is_media:
        if len(raw_text) > 4096:
            text += "\nПодпись сокращена. Попросите клиента прислать продолжение."
    elif content.get("type") != "TEXT" or not text:
        text = (text + "\n" if text else "") + UNSUPPORTED
    elif len(raw_text) > 4096:
        text += "\nТекст сокращён. Попросите клиента прислать продолжение."
    message = Message(channel_id=channel.id, direction="incoming", text=text, content_type="TEXT",
                      max_message_id=msg_id, subscriber_identifier=address, subscriber_id_type="TELEGRAM_ID",
                      subscriber_user_id="tg:" + address, user_name=name, user_last_name=last_name,
                      bitrix_chat_id=tg.chat_id(channel, address), line_id=str(channel.portal.open_line_id),
                      sent_at=_time(data.get("receivedAt")))
    db.add(message)
    db.flush()
    task = _task(db, channel.id, tg.RECEIVE, key, {"message_id": message.id, "phone": phone})
    if is_media:
        media_jobs.append((task, message, attachment, content["type"]))
    if previous is not None and name_source != "identifier" and (name, last_name) != (previous.user_name, previous.user_last_name):
        _task(db, channel.id, tg.RENAME, "tg:rename:%s" % message.id,
              {"message_id": message.id, "receive_key": key})
    saved_channel_id, saved_message_id = channel.id, message.id
    if media_jobs is None:
        _commit(db)
        _log_incoming((saved_channel_id, saved_message_id, msg_id, name_source))
    return saved_channel_id, saved_message_id, msg_id, name_source


def _log_incoming(saved):
    if saved:
        logger.info("Telegram incoming saved: channel=%s message_id=%s external_id=%s name_source=%s", *saved)


async def accept_incoming_with_media(db, channel, data):
    jobs = []
    saved = accept_incoming(db, channel, data, media_jobs=jobs)
    for task, message, attachment, expected_type in jobs:
        try:
            declared = attachment.get("size")
            if declared is not None and (isinstance(declared, bool) or not isinstance(declared, int)
                                         or not 0 < declared <= telegram_files.MAX_BYTES):
                raise tg.TelegramError("media_invalid_size")
            content, mime = await telegram_files.download(attachment.get("url"), {"app.edna.ru"})
            if declared is not None and len(content) != declared:
                raise tg.TelegramError("media_size_mismatch")
            name = attachment.get("name")
            telegram_files.log_metadata("incoming", channel.id, message.id, content, mime, name)
            actual_type = telegram_files.inspect_file(content, mime, name)[0]
            if expected_type == "IMAGE" and actual_type != "IMAGE":
                raise tg.TelegramError("media_type_mismatch")
            key, _ = telegram_files.store(db, message, content, mime, name)
            message.content_type = expected_type
            payload = json.loads(task.payload)
            payload["file_key"] = key
            task.payload = _json(payload)
            db.flush()
        except tg.TelegramError as exc:
            if exc.retryable:
                raise
            message.text = (message.text + "\n" if message.text else "") + MEDIA_UNAVAILABLE
            logger.warning("Telegram incoming file rejected: channel=%s message_id=%s code=%s", channel.id, message.id, exc.code)
    _commit(db)
    _log_incoming(saved)


def accept_outgoing(db, portal, data, *, media_jobs=None):
    if not settings.telegram_enabled or portal.uninstalled_at is not None:
        raise tg.TelegramError("telegram_disabled")
    if not portal.app_token:
        raise tg.TelegramError("app_token_required")
    event = data.get("data")
    if not isinstance(event, dict) or event.get("CONNECTOR") != tg.CONNECTOR:
        raise tg.TelegramError("wrong_connector")
    line = tg.scalar(event.get("LINE"))
    messages = event.get("MESSAGES")
    if not isinstance(messages, list) or not messages or len(messages) > 100:
        raise tg.TelegramError("invalid_messages")
    # Batch acceptance is transactional: no inner commits, including validation errors.
    for item in messages:
        if not isinstance(item, dict):
            raise tg.TelegramError("invalid_message")
        chat = item.get("chat") if isinstance(item.get("chat"), dict) else {}
        external_chat = tg.scalar(chat.get("id"))
        original = db.query(Message).join(Channel).filter(
            Channel.portal_id == portal.id, Channel.channel_type == "TELEGRAM",
            Message.direction == "incoming", Message.bitrix_chat_id == external_chat,
            Message.line_id == line,
        ).order_by(Message.id.desc()).first()
        if original is None:
            raise tg.TelegramError("unknown_telegram_dialog")
        channel = db.get(Channel, original.channel_id)
        # Reconnection may produce a new channel row; preserve known Telegram identity.
        active = db.query(Channel).filter_by(portal_id=portal.id, sender=channel.sender,
                                             channel_type="TELEGRAM", is_active=True).all()
        if len(active) != 1:
            raise tg.TelegramError("channel_unavailable")
        channel = active[0]
        im = item.get("im") if isinstance(item.get("im"), dict) else {}
        im_id, im_chat = tg.scalar(im.get("message_id")), tg.scalar(im.get("chat_id"))
        if not im_id.isascii() or not im_id.isdecimal() or not im_chat.isascii() or not im_chat.isdecimal():
            raise tg.TelegramError("invalid_bitrix_identity")
        # Bitrix message id remains unique across reconnect and rename.
        key = "tg:out:%s:%s" % (portal.id, im_id)
        body = item.get("message") if isinstance(item.get("message"), dict) else {}
        from app.routers.handler import strip_bbcode
        raw = body.get("text")
        text = strip_bbcode(raw) if isinstance(raw, str) else ""
        content_hash = hashlib.sha256(_json([external_chat, line, im_chat, text, bool(body.get("files"))]).encode()).hexdigest()
        files = body.get("files")
        if files:
            if not isinstance(files, list) or any(not isinstance(f, dict) for f in files):
                raise tg.TelegramError("invalid_files")
            # Signed source URLs are transient credentials, not durable task data.
            # Their digest also detects a conflicting event with a reused IM id.
            fingerprints = []
            for file in files:
                try:
                    location = urlsplit(file.get("downloadLink") or file.get("link") or "")
                    source_id = [location.hostname, location.path,
                                 [(k, v) for k, v in parse_qsl(location.query) if k.lower() == "file_id"]]
                except (ValueError, TypeError):
                    raise tg.TelegramError("invalid_file_url") from None
                fingerprints.append([file.get("id"), file.get("name"), file.get("mime"), source_id])
            content_hash = hashlib.sha256(_json([external_chat, line, im_chat, text, fingerprints]).encode()).hexdigest()
        existing = db.query(MessageDeliveryTask).filter_by(dedup_key=key).first()
        if existing:
            if json.loads(existing.payload).get("content_hash") != content_hash:
                raise tg.TelegramError("message_id_conflict")
            continue
        message = Message(channel_id=channel.id, direction="outgoing", text=text, content_type="TEXT",
                          subscriber_identifier=original.subscriber_identifier, subscriber_id_type="TELEGRAM_ID",
                          subscriber_user_id=original.subscriber_user_id, user_name=original.user_name,
                          user_last_name=original.user_last_name, bitrix_chat_id=im_chat,
                          im_message_id=im_id, line_id=line, edna_request_id=uuid.uuid4().hex)
        db.add(message)
        db.flush()
        task = _task(db, channel.id, tg.SEND, key, {
            "message_id": message.id, "chat_id": external_chat, "content_hash": content_hash,
            "attempt_started": False,
        })
        if files and len(files) == 1 and media_jobs is not None and len(text) <= 4096:
            media_jobs.append((task, message, files[0]))
        elif files or not text or len(text) > 4096:
            task.status, task.last_error = "failed", "telegram_text_only_or_invalid_text"
            enqueue_notice(db, message, "unsupported")
        db.flush()  # Make duplicates inside this very batch visible with autoflush=False.
    if media_jobs is None:
        _commit(db)


async def accept_outgoing_with_media(db, portal, data):
    jobs = []
    accept_outgoing(db, portal, data, media_jobs=jobs)
    for task, message, file in jobs:
        try:
            url = file.get("downloadLink") or file.get("link")
            name = file.get("name")
            # Only the authenticated portal's own origin is trusted initially.
            # CDN support requires a verified source, never an arbitrary URL.
            endpoint = urlsplit(portal.client_endpoint)
            content, mime = await telegram_files.download(url, {endpoint.hostname})
            telegram_files.log_metadata("outgoing", message.channel_id, message.id, content, mime, name)
            key, media_type = telegram_files.store(db, message, content, mime, name)
            message.content_type = media_type
            payload = json.loads(task.payload)
            payload.update(file_key=key, media_type=media_type)
            task.payload = _json(payload)
            db.flush()
        except tg.TelegramError as exc:
            if exc.retryable:
                raise  # Entire batch rolls back; no secret URL is persisted.
            task.status, task.last_error = "failed", exc.code
            enqueue_notice(db, message, "media")
            logger.warning("Telegram outgoing file rejected: channel=%s message_id=%s code=%s", message.channel_id, message.id, exc.code)
    _commit(db)


def accept_status(db, channel, data):
    # Status receipts remain available after disconnection and flag-off.
    tg.check_subject(channel, data)
    request_id = tg.scalar(data.get("requestId"))
    status = tg.scalar(data.get("status")).upper()
    if status not in {"SENT", "DELIVERED", "READ", *tg.FAILURES}:
        logger.info("Telegram status ignored: channel=%s request_id=%s status=%s", channel.id, request_id, status)
        return
    message = db.query(Message).filter_by(channel_id=channel.id, direction="outgoing", edna_request_id=request_id).first()
    if message is None:
        logger.warning("Telegram status not matched: channel=%s request_id=%s status=%s", channel.id, request_id, status)
        raise tg.TelegramError("telegram_request_unknown", retryable=True)
    key = "tg:status:%s:%s" % (message.id, status)
    _task(db, channel.id, tg.STATUS, key,
          {"message_id": message.id, "status": status,
           "date": int(_time(data.get("statusAt")).replace(tzinfo=timezone.utc).timestamp())})
    saved_channel_id, saved_message_id = channel.id, message.id
    _commit(db)
    logger.info("Telegram status saved: channel=%s message_id=%s request_id=%s status=%s",
                saved_channel_id, saved_message_id, request_id, status)


def enqueue_notice(db, message, reason):
    _task(db, message.channel_id, tg.NOTICE, "tg:notice:%s:%s" % (message.id, reason),
          {"message_id": message.id, "reason": reason})


def recover_task(db, task):
    payload = json.loads(task.payload)
    if task.task_type == tg.SEND and payload.get("attempt_started"):
        task.status, task.last_error = "uncertain", "telegram_send_interrupted"
        message = db.get(Message, payload.get("message_id"))
        if message:
            enqueue_notice(db, message, "uncertain")
    else:
        task.status = "pending"


async def _deliver(db, message, phone=None, *, notice=None, file_key=None):
    channel = db.get(Channel, message.channel_id)
    portal = channel.portal
    if portal.uninstalled_at is not None:
        raise tg.TelegramError("portal_uninstalled")
    user = {"id": message.subscriber_user_id, "name": message.user_name}
    if message.user_last_name:
        user["last_name"] = message.user_last_name
    if phone:
        user["phone"] = phone  # No skip_phone_validate and no invented phone.
    content = {"id": "tg:%s:%s" % (channel.subject_id, message.max_message_id),
               "date": int(message.sent_at.replace(tzinfo=timezone.utc).timestamp()), "text": message.text}
    if file_key and not notice:
        try:
            entry = telegram_files.get(db, file_key, message.id)
            content["files"] = [{"url": telegram_files.signed_url(db, file_key, message.id),
                                 "name": entry.name.replace("[", "［").replace("]", "］")}]
        except tg.TelegramError:
            # A held incoming task may outlive media retention. Deliver its
            # caption and an explicit warning rather than lose the whole event.
            content["text"] = (message.text + "\n" if message.text else "") + MEDIA_UNAVAILABLE
    if notice:
        content.update(id="tg-notice:%s:%s" % (message.id, notice), disable_crm="Y")
        content["text"] = {
            "uncertain": "Результат отправки в Telegram неизвестен. Не отправляйте повторно до проверки в поддержке edna: клиент мог уже получить сообщение.",
            "unsupported": "Сообщение не отправлено в Telegram. Сейчас поддерживаются текст до 4096 символов и одно изображение или документ до 64 МБ за сообщение. Прикрепите вложения по одному.",
            "media": "Файл не отправлен в Telegram: не удалось безопасно загрузить изображение или документ. Проверьте формат и размер (до 64 МБ) и прикрепите файл заново.",
        }.get(notice, "Сообщение не доставлено клиенту в Telegram. Обратитесь в поддержку edna.")
    await tg.bitrix_call(portal, db, "imconnector.send.messages", {
        "CONNECTOR": tg.CONNECTOR, "LINE": int(message.line_id),
        "MESSAGES": [{"user": user, "chat": {"id": tg.chat_id(channel, message.subscriber_identifier), "name": message.user_name},
                      "message": content}],
    })


async def execute_task(task_id, session_factory):
    # Session-level lock survives token-refresh commits and stale-task scans.
    with session_factory() as probe:
        row = probe.get(MessageDeliveryTask, task_id)
        payload = json.loads(row.payload)
        message_id = payload.get("message_id")
        engine = probe.get_bind()
        lock_id, lock_namespace = message_id, 221911260
        if row.task_type == tg.RENAME:
            naming_message = probe.get(Message, message_id)
            if naming_message:
                # Different messages may rename the same dialog. Serialize those
                # requests, including across OAuth-refresh commits and replicas.
                identity = _json([row.channel_id, naming_message.line_id, naming_message.subscriber_identifier])
                lock_id = int(hashlib.sha256(identity.encode()).hexdigest()[:16], 16)
                lock_namespace = 221911261
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
                if not message or message.channel_id != task.channel_id or not channel or not tg.is_telegram(channel):
                    raise tg.TelegramError("invalid_telegram_task")
                if task.task_type == tg.RECEIVE:
                    await _deliver(db, message, payload.get("phone"), file_key=payload.get("file_key"))
                elif task.task_type == tg.RENAME:
                    if not channel.is_active or channel.portal.uninstalled_at is not None or not settings.telegram_enabled:
                        raise tg.TelegramError("channel_unavailable")
                    received = db.query(MessageDeliveryTask).filter_by(dedup_key=payload.get("receive_key"),
                        task_type=tg.RECEIVE, channel_id=channel.id).first()
                    if received is None or received.status == "failed":
                        raise tg.TelegramError("telegram_chat_name_message_failed")
                    if received.status != "sent":
                        raise tg.TelegramError("telegram_chat_name_waiting_for_message", retryable=True)
                    latest = db.query(Message).filter_by(channel_id=channel.id, direction="incoming",
                        subscriber_identifier=message.subscriber_identifier, line_id=message.line_id).order_by(Message.id.desc()).first()
                    # A queued older rename must not overwrite a newer username.
                    if latest and (latest.user_name, latest.user_last_name) == (message.user_name, message.user_last_name):
                        title = " ".join(value for value in (message.user_name, message.user_last_name) if value)
                        await tg.bitrix_call(channel.portal, db, "imconnector.chat.name.set", {
                            "CONNECTOR": tg.CONNECTOR, "LINE": int(message.line_id),
                            "CHAT_ID": tg.chat_id(channel, message.subscriber_identifier), "NAME": title,
                        })
                        logger.info("Telegram chat name updated: channel=%s message_id=%s", channel.id, message.id)
                elif task.task_type == tg.NOTICE:
                    await _deliver(db, message, notice=payload["reason"])
                elif task.task_type == tg.SEND:
                    if payload.get("attempt_started"):
                        raise tg.TelegramError("telegram_send_interrupted", uncertain=True)
                    if not settings.telegram_enabled or not channel.is_active or channel.portal.uninstalled_at is not None:
                        raise tg.TelegramError("channel_unavailable")
                    if settings.edna_force_fail == "1":
                        raise tg.TelegramError("forced_send_failure")
                    # Check durable media before recording any possibly accepted POST.
                    media_url, media_name = None, None
                    if payload.get("file_key"):
                        media_url = telegram_files.signed_url(db, payload["file_key"], message.id)
                        entry = telegram_files.get(db, payload["file_key"], message.id)
                        stem, dot, ext = entry.name.rpartition(".")
                        media_name = stem[:24 - len(ext)] + dot + ext if dot else entry.name[:25]
                    payload["attempt_started"] = True
                    task.payload = _json(payload)
                    db.commit()  # Durable before the only possibly accepted POST.
                    # Serialize against disconnect/uninstall, in the same portal ->
                    # channel order as management. NO KEY UPDATE permits receipt FK
                    # inserts while the HTTP send is in flight (no callback deadlock).
                    portal = db.query(Portal).filter_by(id=channel.portal_id).populate_existing().with_for_update(key_share=True).one()
                    channel = db.query(Channel).filter_by(id=task.channel_id).populate_existing().with_for_update(key_share=True).one()
                    if not channel.is_active or portal.uninstalled_at is not None or not settings.telegram_enabled:
                        raise tg.TelegramError("channel_unavailable")
                    if media_url:
                        await tg.send_media(channel.api_key, channel.sender, message.subscriber_identifier,
                                            payload["media_type"], media_url, media_name, message.edna_request_id,
                                            text=message.text)
                    else:
                        await tg.send_text(channel.api_key, channel.sender, message.subscriber_identifier,
                                           message.text, message.edna_request_id)
                elif task.task_type == tg.STATUS:
                    source = db.query(MessageDeliveryTask).filter_by(task_type=tg.SEND, channel_id=channel.id,
                        dedup_key="tg:out:%s:%s" % (channel.portal_id, message.im_message_id)).first()
                    if source is None:
                        raise tg.TelegramError("missing_telegram_send")
                    current = json.loads(source.payload)
                    prior = current.get("delivery_status")
                    status = payload["status"]
                    ranks = {"SENT": 1, "DELIVERED": 2, "READ": 3}
                    # A delayed failure cannot override observed delivery/read.
                    apply = prior != "READ" and not (prior == "DELIVERED" and status in tg.FAILURES)
                    apply = apply and not (prior in tg.FAILURES and status == "SENT")
                    apply = apply and (status in tg.FAILURES or ranks.get(status, 0) >= ranks.get(prior, 0))
                    if apply:
                        if status == "READ":
                            params = {
                                "CONNECTOR": tg.CONNECTOR, "LINE": int(message.line_id), "MESSAGES": [{
                                    "im": {"chat_id": int(message.bitrix_chat_id), "message_id": int(message.im_message_id)},
                                    "message": {"id": [message.edna_request_id],
                                                "date": payload.get("date") or int(datetime.now(timezone.utc).timestamp())},
                                    "chat": {"id": tg.chat_id(channel, message.subscriber_identifier)},
                                }],
                            }
                            tg.log_payload("Bitrix READ request", params, channel.id)
                            await tg.bitrix_call(channel.portal, db, "imconnector.send.status.delivery", params)
                            logger.info("Telegram READ forwarded to Bitrix: channel=%s message_id=%s request_id=%s",
                                        channel.id, message.id, message.edna_request_id)
                        elif status in tg.FAILURES:
                            enqueue_notice(db, message, "failed")
                        current["delivery_status"] = status
                        source.payload = _json(current)
                        source.status = "failed" if status in tg.FAILURES else "sent"
                task.status, task.last_error = "sent", None
                task.updated_at = datetime.utcnow()
                db.commit()
            except tg.TelegramError as exc:
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
                if task.task_type == tg.SEND and task.status in {"uncertain", "failed"} and message:
                    enqueue_notice(db, message, "uncertain" if task.status == "uncertain" else "failed")
                task.updated_at = datetime.utcnow()
                db.commit()
                logger.warning("Telegram task %s: state=%s code=%s", task_id, task.status, str(exc))
            except Exception:
                # Unknown errors after a send may be a crash after upstream acceptance.
                db.rollback()
                task = db.get(MessageDeliveryTask, task_id)
                if task.task_type == tg.SEND:
                    recover_task(db, task)
                else:
                    task.retry_count += 1
                    task.status = "pending" if task.retry_count <= len(RETRY) else "failed"
                    task.next_attempt_at = datetime.utcnow() + timedelta(seconds=60)
                task.last_error = "telegram_operation_error"
                task.updated_at = datetime.utcnow()
                db.commit()
                # SQL/transport exceptions may embed secrets; log the safe task id only.
                tg.log_exception("Telegram task %s could not finish; state=%s", task_id, task.status)
