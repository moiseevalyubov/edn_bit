import logging

from fastapi import APIRouter, HTTPException, Query, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session
from urllib.parse import quote

from app.services.file_cache import get as get_cached_file, verify_signed_url
from app.database import get_db
from app.services import telegram_files, telegram

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/file/{file_key}")
async def serve_file(
    file_key: str,
    token: str | None = Query(default=None),
    expires: int | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """Serve HMAC-protected legacy cache or durable Telegram media."""
    if token is None or expires is None:
        raise HTTPException(status_code=403, detail="Access denied")
    if not verify_signed_url(file_key, token, expires):
        raise HTTPException(status_code=403, detail="Access denied")
    if file_key.startswith("tg-"):
        try:
            entry = telegram_files.get(db, file_key)
        except telegram.TelegramError:
            raise HTTPException(status_code=404, detail="File not found") from None
        disposition = "inline" if entry.content_type.startswith("image/") else "attachment"
        content = entry.content
        logger.info("Serving Telegram file: message_id=%s bytes=%s mime=%s",
                    entry.message_id, len(content), entry.content_type)
        return Response(content=content, media_type=entry.content_type, headers={
            "Content-Disposition": disposition + "; filename*=UTF-8''" + quote(entry.name, safe=""),
            "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
        })
    entry = get_cached_file(file_key)
    if entry is None:
        logger.warning("File not found in cache: %s", file_key)
        raise HTTPException(status_code=404, detail="File not found")
    content, content_type = entry
    logger.info("Serving cached file: %s (%d bytes, %s)", file_key, len(content), content_type)
    return Response(content=content, media_type=content_type)
