# Схема базы данных — MAX Bot Bitrix24 Connector

## Пять основных таблиц

Основа #22 добавляет ещё три таблицы CRM и два поля существующих таблиц. Она реализована локально, но пока не выложена на стенд; состояние проверок — [CRM_OUTREACH_PROGRESS.md](CRM_OUTREACH_PROGRESS.md).

### `portals` — порталы Bitrix24

Каждая запись — один портал Bitrix24, который установил приложение.

| Поле | Тип | Описание |
|---|---|---|
| `id` | Integer, PK | Внутренний ID |
| `member_id` | String, уникальный | ID портала в Bitrix24 — главный идентификатор |
| `client_endpoint` | String | URL API портала (напр. `https://company.bitrix24.ru/rest/`) |
| `access_token` | String (зашифрован) | OAuth-токен для запросов к Bitrix24 |
| `refresh_token` | String (зашифрован) | Токен для обновления access_token |
| `token_expires_at` | DateTime | Когда истекает access_token |
| `app_token` | String | Токен от Bitrix24 для входящих вебхуков |
| `open_line_id` | String | ID Открытой линии, к которой подключён портал |
| `installed_at` | DateTime | Когда установили приложение |
| `uninstalled_at` | DateTime | Когда удалили (NULL — активен) |
| `payment_required_at` | DateTime | Когда истекла подписка Bitrix24 (NULL — подписка активна) |
| `crm_outreach_enabled` | Boolean, default false | Внутреннее включение CRM-функции по порталу; поле основы #22 |

---

### `channels` — каналы MAX Bot

Один портал может иметь несколько каналов MAX Bot. Каждый канал — это отдельный бот.

| Поле | Тип | Описание |
|---|---|---|
| `id` | Integer, PK | Внутренний ID |
| `portal_id` | Integer, FK → portals | К какому порталу относится |
| `name` | String | Название канала (для отображения в UI) |
| `api_key` | String (зашифрован) | API-ключ канала в edna/MAX Bot |
| `sender` | String | Идентификатор отправителя в MAX Bot (используется для маршрутизации входящих) |
| `webhook_token` | String, уникальный | Токен в URL `/incoming/{webhook_token}` — аутентификация входящих вебхуков |
| `subject_id` | Integer | `subjectId` канала в edna — определяется автоматически при подключении (автонастройка webhook) |
| `channel_type` | String | Тип канала в edna (напр. `MAX`, `WHATSAPP`) — показывается в UI и в тексте уведомления о недоставке |
| `connected_at` | DateTime | Когда подключили |
| `disconnected_at` | DateTime | Когда отключили (NULL — активен) |
| `is_active` | Boolean | Активен ли канал |

---

### `messages` — история сообщений

Нужна для трёх целей: маршрутизация ответов оператора, дедупликация входящих сообщений (защита от повторных вебхуков edna) и восстановление личности клиента для уведомления о недоставке (чтобы оно легло в тот же диалог).

| Поле | Тип | Описание |
|---|---|---|
| `id` | Integer, PK | Внутренний ID |
| `channel_id` | Integer, FK → channels | Через какой канал прошло сообщение |
| `direction` | String | `incoming` (клиент → оператор) или `outgoing` (оператор → клиент) |
| `text` | Text | Текст сообщения |
| `content_type` | String | Тип: TEXT, IMAGE, DOCUMENT, AUDIO, VIDEO, VOICE, LOCATION |
| `max_message_id` | String | ID сообщения в MAX Bot — уникален по `(channel_id, direction, max_message_id)` |
| `bitrix_chat_id` | String | ID чата в Bitrix24 — ключевое поле для маршрутизации ответа |
| `subscriber_identifier` | String | Идентификатор клиента в edna — всегда «голый», без метки канала |
| `subscriber_id_type` | String | Тип идентификатора: `MAX_ID` или `PHONE`. Нужен каналу MAX — он требует тип в блоке `to` при отправке. Пусто у старых записей → считается `MAX_ID` |
| `subscriber_user_id` | String | ID пользователя, как отправлен в Bitrix (`user.id`) — для входящих; нужен, чтобы уведомление о недоставке легло в тот же диалог, а не создало нового «Гостя» |
| `user_name` | String | Имя пользователя, как отправлено в Bitrix — для той же цели |
| `sent_at` | DateTime | Когда отправлено |
| `raw_payload` | Text | Исходный JSON вебхука (для отладки, до 2000 символов) |

---

### `seen_events` — защита от replay-атак

Хранит отпечатки (SHA-256) последних запросов от Bitrix24. Повторный запрос с тем же телом в течение 10 минут отклоняется. При старте приложения записи старше 1 часа удаляются автоматически.

| Поле | Тип | Описание |
|---|---|---|
| `id` | Integer, PK | Внутренний ID |
| `fingerprint` | String(64), уникальный | SHA-256 от тела запроса |
| `seen_at` | DateTime | Когда получен запрос |

---

