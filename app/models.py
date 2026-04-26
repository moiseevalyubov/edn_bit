from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class Portal(Base):
    __tablename__ = "portals"

    id = Column(Integer, primary_key=True)
    member_id = Column(String, unique=True, nullable=False, index=True)
    client_endpoint = Column(String, nullable=False)
    access_token = Column(String, nullable=False)
    refresh_token = Column(String, nullable=False)
    token_expires_at = Column(DateTime, nullable=True)
    app_token = Column(String, nullable=True)
    open_line_id = Column(String, nullable=True)
    installed_at = Column(DateTime, default=datetime.utcnow)
    uninstalled_at = Column(DateTime, nullable=True)

    channels = relationship("Channel", back_populates="portal")


class Channel(Base):
    __tablename__ = "channels"

    id = Column(Integer, primary_key=True)
    portal_id = Column(Integer, ForeignKey("portals.id"), nullable=False)
    name = Column(String, nullable=False)
    api_key = Column(String, nullable=False)
    sender = Column(String, nullable=False)
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
    sent_at = Column(DateTime, default=datetime.utcnow)
    raw_payload = Column(Text, nullable=True)

    channel = relationship("Channel", back_populates="messages")
