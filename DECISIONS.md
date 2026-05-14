# DECISIONS — REL-4 + REL-1 + REL-2: Message Delivery Queue

## Feature goal

Replace fire-and-forget HTTP calls with a persistent queue:
- REL-4: `MessageDeliveryTask` table in DB, background worker processes it
- REL-1: Exponential backoff retry (1s → 5s → 30s → 2m → 10m → 30m, 6 attempts max)
- REL-2: `/incoming` returns 200 only if task saved; 503 if DB down; 400 if payload invalid

## Architecture decisions

### ADR-1: asyncio background worker, no Celery/Redis
Rationale: app runs on Render single-instance (free tier). Adding Redis/Celery would require paid tier and infrastructure complexity. An `asyncio.create_task` worker in the same process is sufficient for the current scale. Revisit if multi-instance needed.

### ADR-2: Queue backed by PostgreSQL, no separate queue service
Rationale: PostgreSQL is already required. Avoids new dependencies. `MessageDeliveryTask` table with status field replaces a message broker for this scale.

### ADR-3: Payload stored as JSON string in Text column
Rationale: avoids PostgreSQL-specific JSON column type; compatible with SQLAlchemy generic types used throughout.

### ADR-4: Worker resets "processing" → "pending" on startup
Rationale: if the app crashes while processing a task, it stays in "processing" forever. On next startup, these are reset to "pending" so they get retried.

### ADR-5: Dual deduplication for incoming messages
Rationale: with a queue, the Message row is created only after the task is processed (not immediately on webhook arrival). So edna retries could create duplicate tasks. Solution: check both Message table AND MessageDeliveryTask table for the same max_message_id before enqueueing.

### ADR-6: handler.py (outgoing) still returns 200 OK always
Rationale: Bitrix24 retries events on non-200. If we return 503 from /handler, Bitrix24 will retry and create duplicate outgoing tasks. The Bitrix24 event handler MUST always return 200 OK.

### ADR-7: send_delivery_status called by worker on success
Rationale: currently called after send_message/send_media in handler.py. With a queue, it must be called by the worker after successful send. Keeps status reporting accurate.

### ADR-8: No retry for outgoing delivery_status failures
Rationale: delivery_status is best-effort UX feedback. If it fails, the message was already delivered. Retrying delivery_status adds complexity without reliability benefit.

## Task list

| # | Task | Files | Depends on |
|---|------|-------|-----------|
| 1 | MessageDeliveryTask model + migration | app/models.py, app/main.py | — |
| 2 | delivery_worker.py — queue processor | app/services/delivery_worker.py (new) | Task 1 |
| 3 | Refactor incoming.py to use queue | app/routers/incoming.py | Task 2 |
| 4 | Refactor handler.py + wire startup | app/routers/handler.py, app/main.py | Task 2 |
| 5 | Update .conventions/ | .conventions/ | All above |

## Feature Definition of Done

- App starts without errors: `python -c "from app.main import app; print('OK')"`
- No unresolved CRITICAL issues
- incoming.py no longer calls Bitrix24 directly — creates a task instead
- handler.py no longer calls MAX Bot directly — creates a task instead
- Worker processes tasks with correct retry schedule
- 503 returned from /incoming when DB is unavailable
- Worker resets stale "processing" tasks on startup
- .conventions/ updated with new patterns
- All committed code matches gold standards

## Plan validation notes (Tech Lead)

### TL-1: incoming.py dedup — IntegrityError catch moves to task insert
The existing IntegrityError catch on `db.commit()` (currently guards against duplicate Message inserts) must move to the `MessageDeliveryTask` insert. The Message row is now created by the worker, not the webhook handler. Dedup = check Message table (already delivered) + MessageDeliveryTask table (enqueued not yet delivered) by max_message_id.

### TL-2: File download stays inline in handler.py — file_key goes in payload
Bitrix24 file download URLs expire quickly (SIGN). handler.py must download the file inline (before enqueue) and put only the `file_key` (from file_cache) into the task payload. Worker calls `make_signed_url(file_key)` to reconstruct the URL. Do NOT defer the download to the worker.

### TL-3: Worker uses SessionLocal() directly — no Depends(get_db)
Background worker has no request context. Must create sessions as `db = SessionLocal()` with `try/finally: db.close()`. Never use `Depends(get_db)` in the worker.

