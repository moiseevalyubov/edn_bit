# MAX Bot Send Message API — LLM Manifest

## Purpose

Этот метод позволяет серверу отправить исходящее сообщение пользователю в мессенджере MAX через платформу edna.

Используется, когда оператор в Bitrix24 Открытых линиях отправляет ответ — сервер вызывает этот API, чтобы доставить сообщение пользователю.

---

## Endpoint

```
POST https://app.edna.ru/api/v1/out-messages/max-bot
```

---

## Authentication

| Header | Value |
|--------|-------|
| `Content-Type` | `application/json` |
| `X-API-KEY` | API-ключ канала |

---

## Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `sender` | string | Yes | Уникальный идентификатор канала (subject из входящего вебхука) |
| `maxId` | string | Yes | Идентификатор получателя — `subscriber.identifier` из входящего вебхука |
| `content` | object | Yes | Контент сообщения — см. типы ниже |

---

## Content Types

### TEXT — текстовое сообщение

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `type` | string | Yes | `"TEXT"` |
| `text` | string | Yes | Макс. 4000 символов |

```json
{
  "sender": "my_channel",
  "maxId": "79000000000",
  "content": {
    "type": "TEXT",
    "text": "Добрый день! Чем могу помочь?"
  }
}
```

---

### IMAGE — изображение

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `type` | string | Yes | `"IMAGE"` |
| `url` | string | Yes | URL файла, макс. 4096 символов |
| `name` | string | Yes | Имя файла, макс. 4096 символов |
| `caption` | string | No | Подпись, макс. 4096 символов |
| `text` | string | No | Дополнительный текст |

```json
{
  "sender": "my_channel",
  "maxId": "79000000000",
  "content": {
    "type": "IMAGE",
    "url": "https://example.com/photo.jpg",
    "name": "photo.jpg",
    "caption": "Схема подключения"
  }
}
```

---

### VIDEO — видео

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `type` | string | Yes | `"VIDEO"` |
| `url` | string | Yes | URL файла, макс. 4096 символов |
| `name` | string | Yes | Имя файла, макс. 4096 символов |
| `caption` | string | No | Подпись, макс. 4096 символов |
| `text` | string | No | Дополнительный текст |

```json
{
  "sender": "my_channel",
  "maxId": "79000000000",
  "content": {
    "type": "VIDEO",
    "url": "https://example.com/video.mp4",
    "name": "instruction.mp4"
  }
}
```

---

### AUDIO — аудиофайл

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `type` | string | Yes | `"AUDIO"` |
| `url` | string | Yes | URL файла, макс. 4096 символов |
| `name` | string | Yes | Имя файла, макс. 4096 символов |
| `caption` | string | No | Подпись, макс. 4096 символов |
| `text` | string | No | Дополнительный текст |

```json
{
  "sender": "my_channel",
  "maxId": "79000000000",
  "content": {
    "type": "AUDIO",
    "url": "https://example.com/audio.mp3",
    "name": "message.mp3"
  }
}
```

---

### VOICE — голосовое сообщение

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `type` | string | Yes | `"VOICE"` |
| `url` | string | Yes | URL файла, макс. 4096 символов |
| `name` | string | Yes | Имя файла, макс. 4096 символов |
| `caption` | string | No | Подпись, макс. 4096 символов |
| `text` | string | No | Дополнительный текст |

```json
{
  "sender": "my_channel",
  "maxId": "79000000000",
  "content": {
    "type": "VOICE",
    "url": "https://example.com/voice.ogg",
    "name": "voice.ogg"
  }
}
```

---

### DOCUMENT — документ / файл

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `type` | string | Yes | `"DOCUMENT"` |
| `url` | string | Yes | URL файла, макс. 4096 символов |
| `name` | string | Yes | Имя файла, макс. 4096 символов |
| `caption` | string | No | Подпись, макс. 4096 символов |
| `text` | string | No | Дополнительный текст |

```json
{
  "sender": "my_channel",
  "maxId": "79000000000",
  "content": {
    "type": "DOCUMENT",
    "url": "https://example.com/contract.pdf",
    "name": "contract.pdf",
    "caption": "Договор на подписание"
  }
}
```

---

### LOCATION — геолокация

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | Yes | `"LOCATION"` |
| `latitude` | number | Yes | Широта |
| `longitude` | number | Yes | Долгота |
| `text` | string | No | Описание места |

```json
{
  "sender": "my_channel",
  "maxId": "79000000000",
  "content": {
    "type": "LOCATION",
    "latitude": 55.7558,
    "longitude": 37.6176,
    "text": "Наш офис"
  }
}
```

