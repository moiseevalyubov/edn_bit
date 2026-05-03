# Gold standard: event binding pattern
# File: app/services/bitrix.py

async def bind_events(portal: Portal, db: Session) -> None:
    if not settings.app_base_url:
        logger.error("APP_BASE_URL is not set — cannot bind events")
        return
    handler_url = f"{settings.app_base_url}/handler"
    for event in [
        "OnImConnectorMessageAdd",
        "OnImConnectorDialogStart",
        "OnImConnectorDialogFinish",
        "OnAppUninstall",
    ]:
        try:
            await call_bitrix(portal, db, "event.bind", {"event": event, "handler": handler_url})
            logger.info("Bound event %s → %s", event, handler_url)
        except Exception:
            logger.exception("Failed to bind event %s — portal may be in partial state", event)
            raise  # partial bind = broken state, surface the error

# Rules:
# - bind uses CamelCase event names (Bitrix24 API requirement)
# - Each event in try/except — failure raises so install fails loudly
# - handler URL always = settings.app_base_url + "/handler"
