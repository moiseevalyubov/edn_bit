"""Explicit capabilities: adding a channel type never falls through to MAX."""
from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class OutreachCapability:
    code_prefix: str
    label: str
    address_type: str
    content_types: frozenset[str]
    max_text_length: int = 4000


CAPABILITIES = {
    "MAX": OutreachCapability("edna_max", "MAX от edna", "PHONE", frozenset({"TEXT"})),
    "TELEGRAM": OutreachCapability("edna_telegram", "Telegram от edna", "PHONE", frozenset({"TEXT"}), 4096),
}


def outreach_capability(channel):
    kind = (channel.channel_type or "").upper()
    if kind == "TELEGRAM" and not settings.crm_telegram_outreach_enabled:
        return None
    return CAPABILITIES.get(kind)
