# MAX Bot Incoming Message API — LLM Manifest

## Purpose

Этот метод позволяет серверу получить входящее сообщение от пользователя сообщение, которое он написал в своем в мессенджере Max

---

## Endpoint (Webhook)

Сервер получит POST-запрос с контентом входящего сообщения.
Адрес должен начинаться с HTTPS — протокол HTTPS, порт 443.

### Верификация URL при подключении

При сохранении Webhook URL в интерфейсе edna платформа **проверяет доступность адреса** перед сохранением.
Проверка выполняется двумя запросами:

1. `GET {webhook_url}` — ожидает ответ `200 OK`
2. `HEAD {webhook_url}` — ожидает ответ `200 OK`

Если хотя бы один из запросов возвращает не 200, edna отображает ошибку "Запрашиваемый ресурс не существует" и не сохраняет URL.

**Требование к реализации сервера:** эндпоинт `/incoming` должен принимать методы `GET`, `HEAD` и `POST`.

---

## Webhook Payload (Incoming Message)

### Top‑Level Fields

| Field                                                            | Type        | Description                                      |
| ---------------------------------------------------------------- | ----------- | ------------------------------------------------ |
| `id`                                                             | long        | Идентификатор запроса |
| `subject`                                                        | string      | Содержит уникальный идентификатор канала - sender |
| `subjectId`                                                      | long        | Идентификатор канала-sender'а.    |
| `subscriber`                                                     | object      | Информация об отправителе сообщения  |
| `userInfo`                                                       | object      | Дополнительная информация об отправителе сообщения (names, avatar)             |
| `messageContent`                                                 | object      | Контент входящего сообщения              |
| `receivedAt`                                                     | string      | Timestamp when message was received              |
| `replyOutMessageId`                                              | long/null   | If user quoted an outgoing message               |
| `replyOutMessageExternalRequestId`                               | string/null | External ID of quoted outgoing message           |
| `replyInMessageId`                                               | long/null   | Internal ID of quoted incoming message           |
|  |             |                                                  |

---

## Subscriber Object

| Field        | Type   | Description                                     |                           |
| ------------ | ------ | ----------------------------------------------- | ------------------------- |
| `id`         | long   | Идентификатор отправителя сообщения  - внтуренний на сервере   |                           |
| `identifier` | string | Идентификатор отправителя |     |

---

## User Info Object

| Field       | Type        | Description              |                           |
| ----------- | ----------- | ------------------------ | ------------------------- |
| `userName`  | string      | Username or display name |                           |
| `firstName` | string/null | First name if available  |                           |
| `lastName`  | string/null | Last name if available   |                           |
| `avatarUrl` | string/null | URL of user avatar       |   |

---

## MessageContent Object

| Field        | Type        | Description                                      |
|--------------|-------------|--------------------------------------------------|
| `type`       | string      | Message type: `TEXT`, `IMAGE` (others possible)  |
| `text`       | string/null | Текст сообщения (для TEXT)                       |
| `attachment` | object/null | Файл или медиа (для IMAGE и других медиатипов)   |
| `caption`    | string/null | Подпись к медиа (может быть null даже для IMAGE) |
| `location`   | object/null | Геолокация (для LOCATION)                        |
| `referral`   | object/null | Реферальные данные                               |
| `payload`    | string/null | Payload кнопки/команды                           |
| `story`      | object/null | Story reply                                      |
| `items`      | array/null  | Список элементов                                 |
| `contact`    | object/null | Контакт пользователя                             |
| `product`    | object/null | Продукт                                          |
| `catalog`    | object/null | Каталог                                          |
| `order`      | object/null | Заказ                                            |
| `callPermissionReply` | object/null | Ответ на запрос звонка                  |

## Attachment Object (для type=IMAGE и других медиатипов)

| Field  | Type        | Description                                              |
|--------|-------------|----------------------------------------------------------|
| `url`  | string      | Подписанный S3 URL файла. Срок действия ~1 год от отправки. Публично доступен. |
| `name` | string/null | Имя файла. **На практике всегда null** — извлекать из пути URL. |
| `size` | integer/null | Размер файла в байтах. На практике null.                |

