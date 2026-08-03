"""Асинхронный доступ к PostgreSQL: engine, фабрика сессий, healthcheck."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine  # noqa: PLW0603 — один engine на процесс
    if _engine is None:
        _engine = create_async_engine(
            settings.db.async_dsn,
            echo=settings.log.sql_echo,
            pool_size=settings.db.pool_size,
            max_overflow=settings.db.max_overflow,
            pool_timeout=settings.db.pool_timeout,
            pool_recycle=settings.db.pool_recycle,
            pool_pre_ping=True,
            connect_args={
                "server_settings": {
                    "application_name": "router-shop",
                    "timezone": "UTC",
                    "statement_timeout": str(settings.db.statement_timeout_ms),
                },
            },
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker  # noqa: PLW0603
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Транзакционная сессия: commit при успехе, rollback при исключении."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_database(*, timeout: float = 5.0) -> bool:  # noqa: ASYNC109 — проба здоровья, таймаут задаём явно
    """Быстрая проверка для healthcheck: недоступная база не должна вешать пробу."""

    async def _ping() -> None:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(_ping(), timeout=timeout)
    except (TimeoutError, Exception):  # noqa: BLE001 — healthcheck не должен ронять процесс
        return False
    return True


async def dispose_engine() -> None:
    global _engine, _sessionmaker  # noqa: PLW0603
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
