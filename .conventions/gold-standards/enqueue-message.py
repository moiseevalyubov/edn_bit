# Gold standard: enqueue message in router (incoming and outgoing)
# Files: app/routers/incoming.py, app/routers/handler.py

# --- Incoming webhook (enqueue to send to Bitrix24) ---
# Dual dedup: check both delivered (Message) and queued (MessageDeliveryTask)
if msg_id:
    if db.query(Message).filter_by(
        channel_id=channel.id, direction="incoming", max_message_id=msg_id
    ).first():
        return JSONResponse({"status": "ok"})  # already delivered
    if db.query(MessageDeliveryTask).filter_by(
        channel_id=channel.id, direction="incoming", max_message_id=msg_id
    ).first():
        return JSONResponse({"status": "ok"})  # already queued

try:
    enqueue_incoming(db=db, channel=channel, msg_type="text", ...)
except IntegrityError:
    db.rollback()
    return JSONResponse({"status": "ok"})   # concurrent duplicate — silently deduplicate
except Exception as e:
    logger.error("Incoming: failed to enqueue: %s", e)
    return JSONResponse({"error": "service_unavailable"}, status_code=503)

# --- Outgoing handler (enqueue to send to edna/MAX Bot) ---
# File download MUST stay inline — Bitrix24 SIGN expires quickly
async with httpx.AsyncClient(follow_redirects=True) as client:
    resp = await client.get(bitrix_url, timeout=30)
resp.raise_for_status()
file_key = cache_file(resp.content, resp.headers.get("content-type", "application/octet-stream"), ext)

enqueue_outgoing(db=db, channel=channel, msg_type="media", max_id=chat_id,
                 file_key=file_key, ...)  # store file_key, NOT the URL
# Worker calls make_signed_url(file_key) to reconstruct the URL at send time

# Rules:
# - incoming.py: 400 on JSON parse error, 503 on DB/enqueue error, 200 on all other paths
# - handler.py: ALWAYS return 200 OK — Bitrix24 retries on non-200 (creates duplicates)
# - Outgoing files: download inline, store file_key in payload (never the download URL)
# - Dual dedup for incoming only — outgoing has no equivalent edna-side dedup needed
