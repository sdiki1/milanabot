# Telegram-бот для продаж обучения «Искусство быть красивой»

Готовый каркас бота с вашим сценарием:
- `/start` + приветствие с фото
- кнопки `ОПЛАТИТЬ` и `ПОДРОБНЕЕ`
- блоки «Что тебя ждет?» и карточки уроков
- оплата через T-Bank Online эквайринг (`Init` + платежная ссылка)
- фиксация оплаты и выдача одноразовых ссылок в чат/канал
- автонапоминания неоплатившим по датам (3, 5, 6, 7 апреля, `Europe/Moscow`)

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
- `TBANK_PASSWORD`
- `TBANK_NOTIFICATION_URL` (публичный URL callback)
- `COURSE_CHAT_ID`, `COURSE_CHANNEL_ID` (бот должен быть админом)
- `ADMIN_IDS` (кому приходит подтверждение оплаты)
- `WEBHOOK_HOST`, `WEBHOOK_PORT`, `WEBHOOK_PATH`

## 3. Запуск

```bash
set -a
source .env
set +a
python -m bot.main
```

## Webhook T-Bank

T-Bank отправляет callback на `TBANK_NOTIFICATION_URL`.
Локально бот поднимает HTTP endpoint по `WEBHOOK_HOST:WEBHOOK_PORT` + `WEBHOOK_PATH`.

Пример:

```text
TBANK_NOTIFICATION_URL=https://your-domain.com/tbank/notification
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=8080
WEBHOOK_PATH=/tbank/notification
```

После статуса `CONFIRMED`/`AUTHORIZED` бот автоматически выдает доступ.

## Ручное подтверждение (fallback)

Если нужно выдать доступ вручную:

```text
/confirm_paid 123456789
```

## Важно

- Авто-добавление в канал Telegram API не поддерживает: бот выдает одноразовую ссылку на вступление.
- Чтобы одноразовые ссылки создавались, бот должен иметь права администратора в чате и канале.
