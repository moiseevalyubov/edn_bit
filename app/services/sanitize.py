"""SEC-7 + SEC-10: validation and sanitization of inbound client content.

Client text and names arrive from edna / MAX Bot and are forwarded into
Bitrix24 Open Lines, where the operator UI may render them. We therefore:
  - neutralize HTML/JS so a malicious client message can't inject markup
    (SEC-10) — dangerous fragments are REPLACED with a visible placeholder,
    not silently deleted, so the operator understands something was removed
    and can react (ask the client to resend as a file/screenshot, etc.)
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

# Visible marker the operator sees in place of removed markup. Kept free of
# < > and [ ] so neither an HTML renderer nor a BBCode parser can re-interpret
# or hide it. Wording can be adjusted freely.
PLACEHOLDER = "(потенциально опасный код удалён)"

# Match <script>/<style> blocks together with their contents first, then any
# remaining real HTML tag. The tag pattern only matches sequences that start
# with a letter or "/" (i.e. <div>, </b>, <img ...>) so plain text like
# "2 < 3" or "цена > 100" is left untouched.
_SCRIPT_RE = re.compile(r"<\s*(script|style)\b.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<\s*/?[a-zA-Z][^>]*>")
# Collapse 2+ adjacent placeholders (optionally separated by whitespace) into
# one, so a string of tags like <div><span> yields a single marker.
_COLLAPSE_RE = re.compile(r"(?:" + re.escape(PLACEHOLDER) + r"\s*){2,}")


def _removed_preview(original: str) -> str:
    """First dangerous fragment that was removed, truncated for the log."""
    match = _SCRIPT_RE.search(original) or _TAG_RE.search(original)
    return match.group(0)[:120] if match else ""


def sanitize_text(value, *, limit: int = MAX_TEXT_LENGTH) -> str:
    """Replace HTML/JS in a client message with a placeholder and cap length."""
    if not value:
        return ""
    original = str(value)
    text = _SCRIPT_RE.sub(PLACEHOLDER, original)
    text = _TAG_RE.sub(PLACEHOLDER, text)
    if PLACEHOLDER in text:
        text = _COLLAPSE_RE.sub(PLACEHOLDER, text)
        logger.warning(
            "Sanitize: removed potentially dangerous markup from client message; "
            "removed fragment preview: %r",
            _removed_preview(original),
        )
    if len(text) > limit:
        logger.warning("Sanitize: text truncated from %d to %d chars", len(text), limit)
        text = text[:limit]
    return text.strip()


def sanitize_name(value) -> str:
    """Strip HTML tags from a user name and cap to MAX_NAME_LENGTH.

    Names are short identifiers shown as the contact name in Bitrix, so a
    placeholder would look odd here — tags are removed outright instead.
    """
    if not value:
        return ""
    name = _TAG_RE.sub("", str(value))
    if len(name) > MAX_NAME_LENGTH:
        name = name[:MAX_NAME_LENGTH]
    return name.strip()
