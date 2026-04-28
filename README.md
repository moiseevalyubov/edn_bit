# MAX Bot — Bitrix24 Connector

Приложение для маркетплейса Битрикс24: подключает канал MAX Bot к Открытым линиям. Операторы принимают и отправляют сообщения (текст, изображения, файлы, видео, аудио) прямо из Битрикс24.

**Подробные продуктовые требования:** [PRD.md](PRD.md)  
**Технический долг и открытые задачи:** [review.md](review.md)

---

## Архитектура

```
Клиент MAX Bot
     │  входящие сообщения (webhook POST /incoming)
     ▼
edna (сервер MAX Bot API)
     │  исходящие через API POST /api/v1/out-messages/max-bot
     ▲
     │
 Это приложение (FastAPI)
     │
     ├── GET/POST /handler   ← события Bitrix24 (OnImConnectorMessageAdd и др.)
     ├── POST /incoming      ← входящие от edna
     ├── GET  /file/{key}    ← отдаёт предзакешированные файлы для edna
     ├── GET  /settings      ← UI настроек (Jinja2)
     └── /api/*              ← REST API для UI
     │
     └── База данных (SQLite / PostgreSQL)
              Portal → Channel → Message
```

### Поток входящего сообщения (клиент → оператор)
1. edna присылает `POST /incoming` с вебхуком MAX Bot
2. Приложение находит канал по `subject` (sender), получает `subscriber.identifier` (MAX ID)
3. Вызывает `imconnector.send.messages` в Bitrix24 — сообщение появляется в Открытых линиях

### Поток исходящего сообщения (оператор → клиент)
1. Bitrix24 присылает `POST /handler` с событием `OnImConnectorMessageAdd`
2. Тело запроса — **PHP-style URL-encoded форма** (`data[MESSAGES][0][chat][id]=...`), парсится в `_parse_php_form()`
3. Если сообщение содержит файл — приложение **немедленно скачивает его с Bitrix24** и кладёт в память
4. Отправляет запрос в edna API (`POST /api/v1/out-messages/max-bot`)
5. Для файлов edna забирает его с эндпоинта `/file/{key}` — файл уже в памяти, никаких зависимостей от Bitrix

---

## Ключевые архитектурные решения

### Файлы: предзагрузка, а не проксирование на лету
**Проблема:** edna требует URL с расширением файла (`.jpg`, `.png`). Прямые ссылки Bitrix24 выглядят как `pub/im.file.php?FILE_ID=...` — без расширения. Кроме того, edna скачивает файл асинхронно (уже после того, как вернула 200 OK нам), и к тому моменту подпись (SIGN) в URL Bitrix могла бы истечь.

**Решение:** при получении вебхука от Bitrix немедленно скачиваем файл, кладём в `file_cache` с UUID-ключом и 10-минутным TTL. edna получает URL вида `/file/<uuid>.jpg` — он всегда доступен, пока файл в кеше.

### PHP-style вебхуки Bitrix24
Bitrix24 доставляет события в формате `application/x-www-form-urlencoded` с PHP-стилем вложенных ключей:
```
data[MESSAGES][0][chat][id]=abc&data[MESSAGES][0][message][text]=hello
```
Стандартный `parse_qs` даёт плоский словарь. Функция `_parse_php_form()` в `handler.py` разворачивает его в нормальный вложенный dict.

### Маршрутизация канала
Один портал Bitrix может иметь несколько каналов MAX Bot. При исходящем сообщении нужно понять, через какой канал его отправить. Логика:
1. Найти активный канал, через который ранее приходили сообщения от этого `chat_id` (по таблице `messages`)
2. Если не найден — использовать первый активный канал портала (fallback)

### Токены Bitrix24
OAuth-токены (`access_token`, `refresh_token`) автоматически обновляются при каждом событии — Bitrix присылает актуальный `auth` в теле каждого вебхука. `update_portal_tokens()` в `handler.py` сохраняет их при каждом вызове.

---

## Структура проекта

```
app/
  routers/
    handler.py      # события Bitrix24 (OnImConnectorMessageAdd и др.)
    incoming.py     # входящие вебхуки от edna
    api.py          # REST API для UI настроек
    files.py        # отдача предзагруженных файлов
    install.py      # установка приложения в Bitrix24
    settings_page.py# страница настроек (HTML)
  services/
    bitrix.py       # вызовы Bitrix24 REST API
    maxbot.py       # вызовы edna MAX Bot API
    file_cache.py   # кеш файлов в памяти (UUID → bytes, TTL 10 мин)
  models.py         # SQLAlchemy модели: Portal, Channel, Message
  schemas.py        # Pydantic схемы
  config.py         # настройки из переменных окружения
  database.py       # подключение к БД

PRD.md              # продуктовые требования
review.md           # технический долг и открытые задачи
```

---

## Переменные окружения

| Переменная | Обязательная | Описание |
|---|---|---|
| `APP_BASE_URL` | **Да** | Публичный URL сервера, напр. `https://myapp.onrender.com`. Используется для формирования URL вебхуков и файлового прокси. Если не задана — события Bitrix работать не будут. |
| `DATABASE_URL` | Нет | URL базы данных. По умолчанию SQLite (`sqlite:///./app.db`). Для продакшна с несколькими workers обязательно задать PostgreSQL: `postgresql://user:pass@host/db`. |
| `BITRIX_CLIENT_ID` | **Да** | Client ID приложения из маркетплейса Bitrix24. |
| `BITRIX_CLIENT_SECRET` | **Да** | Client Secret приложения из маркетплейса Bitrix24. |

Пример файла: [.env.example](.env.example)

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

Для локального тестирования вебхуков использовать [ngrok](https://ngrok.com):
```bash
ngrok http 8000
# Получить https URL и прописать его в APP_BASE_URL
```

---

## Деплой (Render)

Приложение задеплоено на [Render](https://render.com). Команда запуска из `Procfile`:
```
web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Переменные окружения задаются в дашборде Render → Environment.

**Важно:** на бесплатном тарифе Render сервер засыпает после 15 минут простоя. Для стабильной работы рекомендуется платный тариф.

---

## Известные ограничения (см. подробнее в review.md)

- **Кеш файлов — только в памяти**: при рестарте сервера файлы в кеше теряются. Если edna попытается забрать файл после рестарта — получит 404. На практике edna скачивает файл быстро, поэтому это не проблема в нормальных условиях.
- **SQLite по умолчанию**: не поддерживает несколько workers. Для продакшна нужен PostgreSQL.
- **Нет возможности изменить API-ключ канала**: только отключить и создать новый.
- **Отправка файлов из Bitrix в MAX**: реализована, работает, но требует уточнения поведения у поддержки edna (см. review.md §6).
