# MAX Bot — Bitrix24 Connector

Приложение для маркетплейса Битрикс24: подключает канал MAX Bot к Открытым линиям. Операторы принимают и отправляют сообщения (текст, изображения, файлы, видео, аудио) прямо из Битрикс24, не переключаясь между интерфейсами.

---

## Навигация по репозиторию

В репозитории несколько документов для разных задач — вот где что найти:

| Документ | Для кого | Что внутри |
|---|---|---|
| **README.md** (этот файл) | Разработчики | Архитектура, как запустить, ключевые технические решения |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Команда деплоя | Компоненты, требования к серверу, пошаговая инструкция по развертыванию |
| [docs/PRD.md](docs/PRD.md) | Все | Что делает продукт, кому нужен, что входит в MVP и что нет |
| [docs/MVP.md](docs/MVP.md) | Продукт, менеджмент | Какую гипотезу проверяет MVP, метрики успеха |
| [docs/STYLE_GUIDE.md](docs/STYLE_GUIDE.md) | Разработчики | Стиль edna для веб-форм: палитра, шрифт, компоненты, как снять новый стиль |
| [review.md](review.md) | Разработчики | Технический долг, открытые задачи и известные проблемы |

**Если вы разработчик, который подключается к проекту:**
1. Сначала прочитайте [docs/PRD.md](docs/PRD.md) — понять, что делает продукт и зачем
2. Затем этот файл — архитектура и устройство кода
3. [review.md](review.md) — перед тем как что-то менять: там открытые задачи и известные ограничения

---

## Архитектура

```
Клиент MAX Bot
     │  входящие сообщения (webhook POST /incoming/{webhook_token})
     ▼
edna (сервер MAX Bot API)
     │  исходящие через API POST /api/v1/out-messages/max-bot
     ▲
     │
 Это приложение (FastAPI)
     │
     ├── GET/POST /handler                ← события Bitrix24 (OnImConnectorMessageAdd и др.)
     ├── POST /incoming/{webhook_token}   ← входящие сообщения от edna/MAX Bot
     ├── GET  /file/{uuid}.ext            ← отдаёт предзакешированные файлы для edna
     ├── GET  /settings                   ← UI настроек (Jinja2 HTML)
     └── /api/*                           ← REST API для UI настроек
     │
     ├── База данных (PostgreSQL)
     │        Portal → Channel → Message / SeenEvent
     │        MessageDeliveryTask (очередь доставки)
     └── Delivery Worker (asyncio, фоновый)
              читает MessageDeliveryTask → доставляет → retry при ошибках
```

### Поток входящего сообщения (клиент → оператор)
1. edna присылает `POST /incoming/{webhook_token}` с вебхуком MAX Bot
2. Приложение находит канал по `webhook_token` из URL, проверяет дедупликацию по `max_message_id`
3. Создаёт задачу в `message_delivery_tasks` (status=pending), сразу возвращает `200 OK`
4. Delivery Worker подхватывает задачу и вызывает `imconnector.send.messages` в Bitrix24:
   - **TEXT** — текст передаётся как есть
   - **IMAGE, DOCUMENT, AUDIO, VIDEO, VOICE** — URL файла из edna передаётся напрямую; S3-ссылка живёт ~1 год
   - **LOCATION** — координаты конвертируются в текст со ссылкой на Яндекс.Карты
5. При ошибке — автоматический retry (до 6 раз, расписание 1с → 30м)

### Поток исходящего сообщения (оператор → клиент)
1. Bitrix24 присылает `POST /handler` с событием `OnImConnectorMessageAdd`
2. Тело — **PHP-style URL-encoded форма** (`data[MESSAGES][0][chat][id]=...`), парсится в `_parse_php_form()`
3. Если есть файл — приложение **немедленно скачивает его с Bitrix24** пока SIGN-подпись ещё действительна, кладёт в `file_cache` под UUID-ключом
4. Создаёт задачу в `message_delivery_tasks` (status=pending), сразу возвращает `200 OK` (Bitrix24 повторяет при не-200)
5. Delivery Worker подхватывает задачу, вызывает edna API; edna забирает файл с `/file/{uuid}.ext` — стабильный URL, без зависимости от Bitrix
6. При ошибке — автоматический retry (до 6 раз)

---

## Ключевые архитектурные решения

### Файлы: предзагрузка, а не проксирование на лету
**Проблема:** edna требует URL с расширением файла (`.jpg`, `.png`). Прямые ссылки Bitrix24 выглядят как `pub/im.file.php?FILE_ID=...` — без расширения. Кроме того, edna скачивает файл асинхронно (уже после того, как вернула 200 OK), и к тому моменту подпись (SIGN) в Bitrix-URL могла бы истечь.

**Решение:** при получении вебхука от Bitrix немедленно скачиваем файл, кладём в `file_cache` с UUID-ключом и 10-минутным TTL. edna получает URL вида `/file/<uuid>.jpg` — стабильный, без зависимости от Bitrix.

### PHP-style вебхуки Bitrix24
Bitrix24 доставляет события в формате `application/x-www-form-urlencoded` с PHP-стилем вложенных ключей:
```
data[MESSAGES][0][chat][id]=abc&data[MESSAGES][0][message][text]=hello
```
Стандартный `parse_qs` даёт плоский словарь. Функция `_parse_php_form()` в `handler.py` разворачивает его во вложенный dict.

