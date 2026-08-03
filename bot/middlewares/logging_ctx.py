"""Привязка контекста к логам: все записи внутри апдейта получают update_id и tg_id."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from core.metrics import bot_updates_total


class LoggingContextMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        structlog.contextvars.clear_contextvars()
        if isinstance(event, Update):
            structlog.contextvars.bind_contextvars(update_id=event.update_id)
            bot_updates_total.labels(type=event.event_type).inc()
        tg_user = data.get("event_from_user")
        if tg_user is not None:
            structlog.contextvars.bind_contextvars(tg_id=tg_user.id)
        try:
            return await handler(event, data)
        finally:
            structlog.contextvars.clear_contextvars()
