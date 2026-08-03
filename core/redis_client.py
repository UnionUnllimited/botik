"""Общий Redis: FSM бота, кэш, rate-limit, anti-replay nonce, очередь рассылок."""

from __future__ import annotations

import asyncio

from redis.asyncio import ConnectionPool, Redis

from core.config import settings

_pool: ConnectionPool | None = None
_client: Redis | None = None


def get_redis() -> Redis:
    global _pool, _client  # noqa: PLW0603 — один пул на процесс
    if _client is None:
        _pool = ConnectionPool.from_url(
            settings.redis.url,
            max_connections=settings.redis.max_connections,
            socket_timeout=settings.redis.socket_timeout,
            socket_connect_timeout=settings.redis.socket_timeout,
            health_check_interval=30,
            decode_responses=True,
        )
        _client = Redis(connection_pool=_pool)
    return _client


async def check_redis(*, timeout: float = 3.0) -> bool:  # noqa: ASYNC109 — проба здоровья
    try:
        return bool(await asyncio.wait_for(get_redis().ping(), timeout=timeout))
    except (TimeoutError, Exception):  # noqa: BLE001 — healthcheck
        return False


async def close_redis() -> None:
    global _pool, _client  # noqa: PLW0603
    if _client is not None:
        await _client.aclose()
    if _pool is not None:
        await _pool.aclose()
    _client = None
    _pool = None


class RateLimiter:
    """Счётчик с окном фиксированной длины (INCR + EXPIRE).

    Достаточно для защиты API устройств и логина админки; распределённый,
    переживает рестарт процессов, не требует отдельной инфраструктуры.
    """

    def __init__(self, redis: Redis | None = None) -> None:
        self._redis = redis or get_redis()

    async def hit(self, bucket: str, *, limit: int, window_sec: int) -> tuple[bool, int]:
        """Возвращает (разрешено, сколько осталось попыток)."""
        key = settings.redis.key("rl", bucket)
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_sec, nx=True)
        current, _ = await pipe.execute()
        remaining = max(limit - int(current), 0)
        return int(current) <= limit, remaining

    async def reset(self, bucket: str) -> None:
        await self._redis.delete(settings.redis.key("rl", bucket))


class NonceStore:
    """Anti-replay для подписанных запросов устройств."""

    def __init__(self, redis: Redis | None = None) -> None:
        self._redis = redis or get_redis()

    async def claim(self, device_id: str, nonce: str) -> bool:
        """True — nonce увиден впервые; False — повтор, запрос надо отклонить."""
        key = settings.redis.key("nonce", device_id, nonce)
        stored = await self._redis.set(key, "1", ex=settings.security.device_nonce_ttl_sec, nx=True)
        return bool(stored)
