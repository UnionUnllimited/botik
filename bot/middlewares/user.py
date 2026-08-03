"""Пользователь: заводим при первом апдейте, обновляем профиль, отсекаем забаненных."""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.texts import ru
from core.config import settings
from core.models import User

log = structlog.get_logger("bot.user")

_PROFILE_REFRESH = dt.timedelta(hours=6)


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        session: AsyncSession = data["session"]
        user = await session.scalar(select(User).where(User.tg_id == tg_user.id))
        now = dt.datetime.now(dt.UTC)

        if user is None:
            user = User(
                tg_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                last_name=tg_user.last_name,
                language_code=(tg_user.language_code or "ru")[:8],
                last_seen_at=now,
            )
            session.add(user)
            await session.flush()
            data["is_new_user"] = True
            log.info("user.created", user_id=user.id, tg_id=user.tg_id)
        else:
            data["is_new_user"] = False
            if user.bot_blocked:
                # Пользователь вернулся — снимаем отметку, снова доступен для рассылок.
                user.bot_blocked = False
                user.bot_blocked_at = None
            stale = user.last_seen_at is None or (now - user.last_seen_at) > _PROFILE_REFRESH
            if stale or user.username != tg_user.username:
                user.username = tg_user.username
                user.first_name = tg_user.first_name
                user.last_name = tg_user.last_name
            user.last_seen_at = now

        data["user"] = user
        data["is_admin"] = settings.bot.is_admin(tg_user.id)

        if user.is_blocked and not data["is_admin"]:
            log.warning("user.blocked_access", user_id=user.id)
            await self._notify_blocked(event)
            return None

        return await handler(event, data)

    @staticmethod
    async def _notify_blocked(event: TelegramObject) -> None:
        from aiogram.types import CallbackQuery, Message

        contact = f"@{settings.app.bot_username}" if settings.app.bot_username else "поддержку"
        text = ru.BLOCKED_USER.format(contact=contact)
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
