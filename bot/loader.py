"""Сборка Bot и Dispatcher: FSM в Redis, middlewares, роутеры."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage

from bot.handlers import build_router
from bot.middlewares import (
    DatabaseMiddleware,
    LoggingContextMiddleware,
    ThrottlingMiddleware,
    UserMiddleware,
)
from core.config import settings
from core.redis_client import get_redis


def create_bot() -> Bot:
    return Bot(
        token=settings.bot.token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def create_storage() -> RedisStorage:
    return RedisStorage(
        redis=get_redis(),
        key_builder=DefaultKeyBuilder(prefix=settings.redis.key("fsm"), with_bot_id=True),
        state_ttl=None,
        data_ttl=None,
    )


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=create_storage())

    # Порядок outer-middleware: контекст логов → антифлуд → сессия БД → пользователь.
    # Антифлуд стоит перед сессией намеренно: иначе на каждый мусорный апдейт
    # открывалась бы транзакция, и флуд выедал бы пул соединений к Postgres
    # раньше, чем срабатывал лимит.
    dp.update.outer_middleware(LoggingContextMiddleware())
    dp.update.outer_middleware(ThrottlingMiddleware())
    dp.update.outer_middleware(DatabaseMiddleware())
    dp.update.outer_middleware(UserMiddleware())

    dp.include_router(build_router())
    return dp
