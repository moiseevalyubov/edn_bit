import time
import uuid

# Why this module exists:
# edna requires file URLs with a proper extension (e.g. /file/photo.jpg).
# Bitrix24 download links look like pub/im.file.php?FILE_ID=...&SIGN=... — no extension,
# and the SIGN expires. edna also fetches the URL asynchronously after returning 200 OK,
# so by the time it calls back, the Bitrix SIGN may be gone.
# Solution: download the file immediately on webhook receipt and cache it here.
# edna gets a stable /file/<uuid>.jpg URL served from this cache.

TTL_SECONDS = 600  # files expire after 10 minutes

_cache: dict[str, tuple[bytes, str, float]] = {}


def store(content: bytes, content_type: str, ext: str) -> str:
    _cleanup()
    key = f"{uuid.uuid4()}.{ext}"
    _cache[key] = (content, content_type, time.monotonic())
    return key


def get(key: str) -> tuple[bytes, str] | None:
    _cleanup()
    entry = _cache.get(key)
    if entry is None:
        return None
    content, content_type, _ = entry
    return content, content_type


def _cleanup() -> None:
    now = time.monotonic()
    expired = [k for k, (_, _, ts) in _cache.items() if now - ts > TTL_SECONDS]
    for k in expired:
        del _cache[k]