---

### BUTTON — одна кнопка-ссылка

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | Yes | `"BUTTON"` |
| `text` | string | Yes | Текст сообщения |
| `button` | object | Yes | Кнопка |
| `button.caption` | string | Yes | Подпись кнопки, макс. 30 символов |
| `button.action` | string | Yes | URL по нажатию (HTTPS), макс. 1024 символов |

```json
{
  "sender": "my_channel",
  "maxId": "79000000000",
  "content": {
    "type": "BUTTON",
    "text": "Перейдите для оплаты:",
    "button": {
      "caption": "Оплатить",
      "action": "https://pay.example.com/invoice/123"
    }
  }
}
```

---

### KEYBOARD — клавиатура с кнопками

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | Yes | `"KEYBOARD"` |
| `text` | string | Yes | Текст сообщения |
| `keyboard` | object | Yes | Объект клавиатуры |
| `keyboard.rows` | array | Yes | Строки клавиатуры |
| `keyboard.rows[].buttons` | array | Yes | Кнопки в строке |
| `keyboard.rows[].buttons[].type` | string | Yes | Тип кнопки (см. ниже) |
| `keyboard.rows[].buttons[].text` | string | Yes | Текст кнопки, макс. 30 символов |
| `keyboard.rows[].buttons[].url` | string | No | URL (для типа `LINK`) |
| `keyboard.rows[].buttons[].payload` | string | No | Payload (для типов `MESSAGE`, `CALLBACK`) |

**Типы кнопок клавиатуры:**

| Type | Description |
|------|-------------|
| `LINK` | Открывает URL |
| `MESSAGE` | Отправляет текст как сообщение от пользователя |
| `CALLBACK` | Отправляет callback с payload |
| `LOCATION_REQUEST` | Запрашивает геолокацию пользователя |
| `CONTACT_REQUEST` | Запрашивает контакт пользователя |

```json
{
  "sender": "my_channel",
  "maxId": "79000000000",
  "content": {
    "type": "KEYBOARD",
    "text": "Выберите удобное время:",
    "keyboard": {
      "rows": [
        {
          "buttons": [
            { "type": "MESSAGE", "text": "Утром (9–12)", "payload": "morning" },
            { "type": "MESSAGE", "text": "Днём (12–17)", "payload": "afternoon" }
          ]
        },
        {
          "buttons": [
            { "type": "MESSAGE", "text": "Вечером (17–21)", "payload": "evening" }
          ]
        }
      ]
    }
  }
}
```

---

## Response

**HTTP 200 — успех**

| Field | Type | Description |
|-------|------|-------------|
| `outMessageId` | long | Внутренний ID сообщения на платформе edna |
| `maxId` | string | Идентификатор получателя (эхо из запроса) |

```json
{
  "outMessageId": 987654321,
  "maxId": "79000000000"
}
```

**Ошибки**

| Code | Description |
|------|-------------|
| 400 | Ошибка запроса или валидации |
| 401 | Неверный или отсутствующий API-ключ |

При ошибке необходимо вызвать `imconnector.send.status.undelivered` в Bitrix24, чтобы оператор увидел, что сообщение не доставлено.

---

## Relevant Types for Bitrix24 Open Lines

Из Открытых линий оператор может отправить:

| Content Type | Когда используется |
|---|---|
| `TEXT` | Текстовый ответ оператора |
| `IMAGE` | Оператор прикрепил изображение |
| `DOCUMENT` | Оператор прикрепил файл (PDF, docx и т.д.) |
| `VIDEO` | Оператор прикрепил видео |
| `AUDIO` | Оператор прикрепил аудиофайл |

Типы `VOICE`, `LOCATION`, `BUTTON`, `KEYBOARD` — инициируются программно, не через интерфейс оператора.

---

## Rules for AI Agents

- Всегда передавать `sender` и `maxId` из контекста канала.
- `content.type` должен быть в верхнем регистре: `"TEXT"`, `"IMAGE"`, и т.д.
- Для медиа-типов (IMAGE, VIDEO, AUDIO, VOICE, DOCUMENT) — `url` и `name` обязательны.
- `url` должен быть публично доступен на момент отправки.
- При ошибке HTTP 4xx/5xx — сообщать Bitrix24 через `imconnector.send.status.undelivered`.
- Длина `text` для TEXT не должна превышать 4000 символов — при необходимости разбивать на несколько сообщений.
