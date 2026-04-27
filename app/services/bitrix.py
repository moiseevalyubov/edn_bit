import logging
import time

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Portal
from app.services.token import get_valid_token

logger = logging.getLogger(__name__)
CONNECTOR_ID = "max_bot"


def call_bitrix(portal: Portal, db: Session, method: str, params: dict) -> dict:
    token = get_valid_token(portal, db)
    url = f"{portal.client_endpoint}{method}"
    response = httpx.post(url, json={**params, "auth": token}, timeout=10)
    if not response.is_success:
        logger.error("Bitrix24 API error [%s %s]: %s", response.status_code, method, response.text[:500])
    response.raise_for_status()
    return response.json()


def register_connector(portal: Portal, db: Session) -> None:
    call_bitrix(
        portal,
        db,
        "imconnector.register",
        {
            "ID": CONNECTOR_ID,
            "NAME": "MAX Bot",
            "ICON": {
                "DATA_IMAGE": (
                    "data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22"
                    "%20viewBox%3D%220%200%2048%2048%22%3E%3Ccircle%20cx%3D%2224%22%20cy%3D%2224%22"
                    "%20r%3D%2224%22%20fill%3D%22%23005FF9%22%2F%3E%3Ctext%20x%3D%2212%22%20y%3D%2232%22"
                    "%20font-size%3D%2224%22%20fill%3D%22white%22%3EM%3C%2Ftext%3E%3C%2Fsvg%3E"
                ),
                "COLOR": "#005FF9",
            },
            "PLACEMENT_HANDLER": f"{settings.app_base_url}/settings",
            "COMMENT": "Подключение каналов MAX Bot к Открытым линиям",
        },
    )


def bind_events(portal: Portal, db: Session) -> None:
    handler_url = f"{settings.app_base_url}/handler"
    if not settings.app_base_url:
        logger.error("APP_BASE_URL is not set — event handler URL will be invalid: %r", handler_url)
    else:
        logger.info("Binding events with handler_url=%s", handler_url)
    for event in [
        "OnImConnectorMessageAdd",
        "OnImConnectorDialogStart",
        "OnImConnectorDialogFinish",
    ]:
        call_bitrix(portal, db, "event.bind", {"event": event, "handler": handler_url})
        logger.info("Bound event %s → %s", event, handler_url)


def get_open_lines(portal: Portal, db: Session) -> list:
    result = call_bitrix(portal, db, "imopenlines.config.list.get", {})
    return result.get("result", [])


def create_open_line(portal: Portal, db: Session, name: str) -> str:
    result = call_bitrix(
        portal,
        db,
        "imopenlines.config.add",
        {"fields": {"LINE_NAME": name, "QUEUE_TYPE": 0}},
    )
    line_id = str(result.get("result", ""))
    if not line_id:
        raise ValueError("Битрикс24 не вернул ID созданной линии")
    return line_id


def activate_connector(portal: Portal, db: Session, line_id: str) -> None:
    call_bitrix(
        portal,
        db,
        "imconnector.activate",
        {"CONNECTOR": CONNECTOR_ID, "LINE": int(line_id), "ACTIVE": "1"},
    )


def send_message_to_bitrix(
    portal: Portal,
    db: Session,
    chat_id: str,
    user_id: str,
    user_name: str,
    text: str,
    msg_id: str,
) -> dict:
    line_id = portal.open_line_id or "0"
    result = call_bitrix(
        portal,
        db,
        "imconnector.send.messages",
        {
            "CONNECTOR": CONNECTOR_ID,
            "LINE": int(line_id),
            "MESSAGES": [
                {
                    "user": {
                        "id": user_id,
                        "name": user_name,
                        "skip_phone_validate": "Y",
                    },
                    "message": {
                        "id": msg_id,
                        "date": int(time.time()),
                        "text": text,
                    },
                    "chat": {"id": chat_id},
                }
            ],
        },
    )
    return result


def send_delivery_status(
    portal: Portal,
    db: Session,
    line_id: int,
    bitrix_chat_id: int,
    bitrix_message_id: int,
    chat_id: str,
) -> None:
    call_bitrix(
        portal,
        db,
        "imconnector.send.status.delivery",
        {
            "CONNECTOR": CONNECTOR_ID,
            "LINE": line_id,
            "MESSAGES": [
                {
                    "im": {"chat_id": bitrix_chat_id, "message_id": bitrix_message_id},
                    "message": {"id": ["delivered"], "date": int(time.time())},
                    "chat": {"id": chat_id},
                }
            ],
        },
    )