### Маршрутизация канала
Один портал Bitrix может иметь несколько каналов MAX Bot. При исходящем сообщении нужно понять, через какой отправлять:
1. Ищем активный канал, через который ранее приходили сообщения от этого `chat_id` (по таблице `messages`)
2. Если не найден — берём первый активный канал портала (fallback)

### Токены Bitrix24
OAuth-токены (`access_token`, `refresh_token`) обновляются при каждом событии — Bitrix присылает актуальный `auth` в теле каждого вебхука. `update_portal_tokens()` в `handler.py` сохраняет их при каждом вызове — ручного обновления токенов не требуется.

---

## Структура проекта

```
app/
  routers/
    handler.py       # события Bitrix24 (OnImConnectorMessageAdd и др.)
    incoming.py      # входящие вебхуки от edna/MAX Bot
    api.py           # REST API для UI настроек (/api/channels, /api/open-lines и др.)
    files.py         # отдача предзагруженных файлов для edna
    install.py       # установка приложения в Bitrix24
    settings_page.py # страница настроек (HTML через Jinja2)
  services/
    bitrix.py          # вызовы Bitrix24 REST API
    maxbot.py          # вызовы edna MAX Bot API
    file_cache.py      # кеш файлов в памяти (UUID → bytes, TTL 10 мин)
    delivery_worker.py # фоновый worker: читает очередь, доставляет с retry
  models.py          # SQLAlchemy модели: Portal, Channel, Message, MessageDeliveryTask, SeenEvent
  schemas.py         # Pydantic схемы (валидация запросов и ответов)
  config.py          # настройки из переменных окружения
  database.py        # подключение к БД

Procfile             # команда запуска для Render/Heroku
requirements.txt     # зависимости Python
.env.example         # пример переменных окружения

docs/
  PRD.md             # продуктовые требования
  MVP.md             # гипотеза MVP и метрики успеха
  STYLE_GUIDE.md     # стиль edna для веб-форм (палитра, шрифт, компоненты)
  db_schema.md       # схема базы данных
  MAX Bot Incoming - LLM Manifest.md    # формат вебхука от edna
  MAX Bot send message - LLM Manifest.md # формат API отправки

review.md            # технический долг и открытые задачи
```

---

## Стек

| Библиотека | Версия | Зачем |
|---|---|---|
| FastAPI | 0.115 | Web-фреймворк, async-обработчики |
| Uvicorn | 0.31 | ASGI-сервер |
| SQLAlchemy | 2.0 | ORM, работа с PostgreSQL |
| pydantic-settings | 2.5 | Переменные окружения через `.env` |
| httpx | 0.27 | Async HTTP-клиент (вызовы edna и Bitrix24 API) |
| Jinja2 | 3.1 | HTML-шаблоны страницы настроек |
| python-multipart | 0.0.12 | Парсинг form-data от Bitrix24 |
| psycopg2-binary | — | Драйвер PostgreSQL |
| cryptography | — | Fernet-шифрование секретов в БД |

---

## Переменные окружения

| Переменная | Обязательная | Описание |
|---|---|---|
| `APP_BASE_URL` | **Да** | Публичный HTTPS-URL сервера, напр. `https://myapp.onrender.com`. Используется для вебхуков и файлового кеша. Без неё события Bitrix работать не будут. |
| `DATABASE_URL` | **Да** | URL базы данных. Только PostgreSQL: `postgresql://user:pass@host/db`. SQLite не поддерживается — не выдерживает параллельных запросов от нескольких workers. |
| `SECRET_KEY` | **Да** | Произвольная строка ≥32 символа. Шифрует токены и API-ключи в БД (Fernet). **Нельзя менять после первого запуска** — все зашифрованные данные станут нечитаемыми. Сгенерировать: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `BITRIX_CLIENT_ID` | **Да** | Client ID приложения из маркетплейса Bitrix24. |
| `BITRIX_CLIENT_SECRET` | **Да** | Client Secret приложения из маркетплейса Bitrix24. |

Пример: [.env.example](.env.example)

---

## Локальный запуск

```bash
# 1. Установить зависимости
pip install -r requirements.txt

# 2. Создать .env (скопировать из примера и заполнить)
cp .env.example .env

# 3. Запустить
uvicorn app.main:app --reload --port 8000
```

Для тестирования вебхуков локально нужен [ngrok](https://ngrok.com):
```bash
ngrok http 8000
# Вставить полученный https-URL в APP_BASE_URL в .env
```

---

## Деплой (Render)

Приложение задеплоено на [Render](https://render.com). Команда запуска берётся из `Procfile`:
```
web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Переменные окружения задаются в дашборде Render → Environment. При push в `master` деплой запускается автоматически.

**Важно:** на бесплатном тарифе Render сервер засыпает после 15 минут простоя. Для стабильной работы рекомендуется платный тариф.

---

## Известные ограничения

Подробнее — в [review.md](review.md).

- **Кеш файлов в памяти**: при рестарте сервера файлы теряются. Если в очереди остались незавершённые задачи с `file_key` — worker получит `file_cache_expired` и пометит их как `failed` (не будет бесконечно ретраить). На практике edna скачивает быстро и это не проблема.
- **Нет редактирования API-ключа канала**: только отключить и создать новый.