**Важно об URL:** это подписанный AWS S3 URL вида:
```
https://files.mfms.ru/imfiles4s/{channel}/{uuid}.{ext}?AWSAccessKeyId=...&Expires=...&Signature=...
```
Расширение файла содержится в пути (`.webp`, `.jpg` и т.д.). Для получения имени файла: `urlparse(url).path.split("/")[-1]`.

Срок действия подписи (~1 год) достаточен для того, чтобы передать URL напрямую в Bitrix24 — Bitrix скачает файл сам. Скачивать файл на своём сервере не нужно (см. AD-7 в DECISIONS.md).

---

## Sample Webhook Body — TEXT Message

```json
{
    "id": 101,
    "subject": "test_subject_WA",
    "subjectId": 345,
    "subscriber": {"id": 202, "identifier": "79000000000"},
    "userInfo": {"userName": "alex", "firstName": null, "lastName": null, "avatarUrl": null},
    "messageContent": {
        "type": "TEXT",
        "attachment": null,
        "location": null,
        "caption": null,
        "text": "Спасибо за помощь",
        "payload": null,
        "story": null,
        "items": null,
        "contact": null,
        "product": null,
        "catalog": null,
        "order": null,
        "callPermissionReply": null
    },
    "receivedAt": "2022-04-29T15:30:08Z",
    "replyOutMessageId": 5043874,
    "replyOutMessageExternalRequestId": "2c2dd5f1-5ad8-449d-9c38-b6bdf288f1e5",
    "replyInMessageId": null,
    "lastMessage": null
}
```

## Sample Webhook Body — IMAGE Message

```json
{
    "id": 14558923001,
    "subject": "ednapulse_prodbot",
    "subjectId": 13556,
    "subscriber": {"id": 176469628, "identifier": "194089586"},
    "userInfo": {"userName": null, "firstName": "Lyubov", "lastName": "", "avatarUrl": "https://files.mfms.ru/..."},
    "messageContent": {
        "type": "IMAGE",
        "attachment": {
            "url": "https://files.mfms.ru/imfiles4s/ednapulse_prodbot/Q8PJu5KqTCCbMvPjm9KG9g.webp?AWSAccessKeyId=imfiles4s&Expires=1778001117&Signature=...",
            "name": null,
            "size": null
        },
        "location": null,
        "referral": null,
        "caption": null,
        "text": null,
        "payload": null,
        "story": null,
        "items": null,
        "contact": null,
        "product": null,
        "catalog": null,
        "order": null,
        "callPermissionReply": null
    },
    "receivedAt": "2026-04-28T17:11:57Z",
    "replyOutMessageId": null,
    "replyOutMessageExternalRequestId": null,
    "replyInMessageId": null,
    "lastMessage": null
}
```

---

## How to Use in Integration

Когда сервер получает этот вебхук:

1. Parse JSON body.
2. Determine `messageContent.type`.
3. Для `TEXT`: извлечь `text`.
4. Для `IMAGE`: извлечь `attachment.url`. Имя файла — из пути URL (`urlparse(url).path.split("/")[-1]`), т.к. `attachment.name` всегда null. Передать URL напрямую в `imconnector.send.messages` в параметр `files`.
5. Use `subscriber.identifier` as the unique user identifier for routing in Bitrix24.

---

## Supported Types

| Type     | Статус реализации |
|----------|-------------------|
| TEXT     | ✅ Реализован     |
| IMAGE    | ✅ Реализован     |
| VIDEO    | ⬜ Не реализован  |
| AUDIO    | ⬜ Не реализован  |
| VOICE    | ⬜ Не реализован  |
| DOCUMENT | ⬜ Не реализован  |
| LOCATION | ⬜ Не реализован  |

---

## Rules for AI Agents

* Expect POST webhook — no query parameters.
* This webhook is not an API request you call — it is **delivered to your server**.
* For `TEXT`: handle `messageContent.text`.
* For `IMAGE`: handle `messageContent.attachment.url`. Name is always null — extract from URL path.
* `attachment.url` is a long-lived signed S3 URL — pass directly to Bitrix24, no need to proxy.
* For unsupported types: log and return 200 OK without processing.
