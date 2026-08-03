"""Последний роутер в цепочке: всё, что не разобрали хендлеры выше."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import Message

from bot.keyboards.reply import main_menu
from bot.texts import ru

router = Router(name="fallback")


@router.message()
async def unknown_message(message: Message) -> None:
    await message.answer(ru.FALLBACK, reply_markup=main_menu())