## Risk register

### RISK-1 (CRITICAL): File cache expires before worker processes outgoing task
File cache TTL = 10 min. If the worker queue is backlogged, `file_key` may no longer be in cache when the worker runs. Calling `make_signed_url(file_key)` returns a URL that returns 404, causing infinite transient retries.
**Mitigation (Task 2):** Before processing an outgoing file task, check `file_key in file_cache._cache`. If missing → mark `failed` immediately with `last_error = "file_cache_expired"`. Do not retry.

### RISK-2 (CRITICAL): Worker asyncio task dies silently on unhandled exception
If the worker loop raises an unhandled exception, the asyncio.Task is cancelled and never restarted. Messages queue up as "pending" forever with no processing.
**Mitigation (Task 2):** Wrap the entire worker loop body in `while True: try: ... except Exception: logger.exception("worker loop error"); await asyncio.sleep(5)`. Never let the outer loop die.

### RISK-3 (MAJOR): PaymentRequiredError / uninstalled portal classified as transient
`get_valid_token()` raises `PaymentRequiredError` or `RuntimeError` — neither has `.response.status_code`. The error classifier would fall through to the network-error branch and classify as transient → 6 useless retries.
**Mitigation (Task 2):** Check `isinstance(exc, (PaymentRequiredError, RuntimeError))` before the httpx check → classify as permanent.

### RISK-4 (MAJOR): Dedup race condition on simultaneous identical incoming webhooks
Two POST `/incoming` with same `max_message_id` arrive simultaneously, both pass the "task exists?" check, both insert. Worker sends duplicate message to Bitrix.
**Mitigation (Task 1 + Task 3):** Add `UNIQUE INDEX on message_delivery_tasks(channel_id, max_message_id) WHERE direction='incoming'` in migration. Task 3 catches `IntegrityError` on task insert → return 200 (same as existing dedup).

### RISK-5 (MAJOR): DB session leak if exception escapes worker task processing
If an exception propagates before `db.close()`, the session leaks and eventually exhausts the connection pool.
**Mitigation (Task 2):** Every task processing block must use `db = SessionLocal(); try: ... finally: db.close()`. No exceptions to this rule.

### RISK-6 (MAJOR): Stale portal object across retries
`get_valid_token()` may refresh and commit a new OAuth token. If the worker reuses a portal object loaded before the first attempt, subsequent retries see stale token data → 401 on every retry → task goes dead.
**Mitigation (Task 2):** Reload portal from DB at the start of EVERY processing attempt with a fresh session. Never cache the portal object across retries.

### RISK-7 (ACCEPTED): Event loop blocking from sync SQLAlchemy in async worker
Sync `create_engine` blocks the asyncio event loop on DB queries (~5ms per call). Same pattern already exists in all router handlers. Accepted as known limitation — consistent with codebase, DB ops are fast. Revisit if moving to async SQLAlchemy in the future.

## Canonical task payload schemas

### Incoming payload (task_type="send_to_bitrix")
```json
{
  "msg_type": "text|location|file",
  "chat_id": "subscriber_identifier",
  "user_id": "subscriber_id or subscriber_identifier",
  "user_name": "display name",
  "msg_id": "edna message id",
  "content_type": "TEXT|IMAGE|DOCUMENT|AUDIO|VIDEO|LOCATION|VOICE",
  "subscriber_identifier": "subscriber_identifier",
  "raw_payload": "truncated JSON string <=2000 chars",
  "text": "message text or null",
  "file_url": "edna file URL or null",
  "file_name": "filename or null",
  "caption": "media caption or null"
}
```

### Outgoing payload (task_type="send_to_edna")
```json
{
  "msg_type": "text|media",
  "max_id": "chat_id (subscriber_identifier)",
  "im_chat_id": "bitrix im chat_id or null",
  "im_message_id": "bitrix im message_id or null",
  "line_id": "open line id or null",
  "raw_payload": "truncated str(data) <=2000 chars",
  "text": "message text or null",
  "content_type": "IMAGE|VIDEO|AUDIO|DOCUMENT or null",
  "file_key": "file_cache key (NOT the URL) or null",
  "file_name": "filename or null",
  "caption": "media caption or null"
}
```
