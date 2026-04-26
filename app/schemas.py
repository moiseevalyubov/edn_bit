from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ChannelCreate(BaseModel):
    member_id: str
    name: str
    api_key: str
    sender: str


class ChannelResponse(BaseModel):
    id: int
    name: str
    sender: str
    is_active: bool
    connected_at: datetime
    disconnected_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ChannelSaveResponse(BaseModel):
    channel: ChannelResponse
    webhook_url: str
