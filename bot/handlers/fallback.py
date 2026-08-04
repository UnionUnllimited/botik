"""Последний роутер в цепочке: всё, что не разобрали хендлеры выше."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import Message

from bot.keyboards import inline
from bot.texts import ru
from bot.utils import screen

router = Router(name="fallback")


@router.message()
async def unknown_message(message: Message) -> None:
    """Непонятое сообщение убираем вместе с ответом — оно ничего не значит."""
    await screen.show(message, ru.FALLBACK, markup=inline.main_menu())
