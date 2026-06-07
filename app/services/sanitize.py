"""SEC-7 + SEC-10: validation and sanitization of inbound client content.

Client text and names arrive from edna / MAX Bot and are forwarded into
Bitrix24 Open Lines, where the operator UI may render them. We therefore:
  - strip HTML/JS so a malicious client message can't inject markup (SEC-10)
  - cap lengths so oversized payloads can't bloat the DB or the UI (SEC-7)

Capping is preferred over rejecting: a too-long message is delivered
truncated rather than dropped (returning 400 would make edna give up and
the client's message would be lost entirely).
"""
import logging
import re

logger = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 4096
MAX_NAME_LENGTH = 255

# Remove <script>...</script> blocks together with their contents first,
# then any remaining real HTML tag. The tag pattern only matches sequences
# that start with a letter or "/" (i.e. <div>, </b>, <img ...>) so plain
# text like "2 < 3" or "цена > 100" is left untouched.
_SCRIPT_RE = re.compile(r"<\s*script\b.*?<\s*/\s*script\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<\s*/?[a-zA-Z][^>]*>")


def sanitize_text(value, *, limit: int = MAX_TEXT_LENGTH) -> str:
    """Strip HTML/JS from a client message and cap its length."""
    if not value:
        return ""
    text = _SCRIPT_RE.sub("", str(value))
    text = _TAG_RE.sub("", text)
    if len(text) > limit:
        logger.warning("Sanitize: text truncated from %d to %d chars", len(text), limit)
        text = text[:limit]
    return text.strip()


def sanitize_name(value) -> str:
    """Strip HTML tags from a user name and cap to MAX_NAME_LENGTH."""
    if not value:
        return ""
    name = _TAG_RE.sub("", str(value))
    if len(name) > MAX_NAME_LENGTH:
        name = name[:MAX_NAME_LENGTH]
    return name.strip()
