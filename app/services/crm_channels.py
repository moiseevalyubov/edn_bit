"""Explicit capabilities: adding a channel type never falls through to MAX."""
from dataclasses import dataclass


@dataclass(frozen=True)
class OutreachCapability:
    code_prefix: str
    label: str
    address_type: str
    content_types: frozenset[str]


CAPABILITIES = {"MAX": OutreachCapability("edna_max", "MAX от edna", "PHONE", frozenset({"TEXT"}))}


def outreach_capability(channel):
    return CAPABILITIES.get((channel.channel_type or "").upper())
