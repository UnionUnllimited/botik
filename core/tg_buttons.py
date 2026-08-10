"""Кнопки под уведомлениями, которые шлёт не бот.

Только ссылки, ни одной callback-кнопки. Callback обрабатывает конкретный бот,
а он у нас сменный: кнопка, чей обработчик уехал вместе с прошлым ботом, молча
перестала бы работать — клиент нажимает, и ничего не происходит. Ссылка ведёт
на сайт и переживает любую замену бота.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.config import settings
from core.texts import SUBSCRIPTION_OPEN, TRACK_LINK


def tracking(url: str | None) -> InlineKeyboardMarkup | None:
    """Кнопка отслеживания. Без ссылки кнопки нет — пустая вела бы в никуда."""
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=TRACK_LINK, url=url)]])


def cabinet() -> InlineKeyboardMarkup | None:
    """Кнопка в личный кабинет — там продление и состояние подписки."""
    base = settings.api.public_base_url.rstrip("/")
    if not base.startswith("https://"):
        # Telegram принимает в кнопках только http(s), а localhost из dev-конфига
        # ещё и бесполезен клиенту. Лучше без кнопки, чем с нерабочей.
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=SUBSCRIPTION_OPEN, url=f"{base}/cabinet")]]
    )
