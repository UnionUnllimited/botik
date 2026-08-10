"""
Дебаунсинг запросов - предотвращает множественные запросы к API
для одного UUID в течение короткого времени
"""
import time
import logging
from typing import Optional

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

logger = logging.getLogger(__name__)

# Глобальное подключение к Redis для дебаунсинга
_redis_pool: Optional[aioredis.Redis] = None

# Время дебаунсинга (в секундах)
DEBOUNCE_TIME = 2  # 2 секунды - если запрос на тот же UUID пришел в течение 2 секунд, используем кэш


async def init_debounce(redis_host: str = "127.0.0.1", redis_port: int = 6379, redis_db: int = 0, redis_password: Optional[str] = None):
    """Инициализация дебаунсинга"""
    global _redis_pool
    
    if aioredis is None:
        logger.warning("aioredis не установлен. Дебаунсинг недоступен.")
        return False
    
    try:
        _redis_pool = aioredis.Redis(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            password=redis_password,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2
        )
        
        await _redis_pool.ping()
        logger.info("Дебаунсинг инициализирован через Redis")
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации дебаунсинга: {e}")
        _redis_pool = None
        return False


async def check_debounce(uuid: str) -> bool:
    """
    Проверка дебаунсинга для UUID
    
    Returns:
        True если запрос нужно дебаунсить (использовать кэш),
        False если можно делать запрос к API
    """
    if not _redis_pool:
        return False
    
    try:
        debounce_key = f"debounce:{uuid}"
        exists = await _redis_pool.exists(debounce_key)
        
        if exists:
            # Запрос уже обрабатывается или был недавно - используем кэш
            logger.debug(f"Дебаунсинг для {uuid}: запрос был недавно, используем кэш")
            return True
        
        # Устанавливаем флаг дебаунсинга на DEBOUNCE_TIME секунд
        await _redis_pool.setex(debounce_key, DEBOUNCE_TIME, "1")
        return False
        
    except Exception as e:
        logger.error(f"Ошибка проверки дебаунсинга для {uuid}: {e}")
        # При ошибке разрешаем запрос (fail-open)
        return False

