from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Оплатить", callback_data="pay")
            ],
            [
                InlineKeyboardButton(text="Подробнее", callback_data="details"),
            ]
        ]
    )


def pay_only_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Оплатить", callback_data="pay")]]
    )


def details_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить", callback_data="pay")],
            [InlineKeyboardButton(text="Что тебя ждёт?", callback_data="what_to_expect")],
        ]
    )


def payment_link_keyboard(payment_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить", url=payment_url)],
            [InlineKeyboardButton(text="Я оплатила", callback_data="paid_request")],
        ]
    )
