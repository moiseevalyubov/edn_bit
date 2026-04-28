import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.services.file_cache import get as get_cached_file

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/file/{file_key}")
async def serve_file(file_key: str):
    """Serve a pre-fetched file from in-memory cache."""
    entry = get_cached_file(file_key)
    if entry is None:
        logger.warning("File not found in cache: %s", file_key)
        raise HTTPException(status_code=404, detail="File not found")
    content, content_type = entry
    logger.info("Serving cached file: %s (%d bytes, %s)", file_key, len(content), content_type)
    return Response(content=content, media_type=content_type)
