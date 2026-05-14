# Gold standard: background delivery worker pattern
# File: app/services/delivery_worker.py

# 1. Worker uses SessionLocal() directly — never Depends(get_db)
db = SessionLocal()
try:
    task = db.query(MessageDeliveryTask).filter_by(status="pending").first()
    task.status = "processing"
    db.commit()
finally:
    db.close()  # ALWAYS close in finally — no exceptions

# 2. Extract all values from task BEFORE closing session (avoids DetachedInstanceError)
task_id = task.id
task_type = task.task_type
payload_str = task.payload

# 3. Each execute function opens its OWN fresh session (ensures fresh portal/token per retry)
async def _execute_outgoing(channel_id: int, payload: dict) -> None:
    db = SessionLocal()
    try:
        channel = db.query(Channel).filter_by(id=channel_id).first()
        portal = channel.portal  # reload fresh from DB every attempt — avoids stale token
        ...
    finally:
        db.close()

# 4. Worker loop MUST never die — outer try/except catches everything
async def _worker_loop() -> None:
    while True:
        try:
            processed = await _process_one_task()
            if not processed:
                await asyncio.sleep(2)
        except Exception:
            logger.exception("Worker loop error")  # includes traceback
            await asyncio.sleep(5)

# 5. Start worker from startup event — NOT from module import
def start_worker() -> None:
    asyncio.create_task(_worker_loop())

# Rules:
# - SessionLocal() in worker, never Depends(get_db) — no request context
# - Extract task fields before db.close() — once closed, accessing task fields raises DetachedInstanceError
# - Each retry opens a fresh DB session — so get_valid_token() sees up-to-date portal data
# - Outer loop catches ALL exceptions and sleeps — asyncio.Task must never be cancelled silently
