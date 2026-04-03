# Telegram-бот для продаж обучения «Искусство быть красивой»

Готовый каркас бота с вашим сценарием:
- `/start` + приветствие с фото
- кнопки `ОПЛАТИТЬ` и `ПОДРОБНЕЕ`
- блоки «Что тебя ждет?» и карточки уроков
- оплата через T-Bank Online эквайринг (`Init` + платежная ссылка)
- фиксация оплаты и выдача одноразовых ссылок в чат/канал
- автонапоминания неоплатившим по датам (3, 5, 6, 7 апреля, `Europe/Moscow`)
- ручная рассылка из админ-панели по всем пользователям, кто не оплатил курс

## 1. Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Настройка

```bash
cp .env.example .env
```

Заполни минимум:
- `BOT_TOKEN`
- `TBANK_TERMINAL_KEY`
- `TBANK_PASSWORD` (SecretKey/Password терминала из кабинета эквайринга)
- `COURSE_CHAT_ID`, `COURSE_CHANNEL_ID` (бот должен быть админом)
- `ADMIN_IDS` (кому приходит подтверждение оплаты)
- `ENABLE_TBANK_WEBHOOK=false` (по умолчанию)
- `ADMIN_PANEL_ENABLED=true`
- `ADMIN_PANEL_USERNAME`, `ADMIN_PANEL_PASSWORD` (доступ к сайту `/admin`)

### Проверка TerminalKey/SecretKey вручную

```bash
python scripts/check_tbank_init.py \
  --terminal-key "ВАШ_TERMINAL_KEY" \
  --secret-key 'ВАШ_SECRET_KEY_С_СИМВОЛОМ_#' \
  --amount-kop 1000
```

Если получаешь `ErrorCode=204/205`, значит пара ключей некорректна (или от разных терминалов).

## 3. Запуск через Docker Compose

```bash
docker compose up -d --build
```

Логи:

```bash
docker compose logs -f bot
```

Админ-панель:

```text
https://your-domain.com/admin
```

Логин/пароль берутся из `.env` (`ADMIN_PANEL_USERNAME`, `ADMIN_PANEL_PASSWORD`).

Остановка:

```bash
docker compose down
```

## 4. Запуск локально (без Docker)

```bash
set -a
source .env
set +a
python -m bot.main
```

## Webhook T-Bank

По умолчанию webhook отключен (`ENABLE_TBANK_WEBHOOK=false`), бот работает в polling-режиме без входящего HTTP-порта.
После оплаты пользователь нажимает `Я оплатила`, администратор подтверждает доступ командой `/confirm_paid <user_id>`.

Если нужно автоматическое подтверждение, включи webhook:

```text
ENABLE_TBANK_WEBHOOK=true
TBANK_NOTIFICATION_URL=https://your-domain.com/tbank/notification
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=8080
WEBHOOK_PATH=/tbank/notification
```

T-Bank отправляет callback на `TBANK_NOTIFICATION_URL`.
Локально бот поднимает HTTP endpoint по `WEBHOOK_HOST:WEBHOOK_PORT` + `WEBHOOK_PATH`.
После статуса `CONFIRMED`/`AUTHORIZED` бот автоматически выдает доступ.

## Ручное подтверждение (fallback)

Если нужно выдать доступ вручную:

```text
/confirm_paid 123456789
```

Чтобы узнать ID конкретного сообщения в чате:

```text
1) Ответь на сообщение командой /getidmessage
2) Бот пришлет message_id того сообщения
```

## Важно

- Авто-добавление в канал Telegram API не поддерживает: бот выдает одноразовую ссылку на вступление.
- Чтобы одноразовые ссылки создавались, бот должен иметь права администратора в чате и канале.
- В Docker Compose база SQLite хранится в volume `bot_data` по пути `/app/data/bot_data.sqlite3`.
- Контент админ-панели хранится в `/app/data/content_overrides.json`, загруженные фото — в `/app/data/uploads`.
