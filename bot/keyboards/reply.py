"""Reply-клавиатуры бота."""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

from bot.texts import ru

REMOVE = ReplyKeyboardRemove()


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ru.BTN_BUY), KeyboardButton(text=ru.BTN_MY_DEVICE)],
            [KeyboardButton(text=ru.BTN_SUBSCRIPTION), KeyboardButton(text=ru.BTN_GUIDES)],
            [KeyboardButton(text=ru.BTN_SUPPORT), KeyboardButton(text=ru.BTN_REFERRAL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
