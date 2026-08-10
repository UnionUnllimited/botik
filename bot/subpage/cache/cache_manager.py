"""
Менеджер кэша — Redis only.
"""
import logging
from typing import Optional, Dict, Any

from .redis_cache import (
    init_redis as init_redis_cache,
    close_redis as close_redis_cache,
    get_subscription_cache as get_redis_cache,
    save_subscription_cache as save_redis_cache,
    delete_subscription_cache as delete_redis_cache,
    is_available as redis_is_available,
    get_cache_stats as get_redis_stats,
    cleanup_expired_cache as cleanup_redis_cache
)

logger = logging.getLogger(__name__)

_use_redis = True
_cache_ttl = 300  # секунд


async def init_cache_manager(
    use_redis: bool = True,
    redis_host: str = "127.0.0.1",
    redis_port: int = 6379,
    redis_db: int = 0,
    redis_password: Optional[str] = None,
    cache_ttl: int = 300,
    **_kwargs  # поглощаем устаревшие параметры (redis_fallback_to_sqlite и т.д.)
):
    """Инициализация менеджера кэша (Redis)."""
    global _use_redis, _cache_ttl

    _use_redis = use_redis
    _cache_ttl = cache_ttl

    if _use_redis:
        available = await init_redis_cache(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            password=redis_password
        )
        if available:
            logger.info("✅ Redis кэш инициализирован и доступен")
        else:
            logger.warning("⚠️  Redis недоступен — кэш не будет работать")
    else:
        logger.info("Redis отключён (USE_REDIS=false)")


async def close_cache_manager():
    """Закрытие подключений."""
    if _use_redis:
        await close_redis_cache()


async def get_subscription_cache(
    uuid: str,
    use_stale: bool = False,
    use_backup: bool = False,   # оставлен для совместимости вызовов, поведение = use_stale
) -> Optional[Dict[str, Any]]:
    """
    Получение данных подписки из Redis-кэша.

    use_stale=True — разрешить возврат данных с истёкшим TTL
                     (когда API недоступно).
    """
    if not _use_redis:
        return None

    if not (await redis_is_available()):
        return None

    cached = await get_redis_cache(uuid)
    if cached:
        logger.debug(f"✅ Данные {uuid} получены из Redis кэша")
        return cached

    # Redis доступен, но TTL истёк
    if use_stale or use_backup:
        # Redis TTL уже истёк, данных нет — возвращаем None
        # (get_redis_cache возвращает None при истёкшем TTL)
        logger.debug(f"ℹ️  Redis TTL истёк для {uuid} (use_stale={use_stale}), данных нет")
    return None


async def save_subscription_cache(uuid: str, sub_data: Dict[str, Any]):
    """Сохранение данных подписки в Redis."""
    if not _use_redis:
        return False

    is_found = sub_data.get('isFound', False)
    ttl = _cache_ttl if is_found else 60  # 1 мин для несуществующих UUID

    if await redis_is_available():
        try:
            await save_redis_cache(uuid, sub_data, ttl=ttl)
            logger.info(f"✅ Сохранено в Redis для {uuid} с TTL={ttl}с (isFound={is_found})")
            return True
        except Exception as e:
            logger.warning(f"Не удалось сохранить в Redis: {e}")

    return False


async def delete_subscription_cache(uuid: str):
    """Удаление данных подписки из Redis."""
    if _use_redis and await redis_is_available():
        try:
            await delete_redis_cache(uuid)
            return True
        except Exception as e:
            logger.warning(f"Не удалось удалить из Redis: {e}")
    return False


async def cleanup_expired_cache():
    """Очистка истёкших записей (Redis управляет TTL автоматически)."""
    if _use_redis and await redis_is_available():
        await cleanup_redis_cache()


async def get_cache_stats() -> Dict[str, Any]:
    """Статистика кэша."""
    stats: Dict[str, Any] = {'redis': {}, 'primary': 'none'}
    if _use_redis and await redis_is_available():
        stats['redis'] = await get_redis_stats()
        stats['primary'] = 'redis'
    return stats
