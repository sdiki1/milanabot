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
from urllib.parse import urlencode
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
            default_course_price_rub=settings.course_price_rub,
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
        self.dp.message.register(self.handle_getidmessage, Command("getidmessage"))

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

        course_price_rub = self.content_store.get_content().course_price_rub

        if self.tbank_client:
            order_id = self._build_order_id(callback.from_user.id)
            try:
                result = await self.tbank_client.init_payment(
                    order_id=order_id,
                    amount_kopecks=course_price_rub * 100,
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
                amount=course_price_rub * 100,
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
                + f"Стоимость: {course_price_rub}₽\n"
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
                        amount=course_price_rub * 100,
                    )
                ],
                start_parameter="beauty_course_access",
            )
            return

        if self.settings.payment_url:
            payment_text = (
                f"{PAYMENT_TEXT}\n\n"
                f"Стоимость: {course_price_rub}₽\n"
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
        await callback.answer("Проверяю статус оплаты...")
        if not callback.from_user:
            return

        user_id = callback.from_user.id
        if self.db.is_paid(user_id):
            await self.bot.send_message(
                user_id,
                "Оплата уже подтверждена. Доступ выдан ранее.",
            )
            return

        if self.tbank_client:
            last_order = self.db.get_last_tbank_order_for_user(user_id)
            if not last_order:
                await self.bot.send_message(
                    user_id,
                    "Я не нашла платеж в T‑Bank для твоего аккаунта. Нажми «Оплатить» и после оплаты попробуй снова.",
                )
                return

            try:
                state = await self.tbank_client.get_payment_state(
                    payment_id=last_order.payment_id,
                    order_id=last_order.order_id,
                )
            except TBankError as exc:
                logger.warning("Ошибка T-Bank GetState для user=%s: %s", user_id, exc)
                await self.bot.send_message(user_id, PAID_PENDING_TEXT)
                await self._notify_paid_request_admins(
                    callback.from_user,
                    tbank_status="GET_STATE_ERROR",
                    order_id=last_order.order_id,
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("Неожиданная ошибка T-Bank GetState user=%s: %s", user_id, exc)
                await self.bot.send_message(user_id, PAID_PENDING_TEXT)
                await self._notify_paid_request_admins(
                    callback.from_user,
                    tbank_status="GET_STATE_EXCEPTION",
                    order_id=last_order.order_id,
                )
                return

            resolved_order_id = state.order_id or last_order.order_id
            resolved_payment_id = state.payment_id or last_order.payment_id
            resolved_status = state.status or last_order.status or "UNKNOWN"
            self.db.update_tbank_order_status(
                resolved_order_id,
                resolved_status,
                resolved_payment_id,
            )

            if (
                state.success
                and state.error_code in {"", "0"}
                and resolved_status in {"AUTHORIZED", "CONFIRMED"}
            ):
                self.db.set_paid(user_id, True)
                await self._send_access_links(user_id)
                await self._notify_paid_request_admins(
                    callback.from_user,
                    tbank_status=resolved_status,
                    order_id=resolved_order_id,
                )
                return

            if resolved_status in {"REJECTED", "CANCELED", "DEADLINE_EXPIRED"}:
                await self.bot.send_message(
                    user_id,
                    "Платеж не завершен. Проверь оплату и попробуй снова через кнопку «Оплатить».",
                )
                return

            await self.bot.send_message(user_id, PAID_PENDING_TEXT)
            await self._notify_paid_request_admins(
                callback.from_user,
                tbank_status=resolved_status,
                order_id=resolved_order_id,
            )
            return

        await self.bot.send_message(user_id, PAID_PENDING_TEXT)
        await self._notify_paid_request_admins(callback.from_user)

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

    async def handle_getidmessage(self, message: Message) -> None:
        if not message.reply_to_message:
            await message.answer("Ответь командой /getidmessage на нужное сообщение.")
            return
        await message.answer(f"ID сообщения: <code>{message.reply_to_message.message_id}</code>")

    async def _notify_paid_request_admins(
        self,
        user,
        tbank_status: str = "",
        order_id: str = "",
    ) -> None:
        if not self.settings.admin_ids:
            return

        username = html.escape(f"@{user.username}" if user.username else user.full_name)
        escaped_status = html.escape(tbank_status)
        escaped_order_id = html.escape(order_id)
        lines = [
            "Пользователь сообщил об оплате:",
            f"- id: <code>{user.id}</code>",
            f"- user: {username}",
        ]
        if tbank_status:
            lines.append(f"- статус T‑Bank: <code>{escaped_status}</code>")
        if order_id:
            lines.append(f"- order_id: <code>{escaped_order_id}</code>")
        lines.extend(["", f"Подтвердить доступ: <code>/confirm_paid {user.id}</code>"])
        await self._notify_admins("\n".join(lines))

    def _build_order_id(self, user_id: int) -> str:
        return f"mila_{user_id}_{int(datetime.now(timezone.utc).timestamp() * 1000)}"

    async def _start_http_server(self) -> None:
        # Админ-панель отправляет multipart/form-data с фото, поэтому увеличиваем лимит тела запроса.
        app = web.Application(client_max_size=10 * 1024 * 1024)
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
        mailing_message = ""
        mailing_message_class = ""
        if request.query.get("mailing") == "1":
            mailing_error = request.query.get("mailing_error")
            if mailing_error == "empty_text":
                mailing_message = "Рассылка не отправлена: добавь текст сообщения."
                mailing_message_class = "warn"
            elif mailing_error == "target_user_required":
                mailing_message = "Рассылка не отправлена: выбери пользователя для точечной отправки."
                mailing_message_class = "warn"
            elif mailing_error == "target_user_not_found":
                mailing_message = (
                    "Рассылка не отправлена: выбранный пользователь не найден в базе."
                )
                mailing_message_class = "warn"
            else:
                total = self._parse_non_negative_int(request.query.get("mailing_total"))
                sent = self._parse_non_negative_int(request.query.get("mailing_sent"))
                failed = self._parse_non_negative_int(request.query.get("mailing_failed"))
                if request.query.get("mailing_target") == "single":
                    target_user = self._parse_non_negative_int(
                        request.query.get("mailing_target_user")
                    )
                    mailing_message = (
                        "Точечная рассылка завершена. "
                        f"user_id: {target_user}; отправлено: {sent}; ошибок: {failed}."
                    )
                else:
                    mailing_message = (
                        "Рассылка завершена. "
                        f"Неоплативших: {total}; отправлено: {sent}; ошибок: {failed}."
                    )
                mailing_message_class = "ok"

        unpaid_count = len(self.db.get_all_unpaid_user_ids())
        admin_users = self.db.get_all_users_for_admin()
        return web.Response(
            text=self._render_admin_html(
                saved=request.query.get("saved") == "1",
                unpaid_count=unpaid_count,
                admin_users=admin_users,
                mailing_message=mailing_message,
                mailing_message_class=mailing_message_class,
            ),
            content_type="text/html",
        )

    async def _handle_admin_post(self, request: web.Request) -> web.Response:
        if not self._is_admin_authorized(request):
            return self._admin_unauthorized()

        form = await request.post()
        action = str(form.get("action", "save_content"))
        if action == "broadcast_unpaid":
            broadcast_text = str(form.get("broadcast_text", "")).strip()
            if not broadcast_text:
                raise web.HTTPFound(
                    location=self._build_admin_location(
                        mailing=1,
                        mailing_error="empty_text",
                    )
                )

            broadcast_target_mode = str(form.get("broadcast_target_mode", "unpaid")).strip().lower()
            if broadcast_target_mode not in {"unpaid", "single"}:
                broadcast_target_mode = "unpaid"

            target_user_id: int | None = None
            if broadcast_target_mode == "single":
                target_user_raw = str(form.get("broadcast_target_user_id", "")).strip()
                try:
                    target_user_id = int(target_user_raw)
                except ValueError:
                    target_user_id = None
                if not target_user_id or target_user_id <= 0:
                    raise web.HTTPFound(
                        location=self._build_admin_location(
                            mailing=1,
                            mailing_error="target_user_required",
                        )
                    )
                if not self.db.user_exists(target_user_id):
                    raise web.HTTPFound(
                        location=self._build_admin_location(
                            mailing=1,
                            mailing_error="target_user_not_found",
                        )
                    )

            broadcast_photo_paths = self._extract_uploaded_paths(form, "broadcast_photo_file")
            broadcast_photo = broadcast_photo_paths[0] if broadcast_photo_paths else ""

            with_pay_button = str(form.get("broadcast_with_pay_button", "")).lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            total, sent, failed = await self._send_unpaid_broadcast(
                text=broadcast_text,
                with_pay_button=with_pay_button,
                target_user_id=target_user_id,
                photo_source=broadcast_photo,
            )
            logger.info(
                "Admin broadcast completed: mode=%s target_user=%s total=%s sent=%s failed=%s",
                broadcast_target_mode,
                target_user_id,
                total,
                sent,
                failed,
            )
            redirect_params: dict[str, int | str] = {
                "mailing": 1,
                "mailing_total": total,
                "mailing_sent": sent,
                "mailing_failed": failed,
            }
            if broadcast_target_mode == "single" and target_user_id is not None:
                redirect_params["mailing_target"] = "single"
                redirect_params["mailing_target_user"] = target_user_id
            raise web.HTTPFound(
                location=self._build_admin_location(**redirect_params)
            )

        current = self.content_store.get_content()
        lesson_count = len(current.lessons)

        start_text = str(form.get("start_text", current.start_text))
        overview_text = str(form.get("course_overview_text", current.course_overview_text))
        price_raw = str(form.get("course_price_rub", current.course_price_rub))
        try:
            course_price_rub = max(1, int(price_raw))
        except ValueError:
            course_price_rub = current.course_price_rub

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
            course_price_rub=course_price_rub,
        )
        raise web.HTTPFound(location=self._build_admin_location(saved=1))

    @staticmethod
    def _parse_non_negative_int(raw_value: str | None) -> int:
        try:
            value = int(raw_value or "0")
        except ValueError:
            return 0
        return max(0, value)

    @staticmethod
    def _build_admin_location(**params: int | str) -> str:
        encoded = urlencode({key: str(value) for key, value in params.items()})
        return f"/admin?{encoded}" if encoded else "/admin"

    async def _send_unpaid_broadcast(
        self,
        text: str,
        with_pay_button: bool,
        target_user_id: int | None = None,
        photo_source: str = "",
    ) -> tuple[int, int, int]:
        target_user_ids = (
            [target_user_id]
            if target_user_id is not None
            else self.db.get_all_unpaid_user_ids()
        )
        total = len(target_user_ids)
        if total == 0:
            return 0, 0, 0

        sent = 0
        failed = 0
        normalized_photo = photo_source.strip()
        reply_markup = pay_only_keyboard() if with_pay_button else None

        for user_id in target_user_ids:
            try:
                await self._send_broadcast_message(
                    chat_id=user_id,
                    text=text,
                    photo_source=normalized_photo,
                    reply_markup=reply_markup,
                )
                sent += 1
            except TelegramForbiddenError:
                failed += 1
            except TelegramBadRequest as exc:
                failed += 1
                logger.warning("Broadcast to user %s failed: %s", user_id, exc)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.exception("Unexpected broadcast error for user %s: %s", user_id, exc)
            await asyncio.sleep(0.05)

        return total, sent, failed

    async def _send_broadcast_message(
        self,
        chat_id: int,
        text: str,
        photo_source: str,
        reply_markup=None,
    ) -> None:
        safe_text = html.escape(text)
        parse_mode_error_markers = (
            "can't parse entities",
            "entity beginning",
            "unsupported start tag",
            "entity end",
        )

        if photo_source:
            try:
                await self.bot.send_photo(
                    chat_id=chat_id,
                    photo=self._photo_input(photo_source),
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
                return
            except TelegramBadRequest as exc:
                lowered = str(exc).lower()
                if any(marker in lowered for marker in parse_mode_error_markers):
                    try:
                        await self.bot.send_photo(
                            chat_id=chat_id,
                            photo=self._photo_input(photo_source),
                            caption=safe_text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup,
                        )
                        return
                    except TelegramBadRequest as escaped_exc:
                        logger.warning(
                            "Broadcast photo to user %s failed after caption escaping: %s",
                            chat_id,
                            escaped_exc,
                        )
                else:
                    logger.warning(
                        "Broadcast photo to user %s failed, fallback to text: %s",
                        chat_id,
                        exc,
                    )

        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except TelegramBadRequest as exc:
            lowered = str(exc).lower()
            if not any(marker in lowered for marker in parse_mode_error_markers):
                raise
            await self.bot.send_message(
                chat_id=chat_id,
                text=safe_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )

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

    def _render_admin_html(
        self,
        saved: bool = False,
        unpaid_count: int = 0,
        admin_users: list | None = None,
        mailing_message: str = "",
        mailing_message_class: str = "",
    ) -> str:
        dynamic = self.content_store.get_content()
        nl = chr(10)
        if admin_users is None:
            admin_users = []

        def esc(value: str) -> str:
            return html.escape(value, quote=True)

        user_options = ["<option value=''>Выбери пользователя</option>"]
        for user in admin_users:
            username = f"@{user.username}" if user.username else ""
            identity = " ".join(part for part in (user.first_name, username) if part).strip()
            if not identity:
                identity = "без имени"
            paid_state = "оплачен" if user.is_paid else "не оплачен"
            user_options.append(
                f"<option value='{user.user_id}'>{user.user_id} — {esc(identity)} ({paid_state})</option>"
            )
        user_options_html = "".join(user_options)

        lesson_blocks: list[str] = []
        for idx, lesson in enumerate(dynamic.lessons):
            lesson_photo_urls = esc(nl.join(lesson.photos))
            lesson_blocks.append(
                f"""
                <section class="card lesson-card">
                  <div class="card-head">
                    <h2>Сообщение 3.{idx + 1}</h2>
                    <span class="pill">Блок урока</span>
                  </div>
                  <label>Текст</label>
                  <textarea name="lesson_{idx}_text" rows="7">{esc(lesson.text)}</textarea>
                  <label>Фото/ссылки (по одной строке)</label>
                  <textarea name="lesson_{idx}_photo_urls" rows="4">{lesson_photo_urls}</textarea>
                  <label>Загрузить фото файлами (добавятся к списку)</label>
                  <input type="file" name="lesson_{idx}_photo_files" multiple accept="image/*" />
                  <label>Задержка печати перед этим блоком (сек)</label>
                  <input type="number" min="0" name="lesson_{idx}_typing" value="{lesson.typing_before_seconds}" />
                </section>
                """
            )

        saved_banner = "<div class='ok'>Сохранено.</div>" if saved else ""
        mailing_banner = (
            f"<div class='{mailing_message_class}'>{esc(mailing_message)}</div>"
            if mailing_message and mailing_message_class in {"ok", "warn"}
            else ""
        )
        return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Milana Bot Admin</title>
  <style>
    :root {{
      --bg: #070b12;
      --bg-2: #0d1320;
      --panel: #121b29;
      --panel-2: #182437;
      --line: #2b3b56;
      --text: #e8f0ff;
      --muted: #98abc9;
      --accent: #4fd6a5;
      --accent-2: #ff9f5b;
      --focus: #7fdaff;
      --input: #0f1724;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      font-family: "Manrope", "Segoe UI", sans-serif;
      background:
        radial-gradient(900px 400px at 100% -20%, #14304e66, transparent 70%),
        radial-gradient(800px 320px at -10% 10%, #1f483866, transparent 70%),
        linear-gradient(180deg, var(--bg), var(--bg-2));
      min-height: 100vh;
    }}
    .wrap {{
      max-width: 1120px;
      margin: 28px auto;
      padding: 0 16px 40px;
    }}
    .top {{
      position: sticky;
      top: 12px;
      z-index: 10;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 16px;
      border: 1px solid var(--line);
      background: color-mix(in srgb, var(--panel-2) 90%, transparent);
      backdrop-filter: blur(6px);
      border-radius: 14px;
      margin-bottom: 14px;
    }}
    h1 {{
      margin: 0;
      font-size: 24px;
      line-height: 1.2;
      letter-spacing: 0.2px;
    }}
    .hint {{
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .ok {{
      margin: 12px 0;
      border: 1px solid #2f7059;
      background: #123a2c;
      color: #b7f2dc;
      padding: 10px 12px;
      border-radius: 10px;
      font-size: 14px;
    }}
    .warn {{
      margin: 12px 0;
      border: 1px solid #7e5d33;
      background: #3a2913;
      color: #ffd9ad;
      padding: 10px 12px;
      border-radius: 10px;
      font-size: 14px;
    }}
    .card {{
      margin-top: 16px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: linear-gradient(180deg, var(--panel), #101827);
      padding: 16px;
      box-shadow: 0 8px 28px #0000002e;
    }}
    .card-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 6px;
    }}
    h2 {{
      margin: 0;
      font-size: 18px;
      font-weight: 700;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid #3b536f;
      color: #b4c9e6;
      font-size: 12px;
      background: #111c2d;
    }}
    label {{
      display: block;
      margin: 10px 0 6px;
      color: var(--muted);
      font-size: 13px;
      letter-spacing: 0.1px;
    }}
    textarea, input[type="number"], input[type="text"], input[type="file"], select {{
      width: 100%;
    }}
    textarea,
    input[type="number"],
    input[type="text"],
    select {{
      border: 1px solid #354867;
      border-radius: 10px;
      padding: 10px 12px;
      background: var(--input);
      color: var(--text);
      outline: none;
      transition: border-color .15s ease, box-shadow .15s ease;
    }}
    textarea {{ resize: vertical; min-height: 110px; }}
    textarea:focus,
    input[type="number"]:focus,
    input[type="text"]:focus,
    select:focus {{
      border-color: var(--focus);
      box-shadow: 0 0 0 3px #7fdaff22;
    }}
    select {{
      appearance: none;
      background-image:
        linear-gradient(45deg, transparent 50%, #9db9df 50%),
        linear-gradient(135deg, #9db9df 50%, transparent 50%);
      background-position:
        calc(100% - 18px) calc(50% + 1px),
        calc(100% - 12px) calc(50% + 1px);
      background-size: 6px 6px, 6px 6px;
      background-repeat: no-repeat;
      padding-right: 34px;
    }}
    input[type="file"] {{
      color: var(--muted);
      border: 1px dashed #395271;
      border-radius: 10px;
      padding: 8px;
      background: #0d1522;
    }}
    input[type="file"]::file-selector-button {{
      margin-right: 10px;
      border: 0;
      border-radius: 8px;
      padding: 7px 12px;
      font-weight: 600;
      background: #22314b;
      color: #d3e6ff;
      cursor: pointer;
    }}
    .actions {{
      display: flex;
      justify-content: flex-end;
      margin-top: 20px;
      margin-bottom: 10px;
    }}
    .broadcast-actions {{
      display: flex;
      justify-content: flex-start;
      margin-top: 16px;
      margin-bottom: 0;
    }}
    .broadcast-meta {{
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .field-hint {{
      margin: 6px 0 0;
      color: #7f95b9;
      font-size: 12px;
      line-height: 1.45;
    }}
    .check {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 12px;
      color: var(--text);
      font-size: 14px;
    }}
    .check input[type="checkbox"] {{
      width: 16px;
      height: 16px;
      margin: 0;
    }}
    button {{
      border: none;
      border-radius: 11px;
      padding: 12px 18px;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      color: #072013;
      background: linear-gradient(135deg, var(--accent), #88f5cc);
      transition: transform .08s ease, filter .15s ease;
    }}
    button:hover {{ filter: brightness(1.05); }}
    button:active {{ transform: translateY(1px); }}
    .ghost {{
      color: #fff7ef;
      background: linear-gradient(135deg, #3a4d69, #2c3d56);
      border: 1px solid #51698c;
    }}
    @media (max-width: 760px) {{
      .top {{
        position: static;
        flex-direction: column;
        align-items: flex-start;
      }}
      h1 {{ font-size: 22px; }}
      .actions {{ justify-content: stretch; }}
      .actions button {{ width: 100%; }}
      .broadcast-actions {{ justify-content: stretch; }}
      .broadcast-actions button {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header class="top">
      <div>
        <h1>Milana Bot Admin</h1>
        <p class="hint">Редактируй тексты и фото. Для фото можно указывать ссылки или загружать файлы.</p>
      </div>
      <button class="ghost" type="submit" form="content-form">Сохранить изменения</button>
    </header>
    {saved_banner}
    {mailing_banner}

    <section class="card">
      <div class="card-head">
        <h2>Рассылка неоплатившим</h2>
        <span class="pill">CRM</span>
      </div>
      <p class="broadcast-meta">
        Сообщение отправится всем пользователям с признаком <code>is_paid = 0</code>.
        Сейчас в базе неоплативших: <strong>{unpaid_count}</strong>.
      </p>
      <form id="broadcast-form" method="post" enctype="multipart/form-data">
        <input type="hidden" name="action" value="broadcast_unpaid" />
        <label>Текст рассылки</label>
        <textarea
          name="broadcast_text"
          rows="6"
          placeholder="Например: До конца недели действует спеццена на курс..."
          required
        ></textarea>
        <label>Кому отправить</label>
        <select name="broadcast_target_mode" id="broadcast_target_mode">
          <option value="unpaid" selected>Всем неоплатившим</option>
          <option value="single">Одному пользователю</option>
        </select>
        <label>Пользователь для точечной отправки</label>
        <select name="broadcast_target_user_id" id="broadcast_target_user_id" disabled>
          {user_options_html}
        </select>
        <p class="field-hint">Список берется из пользователей, которые уже запускали бота.</p>
        <label>Фото к рассылке (опционально, 1 файл)</label>
        <input type="file" name="broadcast_photo_file" accept="image/*" />
        <p class="field-hint">Если выбрать фото, текст уйдет как подпись к изображению.</p>
        <label class="check">
          <input type="checkbox" name="broadcast_with_pay_button" checked />
          Добавить кнопку «Оплатить»
        </label>
        <div class="broadcast-actions">
          <button type="submit" class="ghost">Отправить рассылку</button>
        </div>
      </form>
    </section>

    <form id="content-form" method="post" enctype="multipart/form-data">
      <input type="hidden" name="action" value="save_content" />
      <section class="card">
        <div class="card-head">
          <h2>Продукт и цена</h2>
          <span class="pill">Оплата</span>
        </div>
        <label>Стоимость продукта, ₽</label>
        <input type="number" min="1" name="course_price_rub" value="{dynamic.course_price_rub}" />
      </section>

      <section class="card">
        <div class="card-head">
          <h2>Сообщение 1 (Start)</h2>
          <span class="pill">Приветствие</span>
        </div>
        <label>Текст</label>
        <textarea name="start_text" rows="9">{esc(dynamic.start_text)}</textarea>
        <label>Фото/ссылки (по одной строке)</label>
        <textarea name="start_photo_urls" rows="3">{esc(nl.join(dynamic.start_photos))}</textarea>
        <label>Загрузить фото файлами (добавятся к списку)</label>
        <input type="file" name="start_photo_files" multiple accept="image/*" />
      </section>

      <section class="card">
        <div class="card-head">
          <h2>Сообщение 2 (Подробнее)</h2>
          <span class="pill">Описание курса</span>
        </div>
        <label>Текст</label>
        <textarea name="course_overview_text" rows="14">{esc(dynamic.course_overview_text)}</textarea>
      </section>

      {"".join(lesson_blocks)}

      <div class="actions">
        <button type="submit">Сохранить изменения</button>
      </div>
    </form>
  </div>
  <script>
    (() => {{
      const modeSelect = document.getElementById("broadcast_target_mode");
      const userSelect = document.getElementById("broadcast_target_user_id");
      if (!modeSelect || !userSelect) {{
        return;
      }}
      const syncUserTargetState = () => {{
        const singleMode = modeSelect.value === "single";
        userSelect.disabled = !singleMode;
        userSelect.required = singleMode;
      }};
      modeSelect.addEventListener("change", syncUserTargetState);
      syncUserTargetState();
    }})();
  </script>
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
