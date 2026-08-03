"""Антифлуд: не даём одному пользователю завалить бота сообщениями."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.texts import ru
from core.redis_client import RateLimiter, get_redis

log = structlog.get_logger("bot.throttling")


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, *, limit: int = 20, window_sec: int = 10) -> None:
        self.limit = limit
        self.window_sec = window_sec

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or data.get("is_admin"):
            return await handler(event, data)

        limiter = RateLimiter()
        allowed, _ = await limiter.hit(f"tg:{tg_user.id}", limit=self.limit, window_sec=self.window_sec)
        if allowed:
            return await handler(event, data)

        log.warning("bot.throttled", tg_id=tg_user.id)
        # Предупреждаем один раз на окно, иначе сами себе устроим флуд.
        redis = get_redis()
        notified = await redis.set(f"throttle:notified:{tg_user.id}", "1", ex=self.window_sec, nx=True)
        if notified:
            if isinstance(event, Message):
                await event.answer(ru.THROTTLED)
            elif isinstance(event, CallbackQuery):
                await event.answer(ru.THROTTLED, show_alert=False)
        return None
