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

# REL-3: unique index to prevent duplicate incoming messages (edna retries)
with engine.connect() as _conn:
    try:
        _conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_incoming_dedup "
            "ON messages(channel_id, direction, max_message_id) "
            "WHERE max_message_id IS NOT NULL"
        ))
        _conn.commit()
        logger.info("Migration: added dedup index on messages")
    except Exception as _e:
        _conn.rollback()
        logger.warning("Migration messages dedup index skipped: %s", _e)

# SEC-2: cleanup stale anti-replay fingerprints at startup
with engine.connect() as _conn:
    try:
        _conn.execute(text("DELETE FROM seen_events WHERE seen_at < now() - interval '1 hour'"))
        _conn.commit()
        logger.info("Migration: cleaned up stale seen_events")
    except Exception as _e:
        _conn.rollback()
        logger.warning("Cleanup seen_events skipped: %s", _e)

# SEC-3: encrypt plaintext tokens/keys that exist before this migration
from app.crypto import encrypt as _encrypt, is_encrypted as _is_encrypted

try:
    with engine.begin() as _conn:
        _conn.execute(text("SELECT pg_advisory_xact_lock(20260514)"))
        _migrated = 0
        for _row in _conn.execute(text("SELECT id, access_token, refresh_token FROM portals")).fetchall():
            _updates = {}
            if _row.access_token and not _is_encrypted(_row.access_token):
                _updates["access_token"] = _encrypt(_row.access_token)
            if _row.refresh_token and not _is_encrypted(_row.refresh_token):
                _updates["refresh_token"] = _encrypt(_row.refresh_token)
            if _updates:
                _set = ", ".join(f"{k} = :{k}" for k in _updates)
                _updates["id"] = _row.id
                _conn.execute(text(f"UPDATE portals SET {_set} WHERE id = :id"), _updates)
                _migrated += 1
        for _row in _conn.execute(text("SELECT id, api_key FROM channels")).fetchall():
            if _row.api_key and not _is_encrypted(_row.api_key):
                _conn.execute(
                    text("UPDATE channels SET api_key = :api_key WHERE id = :id"),
                    {"api_key": _encrypt(_row.api_key), "id": _row.id},
                )
                _migrated += 1
        if _migrated:
            logger.info("Migration SEC-3: encrypted %d plaintext records", _migrated)
        else:
            logger.info("Migration SEC-3: all records already encrypted")
except Exception as _e:
    logger.warning("Migration SEC-3 skipped: %s", _e)

# T-2: add payment_required_at column for subscription expiry tracking
with engine.connect() as _conn:
    try:
        _conn.execute(text("ALTER TABLE portals ADD COLUMN payment_required_at TIMESTAMP"))
        _conn.commit()
        logger.info("Migration T-2: added payment_required_at column to portals")
    except Exception:
        _conn.rollback()

# REL-4: dedup index for incoming delivery tasks (prevents duplicate enqueue on concurrent webhooks)
with engine.connect() as _conn:
    try:
        _conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_tasks_incoming_dedup "
            "ON message_delivery_tasks(channel_id, max_message_id) "
            "WHERE direction = 'incoming' AND max_message_id IS NOT NULL"
        ))
        _conn.commit()
        logger.info("Migration REL-4: added dedup index on message_delivery_tasks")
    except Exception as _e:
        _conn.rollback()
        logger.warning("Migration REL-4 dedup index skipped: %s", _e)

# UX: store edna subjectId/type captured during automatic webhook setup
with engine.connect() as _conn:
    for _col, _coltype in (("subject_id", "INTEGER"), ("channel_type", "TEXT")):
        try:
            _conn.execute(text(f"ALTER TABLE channels ADD COLUMN {_col} {_coltype}"))
            _conn.commit()
            logger.info("Migration: added channels.%s column", _col)
        except Exception:
            _conn.rollback()

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
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
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


@app.on_event("startup")
async def startup():
    from app.services.delivery_worker import start_worker
    start_worker()
