# Инструкция по развертыванию

Это руководство для команды разработки. Здесь описано, из чего состоит приложение и как его запустить на сервере.

---

## Компоненты приложения

Приложение состоит из двух компонентов, которые нужно развернуть:

| Компонент | Что это | Где запускать |
|---|---|---|
| **Web-приложение** | Python/FastAPI, принимает вебхуки, хранит данные, отображает настройки | VPS или PaaS (Render, Railway) |
| **PostgreSQL** | База данных: порталы, каналы, история сообщений | Тот же VPS или отдельная БД-сервис |

Внешние сервисы, которые НЕ нужно деплоить (они уже работают):
- **Bitrix24** — корпоративный портал клиента. Отправляет вебхуки в наше приложение.
- **edna / MAX Bot** — платформа мессенджера. Получает и отдаёт сообщения через наш webhook URL.

---

## Архитектура

```
Клиент в MAX Bot
      │
      ▼
  edna/MAX Bot API
      │  вебхук POST /incoming/{webhook_token}
      ▼
┌─────────────────────────────────────┐
│        Наше приложение (FastAPI)     │
│                                     │
│  POST /incoming/{webhook_token}  ◄──── вебхуки от edna
│  POST /handler                   ◄──── события от Bitrix24
│  GET  /file/{uuid}.ext           ◄──── edna забирает файлы
│  GET  /settings                  ◄──── UI настроек (внутри Bitrix)
│  /api/*                          ◄──── REST API для UI
│  GET  /health                    ◄──── проверка работоспособности
└──────────────┬──────────────────────┘
               │
               ▼
          PostgreSQL
     portals / channels / messages / seen_events
```

### Поток: клиент → оператор (входящее)

1. edna отправляет `POST /incoming/{webhook_token}` с сообщением из MAX Bot
2. Приложение находит канал по `webhook_token` в URL (защита от поддельных запросов)
3. Если тип — текст: отправляет в Bitrix24 через `imconnector.send.messages`
4. Если тип — файл: передаёт URL файла прямо в Bitrix24 (edna хранит файлы ~1 год)
5. Если тип — геолокация: конвертирует в текст со ссылкой на Яндекс.Карты

### Поток: оператор → клиент (исходящее)

1. Bitrix24 отправляет `POST /handler` с событием `OnImConnectorMessageAdd`
2. Приложение парсит PHP-style форму и находит нужный канал
3. Если есть файл — **немедленно скачивает** его с Bitrix24 в память (10 мин TTL)
4. Отправляет запрос в edna API
5. edna забирает файл с `/file/{uuid}.ext` — он уже в памяти

### Что хранится в базе данных

- **portals** — Bitrix24-порталы (токены OAuth зашифрованы, `member_id`, статус подписки)
- **channels** — подключённые MAX Bot каналы (API-ключ зашифрован, `webhook_token`)
- **messages** — история сообщений (нужна для маршрутизации ответа в нужный канал)
- **seen_events** — отпечатки последних запросов от Bitrix24 (защита от дублей, хранятся 1 час)

---

## Требования к серверу

| Требование | Минимум |
|---|---|
| ОС | Ubuntu 20.04+ (или любой Linux) |
| Python | 3.11+ |
| PostgreSQL | 14+ |
| RAM | 512 МБ |
| HTTPS | **Обязательно** — Bitrix24 не отправляет вебхуки на HTTP |
| Домен | Нужен публичный домен с HTTPS (не IP-адрес) |

---

## Шаг 1. Подготовить сервер

Нужен VPS с публичным IP и доменом. Пример для Ubuntu:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3.11 python3.11-venv python3-pip postgresql nginx certbot python3-certbot-nginx -y
```

---

## Шаг 2. Настроить PostgreSQL

```bash
sudo -u postgres psql
```

```sql
CREATE USER maxbot WITH PASSWORD 'придумайте_пароль';
CREATE DATABASE maxbot_db OWNER maxbot;
\q
```

Запишите `DATABASE_URL`:
```
postgresql://maxbot:придумайте_пароль@localhost:5432/maxbot_db
```

---

## Шаг 3. Скопировать код на сервер

```bash
cd /opt
sudo git clone https://github.com/moiseevalyubov/edn_bit maxbot
sudo chown -R $USER:$USER /opt/maxbot
cd /opt/maxbot

python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Шаг 4. Настроить переменные окружения

```bash
cp .env.example .env
nano .env
```

Заполнить все значения:

