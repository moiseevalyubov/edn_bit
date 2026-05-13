import hashlib
import hmac
import time
import uuid

# Why this module exists:
# edna requires file URLs with a proper extension (e.g. /file/photo.jpg).
# Bitrix24 download links look like pub/im.file.php?FILE_ID=...&SIGN=... — no extension,
# and the SIGN expires. edna also fetches the URL asynchronously after returning 200 OK,
# so by the time it calls back, the Bitrix SIGN may be gone.
# Solution: download the file immediately on webhook receipt and cache it here.
# edna gets a stable /file/<uuid>.jpg URL served from this cache.
#
# Limitation: files are stored in memory for TTL_SECONDS only. After that the URL
# returns 404 (cache miss) or 403 (expired token). This is acceptable because MAX Bot
# downloads the file within seconds of receiving the URL and stores it on its own server.
# Long-term solution: migrate to S3-compatible storage (see PROD-3 in review.md).

TTL_SECONDS = 600  # 10 minutes — cache lifetime and signed URL lifetime

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


def _sign(file_key: str, expires: int) -> str:
    from app.config import settings
    msg = f"{file_key}:{expires}".encode()
    return hmac.new(settings.secret_key.encode(), msg, hashlib.sha256).hexdigest()


def make_signed_url(file_key: str) -> str:
    from app.config import settings
    if not settings.app_base_url:
        raise RuntimeError("APP_BASE_URL is not configured — cannot generate file URL")
    expires = int(time.time()) + TTL_SECONDS
    token = _sign(file_key, expires)
    return f"{settings.app_base_url}/file/{file_key}?token={token}&expires={expires}"


def verify_signed_url(file_key: str, token: str, expires: int) -> bool:
    if time.time() > expires:
        return False
    expected = _sign(file_key, expires)
    return hmac.compare_digest(expected, token)
