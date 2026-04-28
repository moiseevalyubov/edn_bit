# Интеграция MAX Bot с Битрикс24 через кастомный коннектор

## Что мы строим

Промежуточный сервер ("мост") между Битрикс24 и MAX Bot:
- Получает сообщения от пользователей из MAX Bot → передаёт в Горячие линии Битрикс24
- Получает ответы операторов из Битрикс24 → отправляет обратно в MAX Bot

---

## Архитектура

```
Пользователь (MAX Bot)
        ↓ пишет сообщение
MAX Bot Platform
        ↓ вебхук
Ваш сервер (bridge)
        ↓ imconnector.send.messages
Битрикс24 (Горячие линии)
        ↓ OnImConnectorMessageAdd
Ваш сервер (bridge)
        ↓ API MAX Bot
MAX Bot Platform
        ↓ доставляет ответ
Пользователь (MAX Bot)
```

---

## Шаг 1. Какой тип приложения нужен в Битрикс24

Нужно **Локальное серверное приложение** (Local Application).

| Тип | Для чего | Подходит |
|---|---|---|
| Входящий вебхук | Простые запросы к API | ❌ нельзя использовать imconnector.* |
| Локальное приложение | Интеграция для своего портала | ✅ |
| Маркетплейс-приложение | Публикация в каталоге Битрикс24 | Только если нужно продавать другим |

Методы `imconnector.register` и все `imconnector.*` требуют OAuth 2.0-токен от приложения. Через входящий вебхук они вернут ошибку `WRONG_AUTH_TYPE`.

---

## Шаг 2. Создание приложения в Битрикс24

Делается внутри вашего портала, никакой внешней регистрации не нужно.

1. Войдите в Битрикс24 как администратор
2. Перейдите: **Приложения → Разработчикам**
3. Вкладка **"Готовые сценарии"** → **"Другое"** → **"Локальное приложение"**
4. Заполните форму:

| Поле | Что вписать |
|---|---|
| Название | Например: "MAX Bot Connector" |
| Путь к обработчику | `https://ваш-сервер.com/handler` — сюда Битрикс24 шлёт события |
| Путь для установки | `https://ваш-сервер.com/install` — вызывается один раз при установке |
| Права доступа | Выберите **imopenlines** |
| "Использует только API" | ✅ поставьте галочку (нет UI-страниц) |

5. Сохраните. Битрикс24 покажет два значения — **сразу сохраните** их:
   - **ID приложения** → это `client_id`
   - **Ключ приложения** → это `client_secret`

---

## Шаг 3. Где хостить сервер

Ваш сервер должен:
- Быть доступен из интернета (не localhost)
- Работать по HTTPS с валидным SSL-сертификатом (Битрикс24 не принимает HTTP и самоподписанные сертификаты)
- Работать постоянно (принимать события в любое время)

### Варианты хостинга

| Вариант | Цена | HTTPS | Плюсы | Минусы |
|---|---|---|---|---|
| **Render.com** | Бесплатный тариф | Автоматический | Просто, бесплатно, SSL из коробки | Засыпает при неактивности → первый запрос медленный |
| **Railway.app** | ~$5/мес | Автоматический | Надёжно, не засыпает | Нет бесплатного тарифа |
| **VPS (Hetzner, Timeweb, Reg.ru)** | от 200–400 ₽/мес | Через Certbot/Let's Encrypt | Полный контроль, всегда работает | Нужна базовая настройка Linux |
| **Yandex Cloud Functions** | По запросам (дёшево) | Автоматический | Почти бесплатно при малом трафике | Холодный старт, сложнее настройка |
| **Ngrok** | Бесплатно | Автоматический туннель | Работает на localhost, удобно для теста | URL меняется при перезапуске, только для разработки |

**Рекомендация:**
- Для тестирования: **ngrok** на локальной машине
- Для боевого использования: **Render** (бесплатно для старта) или **VPS** (стабильнее)

---

## Шаг 4. Нужен ли GitHub?

**Нет, GitHub не обязателен.** Но он сильно упрощает жизнь:

