import logging

import httpx

EDNA_API_BASE = "https://app.edna.ru/api"
MAXBOT_API_URL = f"{EDNA_API_BASE}/v1/out-messages/max-bot"
# #16: the MAX channel is a different edna channel type with its own endpoint and
# body format. Sending to it through the MAX Bot endpoint is rejected outright
# ("default.subject.not_found"), so the two must never be mixed up.
MAX_API_URL = f"{EDNA_API_BASE}/v1/out-messages/max"
VALID_MEDIA_TYPES = {"IMAGE", "VIDEO", "AUDIO", "DOCUMENT"}
VALID_ID_TYPES = {"MAX_ID", "PHONE"}
logger = logging.getLogger(__name__)


class WebhookSetupError(Exception):
    """Raised when automatic webhook registration in edna fails for a reason
    we can explain to the user (wrong Sender ID, channel not active, etc.).

    The message is a ready-to-show Russian string for the settings UI.

    `fatal=True` means the channel can never work as configured (wrong Sender ID,
    invalid key, channel not active) — the caller must NOT create the channel.
    `fatal=False` means a transient problem (edna/network/proxy/cold start) — the
    caller still creates the channel and falls back to manual URL setup."""

    def __init__(self, message: str, fatal: bool = True):
        super().__init__(message)
        self.fatal = fatal


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


# --- #16: MAX channel (separate endpoint and body format from MAX Bot) ---


async def _post_max(api_key: str, sender: str, to_value: str, to_type: str, content: dict) -> dict:
    """POST /v1/out-messages/max — same idea as _post, different body shape.

    MAX Bot sends {"sender", "maxId", "content"}; MAX sends {"from", "to", "content"}
    where `to` is an object carrying the id AND its type (MAX_ID or PHONE)."""
    if to_type not in VALID_ID_TYPES:
        raise ValueError(f"Invalid to_type '{to_type}'. Must be one of {VALID_ID_TYPES}")
    payload = {"from": sender, "to": {"value": to_value, "type": to_type}, "content": content}
    logger.info("edna MAX request payload: %s", payload)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            MAX_API_URL,
            headers={"Content-Type": "application/json", "X-API-KEY": api_key},
            json=payload,
            timeout=10,
        )
    if not response.is_success:
        logger.error("edna MAX error %s: %s", response.status_code, response.text[:500])
    response.raise_for_status()
    return response.json()


async def send_message_max(api_key: str, sender: str, to_value: str, to_type: str, text: str) -> dict:
    return await _post_max(api_key, sender, to_value, to_type, {"type": "TEXT", "text": text})


async def send_media_max(
    api_key: str,
    sender: str,
    to_value: str,
    to_type: str,
    content_type: str,
    url: str,
    name: str,
    caption: str | None = None,
) -> dict:
    """Media message to a MAX channel.

    The MAX docs list IMAGE / VIDEO / DOCUMENT; AUDIO is sent as-is and, if edna
    rejects it, the operator gets the "undelivered" notice — see the open question
    in review.md (#16). The caption field is assumed to match MAX Bot (`caption`)
    and still needs confirming on a live channel."""
    if content_type not in VALID_MEDIA_TYPES:
        raise ValueError(f"Invalid content_type '{content_type}'. Must be one of {VALID_MEDIA_TYPES}")
    if not url or not name:
        raise ValueError("url and name must be non-empty strings")
    content = {"type": content_type, "url": url}
    if caption is not None:
        content["caption"] = caption
    return await _post_max(api_key, sender, to_value, to_type, content)


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
    Бросает WebhookSetupError при ошибке (см. флаг fatal в самом исключении):
    - fatal=True  — канал заведомо нерабочий или непроверяемый (неверный Sender ID,
      ключ невалиден 401, нет доступа к подписи 403, канал не активирован, edna не
      знает subjectId) → канал создавать нельзя;
    - fatal=False — временная проблема (сеть/прокси/edna 5xx/cold start) → канал
      создаётся, пользователю предлагается ручная настройка URL."""
    # --- 1) Список каналов (валидирует ключ + ищем канал по subject) ---
    try:
        channels = await get_channel_profiles(api_key)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 401:
            raise WebhookSetupError(
                "edna не приняла API-ключ (401). Проверьте, что ключ верный и активен.",
                fatal=True,
            )
        if status == 403:
            # 403 = ключ не может прочитать список каналов, значит мы НЕ можем
            # проверить, что подпись существует. Канал создавать нельзя.
            raise WebhookSetupError(
                "у API-ключа нет доступа к этой подписи (403). Проверьте разрешения этого "
                "ключа в edna или правильность названия подписи (Sender ID).",
                fatal=True,
            )
        raise WebhookSetupError(
            f"edna вернула ошибку {status} при получении списка каналов. Попробуйте ещё раз позже.",
            fatal=False,
        )
    except httpx.HTTPError as e:
        raise WebhookSetupError(
            f"Не удалось связаться с edna при получении списка каналов ({type(e).__name__}). "
            "Попробуйте ещё раз чуть позже.",
            fatal=False,
        )
    if not isinstance(channels, list):
        raise WebhookSetupError("Неожиданный ответ от edna при получении списка каналов.", fatal=False)

    match = next(
        (c for c in channels if isinstance(c, dict) and c.get("subject") == sender),
        None,
    )
    if not match:
        raise WebhookSetupError(
            "В edna не найден канал с таким Sender ID. Проверьте, что Sender ID точно "
            "совпадает с названием подписи (subject) в личном кабинете edna.",
            fatal=True,
        )

    subject_id = match.get("subjectId")
    if not subject_id:
        raise WebhookSetupError(
            "Канал найден в edna, но ещё не активирован (нет subjectId). "
            "Активируйте канал в edna и подключите его снова.",
            fatal=True,
        )

    # --- 2) Установка callback URL ---
    try:
        result = await set_in_message_callback(api_key, subject_id, callback_url)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 401:
            raise WebhookSetupError(
                "edna не приняла API-ключ (401) при установке webhook.", fatal=True
            )
        raise WebhookSetupError(
            f"edna вернула ошибку {status} при установке webhook. Попробуйте ещё раз позже.",
            fatal=False,
        )
    except httpx.HTTPError as e:
        raise WebhookSetupError(
            f"Не удалось связаться с edna при установке webhook ({type(e).__name__}). "
            "Попробуйте ещё раз чуть позже.",
            fatal=False,
        )
    code = result.get("code") if isinstance(result, dict) else None
    if code != "ok":
        # error-subject-unknown = битый конфиг канала → блокируем; остальные коды
        # (наш URL/доступность) — временные, оставляем ручной фолбэк.
        raise WebhookSetupError(_callback_error_message(code), fatal=(code == "error-subject-unknown"))

    return {"subject_id": subject_id, "type": match.get("type")}
