from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="ОПЛАТИТЬ", callback_data="pay"),
                InlineKeyboardButton(text="ПОДРОБНЕЕ", callback_data="details"),
            ]
        ]
    )


def pay_only_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="ОПЛАТИТЬ", callback_data="pay")]]
    )


def payment_link_keyboard(payment_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="ОПЛАТИТЬ", url=payment_url)],
            [InlineKeyboardButton(text="Я ОПЛАТИЛА", callback_data="paid_request")],
        ]
    )