- Render, Railway, Fly.io умеют деплоить прямо из репозитория — push в GitHub → сервер обновился автоматически
- Без GitHub нужно вручную загружать файлы на VPS через SSH или через FTP

Если вы используете VPS — GitHub не нужен, просто копируете файлы через `scp` или редактируете напрямую.

---

## Шаг 5. Как работает авторизация (OAuth 2.0)

При первом открытии `/install` Битрикс24 пришлёт вам токены:

```
access_token   — действует 1 час
refresh_token  — действует 180 дней
client_endpoint — адрес REST вашего портала
member_id       — уникальный ID портала
```

**Сохраните их** (в базе данных или файле `tokens.json`).

Когда `access_token` истечёт — обновляйте его:

```
POST https://oauth.bitrix.info/oauth/token/
  grant_type=refresh_token
  client_id=ВАШ_CLIENT_ID
  client_secret=ВАШ_CLIENT_SECRET
  refresh_token=СОХРАНЁННЫЙ_REFRESH_TOKEN
```

В ответ придут новые `access_token` и `refresh_token` — сохраните оба.

---

## Шаг 6. Регистрация коннектора

После установки приложения выполните эти 4 вызова к API Битрикс24.

### 6.1 Зарегистрировать коннектор

```
POST https://ваш-портал.bitrix24.com/rest/imconnector.register
```

```json
{
  "auth": "ACCESS_TOKEN",
  "ID": "my_max_connector",
  "NAME": "MAX Bot",
  "ICON": {
    "DATA_IMAGE": "data:image/svg+xml,...",
    "COLOR": "#005FF9"
  },
  "PLACEMENT_HANDLER": "https://ваш-сервер.com/settings"
}
```

Правила для `ID`: только строчные буквы, цифры и `_`. Без точек!

### 6.2 Подписаться на события

```
POST https://ваш-портал.bitrix24.com/rest/event.bind
```

```json
{
  "auth": "ACCESS_TOKEN",
  "event": "OnImConnectorMessageAdd",
  "handler": "https://ваш-сервер.com/handler"
}
```

Также подпишитесь на:
- `OnImConnectorDialogStart` — начало нового диалога
- `OnImConnectorDialogFinish` — закрытие диалога оператором

### 6.3 Узнать ID горячей линии

```
POST https://ваш-портал.bitrix24.com/rest/imopenlines.config.list.get
```

В ответе найдите `ID` нужной Горячей линии (например, `107`).

### 6.4 Активировать коннектор на горячей линии

```
POST https://ваш-портал.bitrix24.com/rest/imconnector.activate
```

```json
{
  "auth": "ACCESS_TOKEN",
  "CONNECTOR": "my_max_connector",
  "LINE": 107,
  "ACTIVE": "1"
}
```

---

## Шаг 7. Поток сообщений

### Входящее (пользователь → Битрикс24)

Когда пользователь пишет в MAX Bot, ваш сервер получает вебхук и вызывает:

```
POST https://ваш-портал.bitrix24.com/rest/imconnector.send.messages
```

```json
{
  "auth": "ACCESS_TOKEN",
  "CONNECTOR": "my_max_connector",
  "LINE": 107,
  "MESSAGES": [{
    "user": {
      "id": "max_user_12345",
      "name": "Иван",
      "last_name": "Петров"
    },
    "message": {
      "id": "max_msg_9001",
      "date": 1773265993,
      "text": "Здравствуйте, мне нужна помощь"
    },
    "chat": {
      "id": "max_chat_456"
    }
  }]
}
```

Ключевое поле: `chat.id` — это ID чата в MAX Bot. Битрикс24 его сохраняет и вернёт вам при ответе оператора.

### Исходящее (оператор → пользователь)

Когда оператор отвечает в Битрикс24, на ваш `handler` приходит POST:

