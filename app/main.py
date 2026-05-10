import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.database import Base, engine
from app.routers import api, files, handler, incoming, install, settings_page
from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

Base.metadata.create_all(bind=engine)

# SEC-1: backfill webhook_token for channels created before this field existed
from sqlalchemy import text
with engine.connect() as _conn:
    try:
        _conn.execute(text("ALTER TABLE channels ADD COLUMN webhook_token TEXT"))
        _conn.commit()
        logger.info("Migration: added webhook_token column")
    except Exception:
        _conn.rollback()
    try:
        _conn.execute(text(
            "UPDATE channels SET webhook_token = replace(gen_random_uuid()::TEXT, '-', '') "
            "WHERE webhook_token IS NULL"
        ))
        _conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_webhook_token ON channels(webhook_token)"
        ))
        _conn.commit()
        logger.info("Migration: backfilled webhook_token for existing channels")
    except Exception as _e:
        _conn.rollback()
        logger.warning("Migration webhook_token backfill skipped: %s", _e)

# SEC-2: cleanup stale anti-replay fingerprints at startup
with engine.connect() as _conn:
    try:
        _conn.execute(text("DELETE FROM seen_events WHERE seen_at < now() - interval '1 hour'"))
        _conn.commit()
        logger.info("Migration: cleaned up stale seen_events")
    except Exception as _e:
        _conn.rollback()
        logger.warning("Cleanup seen_events skipped: %s", _e)

logger.info("DATABASE_URL configured: %s", settings.database_url.split("@")[-1])

if settings.app_base_url:
    logger.info("APP_BASE_URL configured: %s", settings.app_base_url)
else:
    logger.error("APP_BASE_URL is NOT set — event binding and webhook URLs will be broken!")

app = FastAPI(title="MAX Bot — Bitrix24 Connector", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = round((time.time() - start) * 1000)
    logger.info("%s %s → %d (%dms)", request.method, request.url.path, response.status_code, duration)
    return response


app.include_router(install.router)
app.include_router(handler.router)
app.include_router(incoming.router)
app.include_router(api.router)
app.include_router(files.router)
app.include_router(settings_page.router)


@app.get("/health")
def health():
    return {"status": "ok"}
