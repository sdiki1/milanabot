from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InputMediaPhoto,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from dotenv import load_dotenv

from bot.config import Settings
from bot.content import (
    COURSE_OVERVIEW_TEXT,
    LESSON_SHOWCASE,
    PAID_PENDING_TEXT,
    PAYMENT_TEXT,
    START_PHOTO_URL,
    START_TEXT,
    Reminder,
    build_reminders,
)
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
        if self.tbank_client and self.settings.enable_tbank_webhook:
            await self._start_tbank_notification_server()
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
        start_text = START_TEXT.replace("{name}", safe_name)
        await self._send_with_optional_photos(
            chat_id=message.chat.id,
            text=start_text,
            photos=self._welcome_photos(),
            reply_markup=start_keyboard(),
        )

    async def handle_details(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return

        await self.bot.send_message(
            callback.message.chat.id,
            text=COURSE_OVERVIEW_TEXT,
            reply_markup=details_keyboard(),
            disable_web_page_preview=True,
            parse_mode=ParseMode.HTML,
        )

    async def handle_what_to_expect(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return

        chat_id = callback.message.chat.id
        for lesson in LESSON_SHOWCASE:
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

    async def _start_tbank_notification_server(self) -> None:
        app = web.Application()
        app.router.add_post(self.settings.webhook_path, self._handle_tbank_notification)
        self._web_runner = web.AppRunner(app)
        await self._web_runner.setup()

        self._web_site = web.TCPSite(
            self._web_runner,
            host=self.settings.webhook_host,
            port=self.settings.webhook_port,
        )
        await self._web_site.start()
        logger.info(
            "T-Bank webhook server started at %s:%s%s",
            self.settings.webhook_host,
            self.settings.webhook_port,
            self.settings.webhook_path,
        )

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
        if START_PHOTO_URL:
            return (START_PHOTO_URL,)
        if self.settings.welcome_photo_url:
            return (self.settings.welcome_photo_url,)
        return self._promo_photos()

    def _promo_photos(self) -> tuple[str, ...]:
        return self.settings.promo_photo_urls[:2]

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
                    photo=photos[0],
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
            media = [InputMediaPhoto(media=url) for url in photos]
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
                    photo=photos[0],
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

        media = [InputMediaPhoto(media=photos[0], caption=text, parse_mode=ParseMode.HTML)]
        media.extend(InputMediaPhoto(media=url) for url in photos[1:])
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