```json
{
  "event": "ONIMCONNECTORMESSAGEADD",
  "data": {
    "CONNECTOR": "my_max_connector",
    "LINE": 107,
    "MESSAGES": [{
      "message": {
        "text": "Здравствуйте! Чем могу помочь?"
      },
      "chat": {
        "id": "max_chat_456"
      }
    }]
  },
  "auth": {
    "application_token": "токен_для_проверки"
  }
}
```

Ваш сервер читает `chat.id` = `"max_chat_456"` и отправляет текст через API MAX Bot в этот чат.

**Проверка безопасности:** сверяйте `auth.application_token` с токеном, сохранённым при установке. Это подтверждает, что запрос пришёл именно от вашего Битрикс24.

---

## Шаг 8. Минимальная структура кода (Python/Flask)

```
your-project/
├── app.py              ← основной сервер
├── tokens.json         ← хранение OAuth-токенов
└── requirements.txt    ← flask, requests
```

**app.py (скелет):**

```python
from flask import Flask, request
import requests, json

app = Flask(__name__)

# Вызывается один раз при установке приложения в Битрикс24
@app.route('/install', methods=['POST', 'GET'])
def install():
    # Битрикс24 присылает сюда первые токены
    # Сохраните access_token и refresh_token
    # Вызовите imconnector.register и event.bind
    return 'OK', 200

# Битрикс24 вызывает это при каждом событии (ответ оператора и т.д.)
@app.route('/handler', methods=['POST'])
def handler():
    event = request.form.get('event')
    if event == 'ONIMCONNECTORMESSAGEADD':
        chat_id = ...   # достать из data[MESSAGES][0][chat][id]
        text = ...      # достать из data[MESSAGES][0][message][text]
        send_to_max_bot(chat_id, text)  # отправить в MAX Bot
    return 'OK', 200

# MAX Bot присылает сюда входящие сообщения пользователей
@app.route('/receive-from-max', methods=['POST'])
def receive_from_max():
    data = request.json
    push_to_bitrix(data['user_id'], data['chat_id'], data['text'])
    return 'OK', 200
```

---

## Шаг 9. Полная последовательность запуска

1. **Разместите сервер** на Render/Railway/VPS — получите HTTPS-адрес
2. **Создайте локальное приложение** в Битрикс24 (Шаг 2), вставьте туда HTTPS-адреса
3. **Сохраните** `client_id` и `client_secret` в конфиг сервера
4. **Задеплойте код** с обработчиком `/install`:
   - Сохраняет OAuth-токены
   - Вызывает `imconnector.register`
   - Вызывает `event.bind`
5. **Откройте** `https://ваш-сервер.com/install` в браузере, будучи авторизованным в Битрикс24 — это запускает установку
6. **Узнайте ID горячей линии** через `imopenlines.config.list.get`
7. **Активируйте** коннектор через `imconnector.activate`
8. **Настройте MAX Bot** — укажите URL вашего сервера как вебхук для входящих сообщений
9. **Проверьте**: напишите сообщение в MAX Bot → оно должно появиться в Горячих линиях → ответьте оператором → ответ должен уйти обратно в MAX Bot

---

## Справочник API-методов

| Метод | Для чего |
|---|---|
| `imconnector.register` | Зарегистрировать коннектор |
| `imconnector.activate` | Включить/выключить коннектор на горячей линии |
| `imconnector.send.messages` | Передать входящее сообщение в Битрикс24 |
| `imconnector.send.status.delivery` | Подтвердить доставку ответа оператора |
| `imconnector.connector.data.set` | Задать отображаемые данные коннектора |
| `imopenlines.config.list.get` | Получить список горячих линий с ID |
| `event.bind` | Подписаться на события Битрикс24 |

## События от Битрикс24 на ваш сервер

| Событие | Когда приходит |
|---|---|
| `OnImConnectorMessageAdd` | Оператор написал ответ |
| `OnImConnectorMessageUpdate` | Оператор отредактировал сообщение |
| `OnImConnectorMessageDelete` | Оператор удалил сообщение |
| `OnImConnectorDialogStart` | Создан новый диалог |
| `OnImConnectorDialogFinish` | Диалог закрыт оператором |
