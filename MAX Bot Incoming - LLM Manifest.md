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

| Field                                                                                            | Type        | Description                                                         |
| ------------------------------------------------------------------------------------------------ | ----------- | ------------------------------------------------------------------- |
| `type`                                                                                           | string      | Message type (TEXT) |
| `text`                                                                                           | string/null | Текст сообщения                  |


---

## Sample Webhook Body — TEXT Message

{
    "id": 101,
    "subject": "test_subject_WA",
    "subjectId": 345,
    "subscriber": {
        "id": 202,
        "identifier": "79000000000"
    },
    "userInfo": {
        "userName": "alex",
        "firstName": null,
        "lastName": null,
        "avatarUrl": null
    },
    "messageContent": {
        "type": "TEXT",
        "attachment": null,
        "location": null,
        "caption": null,
        "text": "Спасибо за помощь",
        "payload": null,
        "story": null,
        "items": null
    },
    "receivedAt": "2022-04-29T15:30:08Z",
    "replyOutMessageId": 5043874,
    "replyOutMessageExternalRequestId": "2c2dd5f1-5ad8-449d-9c38-b6bdf288f1e5",
    "replyInMessageId": null
}


---

## How to Use in Integration

Когда сервер получает этот вебхук:

1. Parse JSON body.
2. Determine `messageContent.type`.
3. Extract `text` if present (for TEXT messages).
4. Use `subscriber.identifier` as the unique user identifier for routing in Bitrix24.
5. If the user quoted another message, use `replyOutMessageId` or `replyOutMessageExternalRequestId` for reference.

---

## Rules for AI Agents

* Expect POST webhook — no query parameters.
* This webhook is not an API request you call — it is **delivered to your server**.
* Always handle `messageContent.text` only when `type = TEXT`.
* Other fields (attachments, interactive objects, flows, orders) may appear but are optional.

---

## Notes

* For non‑TEXT types, clients may include structures like items, product, order, or location. Handle only TEXT for MVP. 
