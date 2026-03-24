from __future__ import annotations

import asyncio
import base64
import html
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.web_request import FileField
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InputMediaPhoto,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from dotenv import load_dotenv

from bot.config import Settings
from bot.content import (
    PAID_PENDING_TEXT,
    PAYMENT_TEXT,
    Reminder,
    build_reminders,
)
from bot.content_store import ContentStore, split_photo_sources
from bot.db import Database
from bot.keyboards import details_keyboard, pay_only_keyboard, payment_link_keyboard, start_keyboard
from bot.tbank import TBankClient, TBankError, validate_notification_token


logger = logging.getLogger(__name__)


class CourseBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)
        self.reminders = build_reminders(settings.campaign_year, self.tz)
        self.db = Database(settings.db_path)
        self.db.init()
        self.content_store = ContentStore(
            path=settings.content_store_path,
            upload_dir=settings.content_upload_dir,
        )

        self.bot = Bot(
            token=settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher()
        self._reminder_task: asyncio.Task[None] | None = None
        self._web_runner: web.AppRunner | None = None
        self._web_site: web.BaseSite | None = None
        self.tbank_client: TBankClient | None = None
        if self.settings.tbank_enabled:
            self.tbank_client = TBankClient(
                terminal_key=self.settings.tbank_terminal_key,
                password=self.settings.tbank_password,
                api_url=self.settings.tbank_api_url,
                notification_url=(
                    self.settings.tbank_notification_url if self.settings.enable_tbank_webhook else ""
                ),
                success_url=self.settings.tbank_success_url,
                fail_url=self.settings.tbank_fail_url,
            )
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.dp.startup.register(self.on_startup)
        self.dp.shutdown.register(self.on_shutdown)

        self.dp.message.register(self.handle_start, CommandStart())
        self.dp.callback_query.register(self.handle_details, F.data == "details")
        self.dp.callback_query.register(self.handle_what_to_expect, F.data == "what_to_expect")
        self.dp.callback_query.register(self.handle_pay, F.data == "pay")
        self.dp.callback_query.register(self.handle_paid_request, F.data == "paid_request")
        self.dp.pre_checkout_query.register(self.handle_pre_checkout_query)
        self.dp.message.register(self.handle_successful_payment, F.successful_payment)
        self.dp.message.register(self.handle_confirm_paid, Command("confirm_paid"))

    async def on_startup(self, bot: Bot) -> None:
        logger.info("Бот запущен. Кампания: %s", self.settings.campaign_year)
        if self.settings.enable_tbank_webhook or self.settings.admin_panel_enabled:
            await self._start_http_server()
        self._reminder_task = asyncio.create_task(self._reminder_loop())

    async def on_shutdown(self, bot: Bot) -> None:
        if self._reminder_task:
            self._reminder_task.cancel()
            try:
                await self._reminder_task
            except asyncio.CancelledError:
                pass
        if self._web_runner:
            await self._web_runner.cleanup()
        self.db.close()

    async def run(self) -> None:
        await self.dp.start_polling(self.bot)

    async def handle_start(self, message: Message) -> None:
        if not message.from_user:
            return
        self.db.upsert_user(
            user_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        safe_name = html.escape(message.from_user.first_name or "девушка")
        dynamic = self.content_store.get_content()
        start_text = dynamic.start_text.replace("{name}", safe_name)
        await self._send_with_optional_photos(
            chat_id=message.chat.id,
            text=start_text,
            photos=dynamic.start_photos,
            reply_markup=start_keyboard(),
        )

    async def handle_details(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return

        dynamic = self.content_store.get_content()
        await self.bot.send_message(
            callback.message.chat.id,
            text=dynamic.course_overview_text,
            reply_markup=details_keyboard(),
            disable_web_page_preview=True,
            parse_mode=ParseMode.HTML,
        )

    async def handle_what_to_expect(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return

        chat_id = callback.message.chat.id
        dynamic = self.content_store.get_content()
        for lesson in dynamic.lessons:
            if lesson.typing_before_seconds > 0:
                await self._simulate_typing(chat_id, lesson.typing_before_seconds)
            await self._send_lesson_with_photos(chat_id, lesson.text, lesson.photos)

    async def handle_pay(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.from_user:
            return

        if self.tbank_client:
            order_id = self._build_order_id(callback.from_user.id)
            try:
                result = await self.tbank_client.init_payment(
                    order_id=order_id,
                    amount_kopecks=self.settings.course_price_rub * 100,
                    description=self.settings.tbank_order_description,
                    data={"tg_user_id": str(callback.from_user.id)},
                )
            except TBankError as exc:
                logger.warning("Ошибка T-Bank Init для user=%s: %s", callback.from_user.id, exc)
                await self.bot.send_message(
                    callback.from_user.id,
                    "Не удалось создать платеж T‑Bank. Попробуй еще раз через минуту.",
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("Неожиданная ошибка T-Bank Init user=%s: %s", callback.from_user.id, exc)
                await self.bot.send_message(
                    callback.from_user.id,
                    "Техническая ошибка при создании оплаты. Попробуй позже.",
                )
                return

            self.db.create_tbank_order(
                order_id=result.order_id,
                user_id=callback.from_user.id,
                amount=self.settings.course_price_rub * 100,
                payment_id=result.payment_id,
                status="NEW",
            )

            payment_text = (
                "Оплата через T‑Bank Online.\n"
                + (
                    "После успешной оплаты доступ откроется автоматически в течение 1-2 минут.\n\n"
                    if self.settings.enable_tbank_webhook
                    else "После оплаты нажми «Я оплатила», и мы подтвердим доступ вручную.\n\n"
                )
                + f"Стоимость: {self.settings.course_price_rub}₽\n"
                f"{self._legal_notice()}"
            )
            await self._send_with_optional_photos(
                chat_id=callback.from_user.id,
                text=payment_text,
                photos=None,
                reply_markup=payment_link_keyboard(result.payment_url),
            )
            return

        if self.settings.payment_provider_token:
            await self.bot.send_invoice(
                chat_id=callback.from_user.id,
                title="Пакет уроков «Искусство быть красивой»",
                description="Доступ навсегда. 4 урока + бонусы + чат + эфир + обратная связь.",
                payload=f"course_access_{callback.from_user.id}",
                provider_token=self.settings.payment_provider_token,
                currency="RUB",
                prices=[
                    LabeledPrice(
                        label="Доступ к обучению",
                        amount=self.settings.course_price_rub * 100,
                    )
                ],
                start_parameter="beauty_course_access",
            )
            return

        if self.settings.payment_url:
            payment_text = (
                f"{PAYMENT_TEXT}\n\n"
                f"Стоимость: {self.settings.course_price_rub}₽\n"
                f"{self._legal_notice()}"
            )
            await self._send_with_optional_photos(
                chat_id=callback.from_user.id,
                text=payment_text,
                photos=self._promo_photos(),
                reply_markup=payment_link_keyboard(self.settings.payment_url),
            )
            return

        await self.bot.send_message(
            callback.from_user.id,
            "Оплата не настроена: укажите реквизиты T‑Bank или fallback PAYMENT_URL/PAYMENT_PROVIDER_TOKEN в .env.",
        )

    async def handle_paid_request(self, callback: CallbackQuery) -> None:
        await callback.answer("Запрос отправлен, проверяем оплату.")
        if not callback.from_user:
            return

        await self.bot.send_message(callback.from_user.id, PAID_PENDING_TEXT)

        if self.settings.admin_ids:
            username = (
                f"@{callback.from_user.username}"
                if callback.from_user.username
                else callback.from_user.full_name
            )
            notify_text = (
                "Пользователь сообщил об оплате:\n"
                f"- id: <code>{callback.from_user.id}</code>\n"
                f"- user: {username}\n\n"
                f"Подтвердить доступ: <code>/confirm_paid {callback.from_user.id}</code>"
            )
            await self._notify_admins(notify_text)

    async def handle_pre_checkout_query(self, pre_checkout_query: PreCheckoutQuery) -> None:
        await pre_checkout_query.answer(ok=True)

    async def handle_successful_payment(self, message: Message) -> None:
        if not message.from_user:
            return
        self.db.set_paid(message.from_user.id, True)
        await self._send_access_links(message.from_user.id)

    async def handle_confirm_paid(self, message: Message) -> None:
        if not message.from_user or message.from_user.id not in self.settings.admin_ids:
            return
        parts = message.text.split(maxsplit=1) if message.text else []
        if len(parts) != 2:
            await message.answer("Использование: /confirm_paid <user_id>")
            return
        try:
            user_id = int(parts[1])
        except ValueError:
            await message.answer("user_id должен быть числом.")
            return

        self.db.set_paid(user_id, True)
        await self._send_access_links(user_id)
        await message.answer(f"Пользователь {user_id} отмечен как оплативший.")

    def _build_order_id(self, user_id: int) -> str:
        return f"mila_{user_id}_{int(datetime.now(timezone.utc).timestamp() * 1000)}"

    async def _start_http_server(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._handle_root)
        if self.settings.enable_tbank_webhook:
            app.router.add_post(self.settings.webhook_path, self._handle_tbank_notification)
        if self.settings.admin_panel_enabled:
            app.router.add_get("/admin", self._handle_admin_get)
            app.router.add_post("/admin", self._handle_admin_post)
        self._web_runner = web.AppRunner(app)
        await self._web_runner.setup()

        self._web_site = web.TCPSite(
            self._web_runner,
            host=self.settings.webhook_host,
            port=self.settings.webhook_port,
        )
        await self._web_site.start()
        logger.info("HTTP server started at %s:%s", self.settings.webhook_host, self.settings.webhook_port)
        if self.settings.enable_tbank_webhook:
            logger.info("Webhook path active: %s", self.settings.webhook_path)
        if self.settings.admin_panel_enabled:
            logger.info("Admin panel active at /admin")

    async def _handle_root(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    def _is_admin_authorized(self, request: web.Request) -> bool:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            return False
        token = auth_header[6:]
        try:
            decoded = base64.b64decode(token).decode("utf-8")
        except Exception:
            return False
        username, _, password = decoded.partition(":")
        return (
            hmac.compare_digest(username, self.settings.admin_panel_username)
            and hmac.compare_digest(password, self.settings.admin_panel_password)
        )

    def _admin_unauthorized(self) -> web.Response:
        return web.Response(
            status=401,
            text="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="Milana Bot Admin"'},
        )

    async def _handle_admin_get(self, request: web.Request) -> web.Response:
        if not self._is_admin_authorized(request):
            return self._admin_unauthorized()
        return web.Response(
            text=self._render_admin_html(saved=request.query.get("saved") == "1"),
            content_type="text/html",
        )

    async def _handle_admin_post(self, request: web.Request) -> web.Response:
        if not self._is_admin_authorized(request):
            return self._admin_unauthorized()

        form = await request.post()
        current = self.content_store.get_content()
        lesson_count = len(current.lessons)

        start_text = str(form.get("start_text", current.start_text))
        overview_text = str(form.get("course_overview_text", current.course_overview_text))

        start_photo_urls_raw = str(
            form.get("start_photo_urls", "\n".join(current.start_photos))
        )
        start_photos = split_photo_sources(start_photo_urls_raw)
        start_photos.extend(self._extract_uploaded_paths(form, "start_photo_files"))

        lessons_payload: list[dict[str, object]] = []
        for idx in range(lesson_count):
            current_lesson = current.lessons[idx]
            lesson_text = str(form.get(f"lesson_{idx}_text", current_lesson.text))
            photos_raw = str(
                form.get(f"lesson_{idx}_photo_urls", "\n".join(current_lesson.photos))
            )
            photos = split_photo_sources(photos_raw)
            photos.extend(self._extract_uploaded_paths(form, f"lesson_{idx}_photo_files"))
            typing_raw = str(form.get(f"lesson_{idx}_typing", current_lesson.typing_before_seconds))
            try:
                typing_before = max(0, int(typing_raw))
            except ValueError:
                typing_before = current_lesson.typing_before_seconds
            lessons_payload.append(
                {
                    "text": lesson_text,
                    "photos": photos,
                    "typing_before_seconds": typing_before,
                }
            )

        self.content_store.update(
            start_text=start_text,
            start_photos=start_photos,
            course_overview_text=overview_text,
            lessons=lessons_payload,
        )
        raise web.HTTPFound(location="/admin?saved=1")

    def _extract_uploaded_paths(self, form, field_name: str) -> list[str]:
        values = form.getall(field_name, [])
        saved_paths: list[str] = []
        for value in values:
            if not isinstance(value, FileField):
                continue
            if not value.filename:
                continue
            raw_name = os.path.basename(value.filename)
            safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw_name)
            if not safe_name:
                safe_name = "upload.bin"
            unique_name = f"{int(datetime.now(timezone.utc).timestamp() * 1000)}_{secrets.token_hex(4)}_{safe_name}"
            path = Path(self.content_store.upload_dir) / unique_name
            with path.open("wb") as output:
                output.write(value.file.read())
            saved_paths.append(str(path))
        return saved_paths

    def _render_admin_html(self, saved: bool = False) -> str:
        dynamic = self.content_store.get_content()

        def esc(value: str) -> str:
            return html.escape(value, quote=True)

        lesson_blocks: list[str] = []
        for idx, lesson in enumerate(dynamic.lessons):
            lesson_blocks.append(
                f"""
                <section class="card">
                  <h2>Сообщение 3.{idx + 1}</h2>
                  <label>Текст</label>
                  <textarea name="lesson_{idx}_text" rows="7">{esc(lesson.text)}</textarea>
                  <label>Фото/ссылки (по одной строке)</label>
                  <textarea name="lesson_{idx}_photo_urls" rows="4">{esc("\\n".join(lesson.photos))}</textarea>
                  <label>Загрузить фото файлами (добавятся к списку)</label>
                  <input type="file" name="lesson_{idx}_photo_files" multiple accept="image/*" />
                  <label>Задержка печати перед этим блоком (сек)</label>
                  <input type="number" min="0" name="lesson_{idx}_typing" value="{lesson.typing_before_seconds}" />
                </section>
                """
            )

        saved_banner = "<div class='ok'>Сохранено.</div>" if saved else ""
        return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Milana Bot Admin</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; background:#f3f5f7; color:#1c1c1c; }}
    .wrap {{ max-width: 1100px; margin: 24px auto; padding: 0 16px 48px; }}
    h1 {{ margin: 0 0 12px; }}
    .ok {{ background:#e8f7e8; border:1px solid #94d894; padding:10px 12px; border-radius:8px; margin: 12px 0; }}
    .card {{ background:#fff; border:1px solid #d9dde3; border-radius:12px; padding:16px; margin-top:16px; }}
    label {{ display:block; font-size:14px; margin:10px 0 6px; color:#4d5561; }}
    textarea, input[type="number"], input[type="text"], input[type="file"] {{ width:100%; box-sizing:border-box; }}
    textarea, input[type="number"], input[type="text"] {{ border:1px solid #c9d0d8; border-radius:8px; padding:10px; background:#fff; }}
    button {{ margin-top:20px; background:#1f6feb; color:#fff; border:none; border-radius:10px; padding:12px 18px; font-size:15px; cursor:pointer; }}
    .hint {{ font-size:13px; color:#667084; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Milana Bot Admin</h1>
    <p class="hint">Редактируй тексты и фото. Для фото можно указать ссылки или загрузить файлы.</p>
    {saved_banner}
    <form method="post" enctype="multipart/form-data">
      <section class="card">
        <h2>Сообщение 1 (Start)</h2>
        <label>Текст</label>
        <textarea name="start_text" rows="9">{esc(dynamic.start_text)}</textarea>
        <label>Фото/ссылки (по одной строке)</label>
        <textarea name="start_photo_urls" rows="3">{esc("\\n".join(dynamic.start_photos))}</textarea>
        <label>Загрузить фото файлами (добавятся к списку)</label>
        <input type="file" name="start_photo_files" multiple accept="image/*" />
      </section>

      <section class="card">
        <h2>Сообщение 2 (Подробнее)</h2>
        <label>Текст</label>
        <textarea name="course_overview_text" rows="14">{esc(dynamic.course_overview_text)}</textarea>
      </section>

      {"".join(lesson_blocks)}

      <button type="submit">Сохранить Изменения</button>
    </form>
  </div>
</body>
</html>
"""

    async def _handle_tbank_notification(self, request: web.Request) -> web.Response:
        if not self.tbank_client:
            return web.Response(status=503, text="DISABLED")

        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400, text="BAD_REQUEST")

        if not isinstance(payload, dict):
            return web.Response(status=400, text="BAD_REQUEST")

        if not validate_notification_token(payload, self.settings.tbank_password):
            logger.warning("T-Bank callback rejected: invalid token")
            return web.Response(status=403, text="INVALID_TOKEN")

        asyncio.create_task(self._process_tbank_notification(payload))
        return web.Response(status=200, text="OK")

    async def _process_tbank_notification(self, payload: dict) -> None:
        order_id = str(payload.get("OrderId", "")).strip()
        payment_id = str(payload.get("PaymentId", "")).strip()
        status = str(payload.get("Status", "")).upper().strip()
        error_code = str(payload.get("ErrorCode", "")).strip()
        raw_success = payload.get("Success")
        success = raw_success is True or str(raw_success).lower() in {"true", "1"}

        if order_id:
            self.db.update_tbank_order_status(order_id, status or "UNKNOWN", payment_id)

        if not order_id:
            return

        user_id = self.db.get_tbank_order_user_id(order_id)
        if user_id is None:
            logger.warning("T-Bank callback for unknown order_id=%s", order_id)
            return

        if success and error_code in ("", "0") and status in {"AUTHORIZED", "CONFIRMED"}:
            if not self.db.is_paid(user_id):
                self.db.set_paid(user_id, True)
                await self._send_access_links(user_id)
                if self.settings.admin_ids:
                    await self._notify_admins(
                        "Подтверждена оплата T‑Bank:\n"
                        f"- user_id: <code>{user_id}</code>\n"
                        f"- order_id: <code>{order_id}</code>\n"
                        f"- status: {status}"
                    )
            return

        if status in {"REJECTED", "CANCELED", "DEADLINE_EXPIRED"}:
            try:
                await self.bot.send_message(
                    user_id,
                    "Платеж не завершен. Попробуй оплатить снова через кнопку «Оплатить».",
                )
            except TelegramBadRequest:
                pass

    async def _send_access_links(self, user_id: int) -> None:
        channel_link = await self._create_one_time_link(self.settings.course_channel_id)

        lines = ["Оплата принята, спасибо!!"]
        if channel_link:
            lines.append(f"Твоя разовая ссылка на вступление в канал:\n{channel_link}")
        else:
            lines.append("Не удалось создать ссылку в канал. Напиши в поддержку для выдачи доступа.")
        lines.append(f"По вопросам: {self.settings.support_contact}")

        await self.bot.send_message(user_id, "\n\n".join(lines))

    async def _create_one_time_link(self, chat_id: int | None) -> str:
        if chat_id is None:
            return ""
        expire_at = datetime.now(timezone.utc) + timedelta(hours=24)
        try:
            invite = await self.bot.create_chat_invite_link(
                chat_id=chat_id,
                expire_date=expire_at,
                member_limit=1,
                name="paid_access",
            )
            return invite.invite_link
        except TelegramBadRequest as exc:
            logger.warning("Не удалось создать invite link для %s: %s", chat_id, exc)
            return ""

    async def _notify_admins(self, text: str) -> None:
        for admin_id in self.settings.admin_ids:
            try:
                await self.bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
            except TelegramBadRequest as exc:
                logger.warning("Не отправлено администратору %s: %s", admin_id, exc)

    async def _reminder_loop(self) -> None:
        while True:
            now_local = datetime.now(self.tz)
            for reminder in self.reminders:
                if now_local < reminder.when:
                    continue
                due_utc_iso = reminder.when.astimezone(timezone.utc).isoformat()
                user_ids = self.db.get_unpaid_user_ids_for_reminder(reminder.reminder_id, due_utc_iso)
                if not user_ids:
                    continue

                for user_id in user_ids:
                    sent = False
                    try:
                        await self._send_reminder(user_id, reminder)
                        sent = True
                    except TelegramForbiddenError:
                        sent = True
                    except TelegramBadRequest as exc:
                        logger.warning("Reminder %s user %s failed: %s", reminder.reminder_id, user_id, exc)
                        sent = True
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Unexpected reminder error for %s: %s", user_id, exc)
                    if sent:
                        self.db.mark_reminder_sent(user_id, reminder.reminder_id)
                    await asyncio.sleep(0.05)
            await asyncio.sleep(30)

    async def _send_reminder(self, user_id: int, reminder: Reminder) -> None:
        await self._send_with_optional_photos(
            chat_id=user_id,
            text=reminder.text,
            photos=self._promo_photos() if reminder.with_photo else (),
            reply_markup=pay_only_keyboard(),
        )

    def _welcome_photos(self) -> tuple[str, ...]:
        dynamic = self.content_store.get_content()
        if dynamic.start_photos:
            return dynamic.start_photos
        if self.settings.welcome_photo_url:
            return (self.settings.welcome_photo_url,)
        return self._promo_photos()

    def _promo_photos(self) -> tuple[str, ...]:
        return self.settings.promo_photo_urls[:2]

    def _photo_input(self, source: str):
        source = source.strip()
        if not source:
            return source
        if source.startswith(("http://", "https://")):
            return source
        if os.path.exists(source):
            return FSInputFile(source)
        return source

    async def _simulate_typing(self, chat_id: int, seconds: int) -> None:
        remaining = max(0, seconds)
        while remaining > 0:
            await self.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            step = min(4, remaining)
            await asyncio.sleep(step)
            remaining -= step

    async def _send_lesson_with_photos(
        self,
        chat_id: int,
        text: str,
        photos: tuple[str, ...],
    ) -> None:
        if not photos:
            await self.bot.send_message(
                chat_id,
                text,
                reply_markup=pay_only_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        if len(photos) == 1:
            try:
                await self.bot.send_photo(
                    chat_id=chat_id,
                    photo=self._photo_input(photos[0]),
                    caption=text,
                    reply_markup=pay_only_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            except TelegramBadRequest:
                await self.bot.send_message(
                    chat_id,
                    text,
                    reply_markup=pay_only_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            return

        try:
            media = [InputMediaPhoto(media=self._photo_input(url)) for url in photos]
            await self.bot.send_media_group(chat_id=chat_id, media=media)
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=pay_only_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        except TelegramBadRequest:
            await self.bot.send_message(
                chat_id,
                text,
                reply_markup=pay_only_keyboard(),
                parse_mode=ParseMode.HTML,
            )

    async def _send_with_optional_photos(
        self,
        chat_id: int,
        text: str,
        photos: tuple[str, ...],
        reply_markup=None,
    ) -> None:
        if not photos:
            await self.bot.send_message(
                chat_id,
                text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )
            return

        if len(photos) == 1:
            try:
                await self.bot.send_photo(
                    chat_id=chat_id,
                    photo=self._photo_input(photos[0]),
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML,
                )
                return
            except TelegramBadRequest:
                await self.bot.send_message(
                    chat_id,
                    text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML,
                )
                return

        media = [InputMediaPhoto(media=self._photo_input(photos[0]), caption=text, parse_mode=ParseMode.HTML)]
        media.extend(InputMediaPhoto(media=self._photo_input(url)) for url in photos[1:])
        try:
            await self.bot.send_media_group(chat_id=chat_id, media=media)
            if reply_markup:
                await self.bot.send_message(chat_id, "Выбери действие:", reply_markup=reply_markup)
            return
        except TelegramBadRequest:
            await self.bot.send_message(
                chat_id,
                text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )

    def _legal_notice(self) -> str:
        if self.settings.offer_url and self.settings.privacy_url:
            return (
                "Нажимая кнопку «Оплатить», вы безоговорочно соглашаетесь с условиями "
                f"<a href=\"{self.settings.offer_url}\">Оферты</a> и даете согласие "
                "на обработку персональных данных в соответствии с "
                f"<a href=\"{self.settings.privacy_url}\">Политикой обработки персональных данных</a>."
            )
        return (
            "Нажимая кнопку «Оплатить», вы соглашаетесь с условиями оферты и политикой "
            "обработки персональных данных."
        )


async def _async_main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv()
    settings = Settings.from_env()
    app = CourseBot(settings)
    await app.run()


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
