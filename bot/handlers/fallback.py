"""Последний роутер в цепочке: всё, что не разобрали хендлеры выше."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import Message

from bot.keyboards import inline
from bot.texts import ru

router = Router(name="fallback")


@router.message()
async def unknown_message(message: Message) -> None:
    await message.answer(ru.FALLBACK, reply_markup=inline.main_menu())
