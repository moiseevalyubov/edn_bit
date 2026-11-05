"""Permanent messageservice callback. No external send in the HTTP request."""
import json
import logging
import re
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.handler import _parse_php_form
from app.services.crm_outreach import CrmRequestError, accept_crm_message
from app.services.rate_limiter import rate_limiter

router = APIRouter(prefix="/crm/messages")
logger = logging.getLogger(__name__)
MAX_BODY_BYTES = 65536


@router.post("/send")
async def send(request: Request, db: Session = Depends(get_db)):
    if not rate_limiter.is_allowed("crm:callbacks"):
        return JSONResponse({"error": "too_many_requests"}, status_code=429)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
    try:
        decoded = body.decode("utf-8")
        if "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
            flat = parse_qs(decoded, keep_blank_values=True, max_num_fields=128)
            for key in flat:
                if key.count("[") > 8 or any(int(index) > 100 for index in re.findall(r"\[(\d+)\]", key)):
                    raise ValueError("Invalid form nesting")
            data = _parse_php_form(flat)
        else:
            data = json.loads(decoded)
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
    except (ValueError, TypeError, KeyError, IndexError, RecursionError):
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    try:
        # A concurrent unique-key conflict can be retried once as a lookup of
        # the committed receipt; errors never become a false accepted response.
        for attempt in range(2):
            try:
                accept_crm_message(db, data)
                db.commit()
                return {"status": "ok"}
            except IntegrityError:
                db.rollback()
                if attempt:
                    raise
    except CrmRequestError as exc:
        db.rollback()
        return JSONResponse({"error": exc.code}, status_code=exc.status_code)
    except SQLAlchemyError:
        db.rollback()
        # SQL exceptions may include bound message text/phone: do not dump them.
        logger.error("CRM receipt could not be saved; no acceptance returned")
        return JSONResponse({"error": "service_unavailable"}, status_code=503)
