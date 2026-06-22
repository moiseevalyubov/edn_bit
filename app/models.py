from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import types
from sqlalchemy.orm import relationship

from app.database import Base


class EncryptedString(types.TypeDecorator):
    impl = types.Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        from app.crypto import encrypt
        return encrypt(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        from app.crypto import decrypt
        return decrypt(value)


class Portal(Base):
    __tablename__ = "portals"

    id = Column(Integer, primary_key=True)
    member_id = Column(String, unique=True, nullable=False, index=True)
    client_endpoint = Column(String, nullable=False)
    access_token = Column(EncryptedString, nullable=False)
    refresh_token = Column(EncryptedString, nullable=False)
    token_expires_at = Column(DateTime, nullable=True)
    app_token = Column(String, nullable=True)
    open_line_id = Column(String, nullable=True)
    installed_at = Column(DateTime, default=datetime.utcnow)
    uninstalled_at = Column(DateTime, nullable=True)
    payment_required_at = Column(DateTime, nullable=True)

    channels = relationship("Channel", back_populates="portal")


class Channel(Base):
    __tablename__ = "channels"

    id = Column(Integer, primary_key=True)
    portal_id = Column(Integer, ForeignKey("portals.id"), nullable=False)
    name = Column(String, nullable=False)
    api_key = Column(EncryptedString, nullable=False)
    sender = Column(String, nullable=False)
    webhook_token = Column(String, nullable=True, unique=True, index=True)
    subject_id = Column(Integer, nullable=True)      # edna subjectId, captured on auto webhook setup
    channel_type = Column(String, nullable=True)     # edna channel type (e.g. WHATSAPP, MAX_BOT)
    connected_at = Column(DateTime, default=datetime.utcnow)
    disconnected_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)

    portal = relationship("Portal", back_populates="channels")
    messages = relationship("Message", back_populates="channel")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=False)
    direction = Column(String, nullable=False)  # 'incoming' | 'outgoing'
    text = Column(Text, nullable=True)
    content_type = Column(String, nullable=False, default="TEXT")
    max_message_id = Column(String, nullable=True)
    bitrix_chat_id = Column(String, nullable=True)
    subscriber_identifier = Column(String, nullable=True)
    # #2: client identity exactly as sent to Bitrix (user.id / name) on incoming
    # messages — reused to post the "undelivered" notice into the SAME dialog.
    subscriber_user_id = Column(String, nullable=True)
    user_name = Column(String, nullable=True)
    sent_at = Column(DateTime, default=datetime.utcnow)
    raw_payload = Column(Text, nullable=True)

    channel = relationship("Channel", back_populates="messages")


class SeenEvent(Base):
    __tablename__ = "seen_events"

    id = Column(Integer, primary_key=True)
    fingerprint = Column(String(64), unique=True, nullable=False, index=True)
    seen_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class MessageDeliveryTask(Base):
    __tablename__ = "message_delivery_tasks"

    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=False)
    task_type = Column(String, nullable=False)   # "send_to_bitrix" | "send_to_edna"
    direction = Column(String, nullable=False)    # "incoming" | "outgoing"
    payload = Column(Text, nullable=False)        # JSON string with all delivery params
    status = Column(String, nullable=False, default="pending")  # pending|processing|sent|failed|dead
    retry_count = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_error = Column(Text, nullable=True)
    max_message_id = Column(String, nullable=True)  # for dedup on incoming tasks
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
