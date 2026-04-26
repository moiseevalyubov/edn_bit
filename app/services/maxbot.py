import httpx

MAXBOT_API_URL = "https://app.edna.ru/api/v1/out-messages/max-bot"


def send_message(api_key: str, sender: str, max_id: str, text: str) -> dict:
    response = httpx.post(
        MAXBOT_API_URL,
        headers={"Content-Type": "application/json", "X-API-KEY": api_key},
        json={"sender": sender, "maxId": max_id, "content": {"type": "TEXT", "text": text}},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()
