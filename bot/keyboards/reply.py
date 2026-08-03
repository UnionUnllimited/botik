"""Reply-клавиатуры.

Главное меню живёт в инлайн-кнопках под сообщением (bot/keyboards/inline.py) —
так оно не занимает место внизу экрана и остаётся привязанным к контексту.
Reply-клавиатура нужна только там, где Telegram иначе не умеет: запрос контакта.
"""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

from bot.texts import ru

REMOVE = ReplyKeyboardRemove()


def request_phone() -> ReplyKeyboardMarkup:
    """Кнопка «Отправить номер» — единственный способ получить контакт одним тапом."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=ru.BTN_SHARE_PHONE, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