### `message_delivery_tasks` — очередь доставки сообщений

Каждая запись — одна задача на доставку: либо входящее сообщение (MAX Bot → Bitrix24), либо исходящее (Bitrix24 → MAX Bot). Worker обрабатывает задачи по одной, обновляет статус и retry_count.

| Поле | Тип | Описание |
|---|---|---|
| `id` | Integer, PK | Внутренний ID |
| `channel_id` | Integer, FK → channels | Через какой канал передаётся сообщение |
| `task_type` | String | `send_to_bitrix` (входящее) или `send_to_edna` (исходящее) |
| `direction` | String | `incoming` или `outgoing` — для дедупликации и фильтрации |
| `payload` | Text | JSON со всеми параметрами доставки (текст, file_key, chat_id и т.д.) |
| `status` | String | `pending` → `processing` → `sent` / `failed` / `dead` |
| `retry_count` | Integer | Сколько попыток уже было (0 = ещё не пробовали) |
| `next_attempt_at` | DateTime | Когда worker должен следующий раз попробовать |
| `last_error` | Text | Последнее сообщение об ошибке (до 2000 символов) |
| `max_message_id` | String | ID сообщения в MAX Bot — для дедупликации входящих задач |
| `dedup_key` | String, nullable | Устойчивый ключ CRM-операции; у прежних задач NULL. Уникальный индекс только непустых ключей |
| `created_at` | DateTime | Когда создана задача |
| `updated_at` | DateTime | Когда последний раз обновлялась |

**Статусы:**
- `pending` — ждёт обработки; worker берёт задачи с `next_attempt_at ≤ now()`
- `processing` — worker взял задачу прямо сейчас
- `sent` — доставлено успешно
- `failed` — постоянная ошибка (напр. истёк file cache) — повтора не будет
- `dead` — 6 временных ошибок подряд — повтора не будет

**Уникальный индекс:** `(channel_id, max_message_id) WHERE direction='incoming'` — защита от дубликатов при параллельных вебхуках.

## Таблицы основы CRM (#22)

| Таблица | Данные | Ограничения |
|---|---|---|
| `crm_message_providers` | Портал, канал, CODE, желаемое состояние/имя/HANDLER, версии синхронизации и ошибка | Один provider на Channel; CODE уникален в пределах портала |
| `crm_outgoing_messages` | Портал, канал, CRM-message ID, bindings, автор, текст, телефон, outMessageId, состояние отправки и подтверждённые статусы | UNIQUE(portal_id, provider_code, bitrix_message_id), UNIQUE(channel_id, edna_out_message_id) |
| `crm_message_status_events` | Статус и время edna, requestId, ошибка и fingerprint для CRM-сообщения | UNIQUE(crm_message_id, fingerprint) |

Определения полей находятся в [app/models.py](../app/models.py). Сырые auth, API-ключи и OAuth-токены в этих таблицах не хранятся. История Открытых линий остаётся в Message.

Типы очереди: `send_crm_to_edna`, `update_crm_status`, `sync_crm_provider`. Все три исполнителя подключены локально. Сообщение и его задачи, канал и желаемое состояние provider сохраняются одной транзакцией соответствующей операции; payload содержит внутренний ID/версию, без auth/телефона/текста. До HTTP фиксируются sending и attempt_started_at. Прерванная CRM-отправка с неизвестным результатом получает `uncertain`, автоматического повторного POST нет. Статусы и синхронизация provider имеют независимые повторы и desired/applied_revision; READ сохраняется в БД и отображается как delivered через API CRM. При выключении новые отправки не исполняются, queued отменяются локальной сверкой, планируется удаление provider; статусы уже переданных сообщений продолжают обрабатываться. Фактическая возможность обновить статус после удаления sender ещё требует проверки Битрикс24. Состояние реализации и ограничения: [CRM_OUTREACH_PROGRESS.md](CRM_OUTREACH_PROGRESS.md).

Добавочная миграция [crm_outreach_v1.py](../app/migrations/crm_outreach_v1.py) вызывается до worker. На PostgreSQL блокировка охватывает проверку схемы и DDL одной транзакцией. Старые строки, шифротекст и идентификаторы не переписываются.

---

## Связи между таблицами

```
Portal  ──(один-ко-многим)──  Channel  ──(один-ко-многим)──  Message
                                                 │
                                                 └──(один-ко-многим)──  MessageDeliveryTask

SeenEvent — независимая таблица, не связана с Portal/Channel
```

---

## Шифрование секретов

Поля `access_token`, `refresh_token` (таблица `portals`) и `api_key` (таблица `channels`) хранятся в зашифрованном виде через Fernet (AES-128-CBC + HMAC). Шифрование прозрачное — происходит автоматически через SQLAlchemy TypeDecorator при записи и чтении.

Ключ шифрования деривируется из переменной окружения `SECRET_KEY`. **Нельзя менять `SECRET_KEY` после первого запуска** — все зашифрованные данные станут нечитаемыми.