```env
APP_BASE_URL=https://ваш-домен.ru
DATABASE_URL=postgresql://maxbot:пароль@localhost:5432/maxbot_db
BITRIX_CLIENT_ID=получить_из_партнёрского_кабинета_Bitrix24
BITRIX_CLIENT_SECRET=получить_из_партнёрского_кабинета_Bitrix24
SECRET_KEY=сгенерировать_командой_ниже
```

Сгенерировать `SECRET_KEY`:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

> **ВАЖНО:** `SECRET_KEY` нельзя менять после первого запуска. Все API-ключи и токены в базе зашифрованы этим ключом. Если ключ изменить — данные станут нечитаемыми и все каналы придётся перенастраивать.

---

## Шаг 5. Запустить приложение

### Запуск для проверки (временный)

```bash
source venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Проверить: `curl http://localhost:8000/health` → `{"status":"ok"}`

### Постоянный запуск через systemd

Создать файл сервиса:

```bash
sudo nano /etc/systemd/system/maxbot.service
```

```ini
[Unit]
Description=MAX Bot — Bitrix24 Connector
After=network.target postgresql.service

[Service]
User=www-data
WorkingDirectory=/opt/maxbot
EnvironmentFile=/opt/maxbot/.env
ExecStart=/opt/maxbot/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable maxbot
sudo systemctl start maxbot
sudo systemctl status maxbot
```

---

## Шаг 6. Настроить HTTPS через nginx + certbot

Создать конфиг nginx:

```bash
sudo nano /etc/nginx/sites-available/maxbot
```

```nginx
server {
    listen 80;
    server_name ваш-домен.ru;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 55M;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/maxbot /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# Получить SSL-сертификат
sudo certbot --nginx -d ваш-домен.ru
```

После этого certbot автоматически настроит HTTPS и редирект с HTTP.

---

## Шаг 7. Зарегистрировать приложение в Bitrix24

Это нужно сделать один раз. Если уже сделано — пропустите.

1. Войти в [партнёрский кабинет Bitrix24](https://vendors.bitrix24.ru)
2. Создать новое приложение типа "Тиражное"
3. В настройках приложения указать:
   - **Handler path:** `https://ваш-домен.ru/handler`
   - **Install path:** `https://ваш-домен.ru/install`
   - **Menu path:** `https://ваш-домен.ru/settings`
4. Получить `client_id` и `client_secret` — вставить в `.env`

---

## Шаг 8. Проверить работоспособность

```bash
# Приложение отвечает
curl https://ваш-домен.ru/health

# Логи приложения
sudo journalctl -u maxbot -f

# Состояние сервиса
sudo systemctl status maxbot
```

При установке приложения на тестовый Bitrix24-портал в логах должны появляться сообщения об успешной привязке событий:
```
Bound event OnImConnectorMessageAdd → https://ваш-домен.ru/handler
Bound event OnImConnectorDialogStart → ...
```

---

## Переменные окружения — полная таблица

| Переменная | Обязательная | Описание |
|---|---|---|
| `APP_BASE_URL` | **Да** | Публичный HTTPS-URL сервера, например `https://myapp.example.com`. Без неё вебхуки Bitrix24 не будут работать. |
| `DATABASE_URL` | **Да** | Только PostgreSQL: `postgresql://user:pass@host:5432/db`. SQLite не поддерживается. |
| `SECRET_KEY` | **Да** | Произвольная строка ≥32 символа. Используется для шифрования токенов и API-ключей в БД. **Нельзя менять после первого запуска.** |
| `BITRIX_CLIENT_ID` | **Да** | Client ID приложения из партнёрского кабинета Bitrix24. |
| `BITRIX_CLIENT_SECRET` | **Да** | Client Secret приложения из партнёрского кабинета Bitrix24. |

---

## Обновление приложения

```bash
cd /opt/maxbot
git pull origin master
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart maxbot
```

Миграции БД применяются автоматически при старте приложения.

---

## Альтернатива: деплой на Render (без своего сервера)

Если не хочется настраивать сервер вручную:

1. Создать аккаунт на [render.com](https://render.com)
2. Подключить репозиторий GitHub
3. Создать **Web Service** (тип: Python) — команда запуска возьмётся из `Procfile` автоматически
4. Создать **PostgreSQL** базу данных (Render предоставляет)
5. В разделе **Environment** добавить все переменные из таблицы выше
6. При каждом push в `master` деплой запускается автоматически

> **Важно:** на бесплатном тарифе Render сервер засыпает через 15 минут простоя. Для продакшна нужен тариф **Starter** ($7/мес) или выше.
