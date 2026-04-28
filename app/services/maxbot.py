import httpx

MAXBOT_API_URL = "https://app.edna.ru/api/v1/out-messages/max-bot"
VALID_MEDIA_TYPES = {"IMAGE", "VIDEO", "AUDIO", "DOCUMENT"}


async def _post(api_key: str, sender: str, max_id: str, content: dict) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            MAXBOT_API_URL,
            headers={"Content-Type": "application/json", "X-API-KEY": api_key},
            json={"sender": sender, "maxId": max_id, "content": content},
            timeout=10,
        )
    response.raise_for_status()
    return response.json()


async def send_message(api_key: str, sender: str, max_id: str, text: str) -> dict:
    return await _post(api_key, sender, max_id, {"type": "TEXT", "text": text})


async def send_media(api_key: str, sender: str, max_id: str, content_type: str, url: str, name: str, caption: str | None = None) -> dict:
    if content_type not in VALID_MEDIA_TYPES:
        raise ValueError(f"Invalid content_type '{content_type}'. Must be one of {VALID_MEDIA_TYPES}")
    if not url or not name:
        raise ValueError("url and name must be non-empty strings")
    content = {"type": content_type, "url": url, "name": name}
    if caption is not None:
        content["caption"] = caption
    return await _post(api_key, sender, max_id, content)
