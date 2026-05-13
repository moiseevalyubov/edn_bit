import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


def _make_fernet(secret_key: str) -> Fernet:
    # Derive a valid 32-byte Fernet key from any string via SHA-256
    key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())
    return Fernet(key)


@lru_cache(maxsize=1)
def get_fernet() -> Fernet:
    from app.config import settings
    return _make_fernet(settings.secret_key)


def is_encrypted(value: str) -> bool:
    try:
        get_fernet().decrypt(value.encode())
        return True
    except InvalidToken:
        return False


def encrypt(value: str) -> str:
    return get_fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return get_fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        logger.warning("decrypt: InvalidToken — returning raw value; if migration is complete, check SECRET_KEY")
        return value
