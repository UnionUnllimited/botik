"""Зависимости FastAPI: сессия БД, redis, реальный IP клиента."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.db import get_sessionmaker
from core.redis_client import get_redis


async def get_session() -> AsyncIterator[AsyncSession]:
    """Сессия на запрос: commit делает сам обработчик, здесь — только rollback и закрытие."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_transaction() -> AsyncIterator[AsyncSession]:
    """Сессия с автокоммитом — для обработчиков, меняющих данные одной транзакцией."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_redis_client() -> Redis:
    return get_redis()


def client_ip(request: Request) -> str:
    """Реальный IP с учётом того, сколько прокси стоит перед приложением."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        chain = [part.strip() for part in forwarded.split(",") if part.strip()]
        hops = max(settings.api.trusted_proxy_hops, 1)
        if len(chain) >= hops:
            return chain[-hops]
        return chain[0]
    return request.client.host if request.client else "unknown"
