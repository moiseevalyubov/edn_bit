import logging

import httpx

EDNA_API_BASE = "https://app.edna.ru/api"
MAXBOT_API_URL = f"{EDNA_API_BASE}/v1/out-messages/max-bot"
VALID_MEDIA_TYPES = {"IMAGE", "VIDEO", "AUDIO", "DOCUMENT"}
logger = logging.getLogger(__name__)


class WebhookSetupError(Exception):
    """Raised when automatic webhook registration in edna fails for a reason
    we can explain to the user (wrong Sender ID, channel not active, etc.).

    The message is a ready-to-show Russian string for the settings UI."""


async def _post(api_key: str, sender: str, max_id: str, content: dict) -> dict:
    payload = {"sender": sender, "maxId": max_id, "content": content}
    logger.info("edna request payload: %s", payload)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            MAXBOT_API_URL,
            headers={"Content-Type": "application/json", "X-API-KEY": api_key},
            json=payload,
            timeout=10,
        )
    if not response.is_success:
        logger.error("edna error %s: %s", response.status_code, response.text[:500])
    response.raise_for_status()
    return response.json()


async def send_message(api_key: str, sender: str, max_id: str, text: str) -> dict:
    return await _post(api_key, sender, max_id, {"type": "TEXT", "text": text})


async def send_media(api_key: str, sender: str, max_id: str, content_type: str, url: str, name: str, caption: str | None = None) -> dict:
    if content_type not in VALID_MEDIA_TYPES:
        raise ValueError(f"Invalid content_type '{content_type}'. Must be one of {VALID_MEDIA_TYPES}")
    if not url or not name:
        raise ValueError("url and name must be non-empty strings")
    content = {"type": content_type, "url": url}
    if caption is not None:
        content["caption"] = caption
    return await _post(api_key, sender, max_id, content)


# --- Channel / callback management (used to auto-configure the webhook URL) ---

_CALLBACK_ERROR_MESSAGES = {
    "error-callback-url-max-length": "URL webhook слишком длинный (более 500 символов).",
    "error-callback-url-not-https": "URL webhook должен использовать HTTPS.",
    "error-callback-url-not-available": "edna не смогла открыть наш URL для проверки. Попробуйте ещё раз чуть позже.",
    "error-subject-unknown": "edna не распознала идентификатор канала (subjectId).",
    "url-not-specified": "Не передан URL webhook.",
}


def _callback_error_message(code) -> str:
    return _CALLBACK_ERROR_MESSAGES.get(code, f"edna отклонила настройку webhook (код: {code}).")


def _http_status_message(status: int, action: str) -> str:
    if status in (401, 403):
        return "edna отклонила запрос: неверный или недействительный API-ключ канала."
    return f"edna вернула ошибку {status} при {action}."


async def get_channel_profiles(api_key: str) -> list:
    """GET /api/channel-profile — список всех каналов для этого API-ключа.

    Возвращает массив объектов канала (поля subject, subjectId, type, active и т.д.).
    Фильтр types не передаём — нужны все каналы, чтобы найти наш по subject."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{EDNA_API_BASE}/channel-profile",
            headers={"X-API-KEY": api_key},
            timeout=10,
        )
    if not response.is_success:
        logger.error("edna channel-profile error %s: %s", response.status_code, response.text[:500])
    response.raise_for_status()
    return response.json()


async def set_in_message_callback(api_key: str, subject_id: int, callback_url: str) -> dict:
    """POST /api/callback/set — прописать URL для входящих сообщений у канала."""
    payload = {"subjectId": subject_id, "inMessageCallbackUrl": callback_url}
    logger.info("edna callback/set payload: %s", payload)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{EDNA_API_BASE}/callback/set",
            headers={"Content-Type": "application/json", "X-API-KEY": api_key},
            json=payload,
            timeout=20,
        )
    if not response.is_success:
        logger.error("edna callback/set error %s: %s", response.status_code, response.text[:500])
    response.raise_for_status()
    return response.json()


async def configure_incoming_webhook(api_key: str, sender: str, callback_url: str) -> dict:
    """Найти в edna канал, у которого subject совпадает с sender, и прописать ему
    callback_url как webhook для входящих сообщений.

    Возвращает {"subject_id": int, "type": str|None} при успехе.
    Бросает WebhookSetupError с готовым для пользователя текстом при логической
    ошибке (канал не найден, не активирован, edna отклонила URL). Сетевые ошибки
    httpx пробрасываются наверх — их обрабатывает вызывающий код."""
    try:
        channels = await get_channel_profiles(api_key)
    except httpx.HTTPStatusError as e:
        raise WebhookSetupError(_http_status_message(e.response.status_code, "получении списка каналов"))
    except httpx.HTTPError as e:
        raise WebhookSetupError(
            f"Не удалось связаться с edna при получении списка каналов ({type(e).__name__}). "
            "Попробуйте ещё раз чуть позже."
        )
    if not isinstance(channels, list):
        raise WebhookSetupError("Неожиданный ответ от edna при получении списка каналов.")

    match = next(
        (c for c in channels if isinstance(c, dict) and c.get("subject") == sender),
        None,
    )
    if not match:
        raise WebhookSetupError(
            "В edna не найден канал с таким Sender ID. Проверьте, что Sender ID точно "
            "совпадает с названием подписки (subject) в личном кабинете edna."
        )

    subject_id = match.get("subjectId")
    if not subject_id:
        raise WebhookSetupError(
            "Канал найден в edna, но ещё не активирован (нет subjectId). "
            "Дождитесь активации канала и подключите его снова."
        )

    try:
        result = await set_in_message_callback(api_key, subject_id, callback_url)
    except httpx.HTTPStatusError as e:
        raise WebhookSetupError(_http_status_message(e.response.status_code, "установке webhook"))
    except httpx.HTTPError as e:
        raise WebhookSetupError(
            f"Не удалось связаться с edna при установке webhook ({type(e).__name__}). "
            "Попробуйте ещё раз чуть позже."
        )
    code = result.get("code") if isinstance(result, dict) else None
    if code != "ok":
        raise WebhookSetupError(_callback_error_message(code))

    return {"subject_id": subject_id, "type": match.get("type")}
