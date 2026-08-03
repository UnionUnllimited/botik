"""Единый обработчик ошибок бота.

Отдельно разбираем случаи Telegram API: блокировку бота пользователем помечаем
в БД (чтобы не слать ему рассылки), RetryAfter не считаем ошибкой приложения.
"""

from __future__ import annotations

import datetime as dt

import structlog
from aiogram import Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import CallbackQuery, ErrorEvent, Message
from sqlalchemy import update

from bot.texts import ru
from core.db import session_scope
from core.metrics import bot_errors_total
from core.models import User

router = Router(name="errors")
log = structlog.get_logger("bot.errors")


async def _mark_bot_blocked(tg_id: int) -> None:
    async with session_scope() as session:
        await session.execute(
            update(User)
            .where(User.tg_id == tg_id, User.bot_blocked.is_(False))
            .values(bot_blocked=True, bot_blocked_at=dt.datetime.now(dt.UTC))
        )


@router.errors()
async def handle_error(event: ErrorEvent) -> bool:
    exception = event.exception
    update_obj = event.update
    tg_user = update_obj.event.from_user if hasattr(update_obj.event, "from_user") else None

    if isinstance(exception, TelegramForbiddenError):
        bot_errors_total.labels(kind="forbidden").inc()
        if tg_user is not None:
            await _mark_bot_blocked(tg_user.id)
            log.info("bot.blocked_by_user", tg_id=tg_user.id)
        return True

    if isinstance(exception, TelegramRetryAfter):
        bot_errors_total.labels(kind="retry_after").inc()
        log.warning("telegram.retry_after", seconds=exception.retry_after)
        return True

    if isinstance(exception, TelegramBadRequest) and "message is not modified" in str(exception):
        bot_errors_total.labels(kind="not_modified").inc()
        return True

    bot_errors_total.labels(kind=type(exception).__name__).inc()
    log.exception("bot.unhandled_error", error=str(exception))

    target = update_obj.message or (update_obj.callback_query.message if update_obj.callback_query else None)
    try:
        if isinstance(update_obj.callback_query, CallbackQuery):
            await update_obj.callback_query.answer(ru.ERROR_GENERIC[:200], show_alert=True)
        elif isinstance(target, Message):
            await target.answer(ru.ERROR_GENERIC)
    except Exception as notify_error:  # noqa: BLE001 — не даём ошибке уведомления перекрыть исходную
        log.warning("bot.error_notify_failed", error=str(notify_error))
    return True
