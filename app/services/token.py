import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Portal

logger = logging.getLogger(__name__)

OAUTH_URL = "https://oauth.bitrix24.tech/oauth/token/"


class PaymentRequiredError(RuntimeError):
    """Raised when the portal's Bitrix24 subscription has expired."""


def is_token_expired(portal: Portal) -> bool:
    if portal.token_expires_at is None:
        return True
    return datetime.utcnow() >= portal.token_expires_at - timedelta(minutes=5)


async def refresh_token(portal: Portal, db: Session) -> Portal:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            OAUTH_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": settings.bitrix_client_id,
                "client_secret": settings.bitrix_client_secret,
                "refresh_token": portal.refresh_token,
            },
        )
    response.raise_for_status()
    data = response.json()

    if data.get("error") == "PAYMENT_REQUIRED":
        portal.payment_required_at = datetime.utcnow()
        db.commit()
        logger.warning("Portal %s subscription expired (PAYMENT_REQUIRED)", portal.member_id)
        raise PaymentRequiredError(f"Portal {portal.member_id} subscription expired")

    if "error" in data:
        raise RuntimeError(f"Token refresh failed: {data['error']} — {data.get('error_description', '')}")

    portal.access_token = data["access_token"]
    portal.refresh_token = data["refresh_token"]
    portal.token_expires_at = datetime.utcnow() + timedelta(
        seconds=int(data.get("expires_in", 3600))
    )
    db.commit()
    db.refresh(portal)
    return portal


async def get_valid_token(portal: Portal, db: Session) -> str:
    if portal.uninstalled_at is not None:
        raise RuntimeError(f"Portal {portal.member_id} has been uninstalled — refusing token refresh")
    if portal.payment_required_at is not None:
        raise PaymentRequiredError(f"Portal {portal.member_id} subscription expired")
    if is_token_expired(portal):
        portal = await refresh_token(portal, db)
    return portal.access_token
