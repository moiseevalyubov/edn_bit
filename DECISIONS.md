# Feature: async-media — Decisions & Definition of Done

## Definition of Done

- No sync `httpx` calls inside async FastAPI handlers (event loop never blocked)
- `app/services/maxbot.py` exports async functions: `send_message`, `send_image`, `send_video`, `send_audio`, `send_document`
- `app/routers/handler.py` detects Bitrix24 attachment type and dispatches to the correct maxbot function
- All routers `await` async service calls — no unawaited coroutine warnings at runtime
- `review.md` items #1 (sync httpx) and #4 (media manifest) marked as fixed
- App starts without import errors: `uvicorn app.main:app --host 0.0.0.0 --port 8000`

---

## Risks

### RISK-1: api.py sync route handlers calling async bitrix functions
**Severity:** CRITICAL
**Affected tasks:** #2, #4
**Detail:** Five route handlers in `api.py` are plain `def`. After Task #2 makes bitrix functions async, calling them without `await` inside a sync handler returns a coroutine object silently — no exception, just wrong data or None returned to the client.
**Confirmed affected handlers:**
- `list_open_lines()` (line 51) — calls `get_open_lines()`
- `create_line()` (line 61) — calls `create_open_line()`, `activate_connector()`
- `set_open_line()` (line 74) — calls `activate_connector()`
- `repair_endpoint()` (line 86) — calls `register_connector()`, `bind_events()`
- `disconnect_channel()` (line 108) — no bitrix calls, safe to leave as `def`
**Fix:** Task #4 converts the four affected handlers to `async def` then adds `await`.

### RISK-2: Bitrix24 file attachment format undocumented
**Severity:** MAJOR
**Affected tasks:** #3
**Detail:** The `OnImConnectorMessageAdd` event docs only describe `message.text`. File attachment fields are not officially documented. Best available assumption: `msg["message"]["params"]` contains `FILE` with subfields `LINK`, `NAME`, `CONTENT_TYPE`.
**Mitigation:** Implement detection against the assumed format. Log the full `msg["message"]` payload whenever `params` is non-empty. Add graceful fallback: if attachment format doesn't match expectation, log a warning and skip rather than crash.

### RISK-SEC-1: httpx debug logging may expose X-API-KEY in production
**Severity:** MEDIUM
**Status:** Pre-existing, deferred — out of scope for this feature
**Detail:** If `HTTPX_LOG_LEVEL` is set to `debug` or `trace` in the production environment, httpx logs full request headers including the `X-API-KEY` sent to the edna API. This key would appear in plaintext in application logs.
**Mitigation (future task):** Verify `HTTPX_LOG_LEVEL` is not set to `debug` or `trace` in any production config or deployment env. Add a startup warning if detected.

### RISK-SEC-2: /incoming webhook has no HMAC/signature verification
**Severity:** MEDIUM
**Status:** Pre-existing, deferred — out of scope for this feature
**Detail:** `app/routers/incoming.py` accepts any POST request to `/incoming` without verifying that it originates from the edna platform. Any caller who knows the URL can inject arbitrary messages into Bitrix24. This risk increases as the media feature routes more traffic through this endpoint.
**Mitigation (future task):** Add HMAC signature verification using a shared secret from the edna platform, rejecting requests with missing or invalid signatures.

### RISK-3: Missing await anywhere in the cascade causes silent no-op
**Severity:** MAJOR
**Affected tasks:** #2, #3, #4
**Detail:** Python does not raise an error when a coroutine is called without `await` — it returns a coroutine object. The bug is invisible at import time and only shows as silent wrong runtime behavior.
**Full call-site inventory:**

| File | Handler | Sync/Async | Calls | Fix needed |
|------|---------|-----------|-------|-----------|
| api.py:51 | `list_open_lines` | `def` | `get_open_lines` | → `async def` + `await` |
| api.py:61 | `create_line` | `def` | `create_open_line`, `activate_connector` | → `async def` + `await` |
| api.py:74 | `set_open_line` | `def` | `activate_connector` | → `async def` + `await` |
| api.py:86 | `repair_endpoint` | `def` | `register_connector`, `bind_events` | → `async def` + `await` |
| api.py:108 | `disconnect_channel` | `def` | none | no change needed |
| incoming.py:23 | `incoming` | `async def` | `send_message_to_bitrix` | add `await` |
| install.py:17 | `install` | `async def` | `register_connector`, `bind_events` | add `await` |
| handler.py:77 | `handler` | `async def` | `_handle_outgoing_message` | add `await` (Task #3) |
| handler.py:122 | `_handle_outgoing_message` | `def` | `send_message`, `send_delivery_status` | → `async def` + `await` (Task #3) |

---

## Architectural Decisions

### AD-1: token.py must be converted to async alongside bitrix.py (Task #2 scope)

`app/services/token.py:refresh_token()` uses sync `httpx.post`. It is called by `get_valid_token()`, which is called by `call_bitrix()`. Task #2 must convert both `refresh_token()` and `get_valid_token()` to `async def` using `async with httpx.AsyncClient()`. Leaving token.py sync makes the bitrix.py conversion incomplete — token refresh still blocks the event loop.

### AD-2: Four api.py handlers must become async def — disconnect_channel does not

`disconnect_channel` makes no bitrix calls — leave as `def`. The other four must become `async def` to legally use `await`.

### AD-3: handler.py empty-text guard must be restructured before media detection

Current guard at lines 138-143 uses `continue` when text is empty after BBCode strip. Media-only messages have no text and are silently dropped before any attachment detection code runs. Task #3 must change the guard: skip only when text is empty AND no attachment field is present.

### AD-4: Each maxbot media function uses its own AsyncClient

Each function (`send_image`, `send_video`, etc.) opens `async with httpx.AsyncClient()` independently. No shared client singleton — keeps functions stateless, consistent with the converted `send_message`.

### AD-5: Assumed Bitrix24 attachment payload structure

Best available assumption: `msg["message"]["params"]["FILE"]` with subfields `LINK`, `NAME`, `CONTENT_TYPE`. This must be logged and treated as provisional until verified against a live Bitrix24 event.

### AD-6: settings_page.py requires no changes

Makes no calls to any bitrix service function. Excluded from Task #4 scope.
