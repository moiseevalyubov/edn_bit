# Gold standard: Bitrix24 event handler pattern
# File: app/routers/handler.py

# 1. Routing inside handler() — after portal lookup, app_token verify, update_portal_tokens:
if event == "ONIMCONNECTORMESSAGEADD":
    await _handle_outgoing_message(data, portal, db)
elif event == "ONAPPUNINSTALL":
    await _handle_app_uninstall(portal, db)
return JSONResponse({"status": "ok"})

# 2. Private handler function signature:
async def _handle_outgoing_message(data: dict, portal: Portal, db: Session) -> None:
    # receives raw event data when needed
    ...

async def _handle_app_uninstall(portal: Portal, db: Session) -> None:
    # data not needed — omit it
    ...

# Rules:
# - Event names compared in UPPERCASE (data.get("event", "").upper())
# - bind uses CamelCase: "OnImConnectorMessageAdd", handler compares UPPERCASE: "ONIMCONNECTORMESSAGEADD"
# - Always return JSONResponse({"status": "ok"}) — Bitrix24 retries on non-200
# - Guard uninstalled portals: if portal.uninstalled_at is not None and event != "ONAPPUNINSTALL": return early
