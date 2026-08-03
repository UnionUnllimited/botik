"""Отправка сообщений из процессов без диспетчера (api, worker).

Бот здесь создаётся по требованию и закрывается вместе с процессом.
Все ошибки Telegram обрабатываются: заблокировавшего бота пользователя
помечаем в БД, RetryAfter выжидаем.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.models import User

log = structlog.get_logger("notifications")

_bot: Bot | None = None


def get_bot() -> Bot:
    global _bot  # noqa: PLW0603 — один клиент на процесс
    if _bot is None:
        _bot = Bot(
            token=settings.bot.token.get_secret_value(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
        )
    return _bot


async def close_bot() -> None:
    global _bot  # noqa: PLW0603
    if _bot is not None:
        await _bot.session.close()
    _bot = None


async def send_message(
    chat_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    session: AsyncSession | None = None,
    retries: int = 2,
) -> bool:
    """Отправляет сообщение. False — доставить не удалось."""
    if not chat_id:
        return False
    bot = get_bot()
    for attempt in range(retries + 1):
        try:
            await bot.send_message(chat_id, text, reply_markup=reply_markup)
        except TelegramForbiddenError:
            log.info("notify.bot_blocked", chat_id=chat_id)
            if session is not None:
                await session.execute(
                    update(User)
                    .where(User.tg_id == chat_id, User.bot_blocked.is_(False))
                    .values(bot_blocked=True, bot_blocked_at=dt.datetime.now(dt.UTC))
                )
            return False
        except TelegramRetryAfter as exc:
            log.warning("notify.retry_after", chat_id=chat_id, seconds=exc.retry_after)
            if attempt >= retries:
                return False
            await asyncio.sleep(exc.retry_after)
        except Exception as exc:  # noqa: BLE001 — уведомление не должно ронять транзакцию
            log.warning("notify.failed", chat_id=chat_id, error=str(exc))
            return False
        else:
            return True
    return False


async def notify_admins(text: str, *, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Служебное сообщение в админ-канал; если он не задан — владельцу."""
    chat_id = settings.bot.alerts_chat_id or settings.bot.owner_id
    if not chat_id:
        log.warning("notify.no_admin_chat", text=text[:120])
        return
    await send_message(chat_id, text, reply_markup=reply_markup)
