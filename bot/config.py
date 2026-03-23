from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


def _parse_int_list(raw_value: str) -> set[int]:
    result: set[int] = set()
    for part in raw_value.split(","):
        part = part.strip()
        if not part:
            continue
        result.add(int(part))
    return result


def _parse_optional_int(raw_value: str) -> int | None:
    value = raw_value.strip()
    if not value:
        return None
    return int(value)


def _parse_urls(raw_value: str) -> tuple[str, ...]:
    items: list[str] = []
    for part in raw_value.split(","):
        part = part.strip()
        if part:
            items.append(part)
    return tuple(items)


def _parse_int(raw_value: str, default: int) -> int:
    value = raw_value.strip()
    if not value:
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    payment_url: str
    payment_provider_token: str
    tbank_terminal_key: str
    tbank_password: str
    tbank_api_url: str
    tbank_notification_url: str
    tbank_success_url: str
    tbank_fail_url: str
    tbank_order_description: str
    webhook_host: str
    webhook_port: int
    webhook_path: str
    offer_url: str
    privacy_url: str
    support_contact: str
    course_chat_id: int | None
    course_channel_id: int | None
    admin_ids: set[int]
    welcome_photo_url: str
    promo_photo_urls: tuple[str, ...]
    course_price_rub: int
    timezone: str
    campaign_year: int
    db_path: str

    @property
    def tbank_enabled(self) -> bool:
        return bool(self.tbank_terminal_key and self.tbank_password)

    @classmethod
    def from_env(cls) -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if not bot_token:
            raise ValueError("Переменная BOT_TOKEN обязательна.")

        timezone = os.getenv("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"
        default_year = datetime.now(ZoneInfo(timezone)).year

        return cls(
            bot_token=bot_token,
            payment_url=os.getenv("PAYMENT_URL", "").strip(),
            payment_provider_token=os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip(),
            tbank_terminal_key=os.getenv("TBANK_TERMINAL_KEY", "").strip(),
            tbank_password=os.getenv("TBANK_PASSWORD", "").strip(),
            tbank_api_url=os.getenv("TBANK_API_URL", "https://securepay.tinkoff.ru/v2").strip()
            or "https://securepay.tinkoff.ru/v2",
            tbank_notification_url=os.getenv("TBANK_NOTIFICATION_URL", "").strip(),
            tbank_success_url=os.getenv("TBANK_SUCCESS_URL", "").strip(),
            tbank_fail_url=os.getenv("TBANK_FAIL_URL", "").strip(),
            tbank_order_description=(
                os.getenv(
                    "TBANK_ORDER_DESCRIPTION",
                    "Пакет уроков «Искусство быть красивой»",
                ).strip()
                or "Пакет уроков «Искусство быть красивой»"
            ),
            webhook_host=os.getenv("WEBHOOK_HOST", "0.0.0.0").strip() or "0.0.0.0",
            webhook_port=_parse_int(os.getenv("WEBHOOK_PORT", "8080"), 8080),
            webhook_path=os.getenv("WEBHOOK_PATH", "/tbank/notification").strip() or "/tbank/notification",
            offer_url=os.getenv("OFFER_URL", "").strip(),
            privacy_url=os.getenv("PRIVACY_URL", "").strip(),
            support_contact=os.getenv("SUPPORT_CONTACT", "@beautymi30").strip() or "@beautymi30",
            course_chat_id=_parse_optional_int(os.getenv("COURSE_CHAT_ID", "")),
            course_channel_id=_parse_optional_int(os.getenv("COURSE_CHANNEL_ID", "")),
            admin_ids=_parse_int_list(os.getenv("ADMIN_IDS", "")),
            welcome_photo_url=os.getenv("WELCOME_PHOTO_URL", "").strip(),
            promo_photo_urls=_parse_urls(os.getenv("PROMO_PHOTO_URLS", "")),
            course_price_rub=int(os.getenv("COURSE_PRICE_RUB", "2999")),
            timezone=timezone,
            campaign_year=int(os.getenv("CAMPAIGN_YEAR", str(default_year))),
            db_path=os.getenv("DB_PATH", "bot_data.sqlite3").strip() or "bot_data.sqlite3",
        )
