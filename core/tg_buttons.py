"""Кнопки под уведомлениями, которые шлёт не бот.

Только ссылки, ни одной callback-кнопки. Callback обрабатывает конкретный бот,
а он у нас сменный: кнопка, чей обработчик уехал вместе с прошлым ботом, молча
перестала бы работать — клиент нажимает, и ничего не происходит. Ссылка ведёт
на сайт и переживает любую замену бота.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.texts import TRACK_LINK


def tracking(url: str | None) -> InlineKeyboardMarkup | None:
    """Кнопка отслеживания. Без ссылки кнопки нет — пустая вела бы в никуда."""
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=TRACK_LINK, url=url)]])
