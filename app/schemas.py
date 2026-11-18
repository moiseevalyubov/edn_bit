from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ChannelCreate(BaseModel):
    member_id: str
    name: str
    api_key: str
    sender: str
    subject_id: int | None = None

    @field_validator("name", "api_key", "sender")
    @classmethod
    def required_channel_field(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Обязательное поле")
        return value

    @field_validator("subject_id", mode="before")
    @classmethod
    def optional_subject_id(cls, value):
        if value is None or isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
            value = int(value.strip())
        # Existing Channel.subject_id is a PostgreSQL INTEGER; never round floats.
        if type(value) is not int or not 1 <= value <= 2147483647:
            raise ValueError("Subject ID должен быть целым числом от 1 до 2147483647")
        return value


class ChannelRename(BaseModel):
    member_id: str
    name: str


class ChannelResponse(BaseModel):
    id: int
    name: str
    sender: str
    channel_type: Optional[str] = None
    is_active: bool
    connected_at: datetime
    disconnected_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ChannelSaveResponse(BaseModel):
    channel: ChannelResponse
    webhook_url: str
    auto_configured: bool = False
    auto_error: Optional[str] = None
    setup_warnings: list[str] = Field(default_factory=list)


class OpenLineSet(BaseModel):
    member_id: str
    line_id: str
