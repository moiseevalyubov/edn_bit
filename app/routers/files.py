import logging
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/file/{filename}")
async def proxy_bitrix_file(filename: str, dl: str):
    """Proxy Bitrix24 file downloads so edna sees a URL with a proper file extension."""
    parsed = urlparse(dl)
    if not parsed.hostname or not (
        parsed.hostname.endswith(".bitrix24.ru") or parsed.hostname == "bitrix24.ru"
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(dl, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Failed to fetch file from Bitrix24: %s", e)
        raise HTTPException(status_code=502, detail="Failed to fetch file")

    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "application/octet-stream"),
    )
