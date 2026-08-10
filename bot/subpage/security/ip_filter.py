"""
IP фильтрация - blacklist/whitelist через Redis
"""
import logging
from typing import Optional, Set
from fastapi import Request, Response

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

logger = logging.getLogger(__name__)

# Глобальное подключение к Redis для IP фильтрации
_redis_pool: Optional[aioredis.Redis] = None

# In-memory кэш для быстрого доступа (обновляется каждые 60 секунд)
_blacklist_cache: Set[str] = set()
_whitelist_cache: Set[str] = set()
_cache_updated_at = 0


async def init_ip_filter(redis_host: str = "127.0.0.1", redis_port: int = 6379, redis_db: int = 0, redis_password: Optional[str] = None):
    """Инициализация IP фильтра"""
    global _redis_pool
    
    if aioredis is None:
        logger.warning("aioredis не установлен. IP фильтрация через Redis недоступна.")
        return False
    
    try:
        _redis_pool = aioredis.Redis(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            password=redis_password,
            decode_responses=True,  # Получаем строки
            socket_connect_timeout=2,
            socket_timeout=2
        )
        
        await _redis_pool.ping()
        logger.info("IP фильтр инициализирован через Redis")
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации IP фильтра: {e}")
        _redis_pool = None
        return False


def get_client_ip(request: Request) -> str:
    """Получение реального IP клиента"""
    # Проверяем заголовки прокси
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Берем первый IP из списка
        return forwarded_for.split(",")[0].strip()
    
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    
    # Fallback на прямой IP
    if request.client:
        return request.client.host
    
    return "unknown"


async def is_ip_blocked(ip: str) -> bool:
    """Проверка, заблокирован ли IP"""
    if not _redis_pool:
        return False
    
    try:
        # Проверяем в Redis
        blocked = await _redis_pool.exists(f"ip_blocked:{ip}")
        return bool(blocked)
    except Exception as e:
        logger.error(f"Ошибка проверки блокировки IP {ip}: {e}")
        return False


async def block_ip(ip: str, ttl: int = 3600):
    """Блокировка IP на указанное время (в секундах)"""
    if not _redis_pool:
        return False
    
    try:
        await _redis_pool.setex(f"ip_blocked:{ip}", ttl, "1")
        logger.warning(f"IP {ip} заблокирован на {ttl} секунд")
        return True
    except Exception as e:
        logger.error(f"Ошибка блокировки IP {ip}: {e}")
        return False


async def unblock_ip(ip: str):
    """Разблокировка IP"""
    if not _redis_pool:
        return False
    
    try:
        await _redis_pool.delete(f"ip_blocked:{ip}")
        logger.info(f"IP {ip} разблокирован")
        return True
    except Exception as e:
        logger.error(f"Ошибка разблокировки IP {ip}: {e}")
        return False


async def check_ip_middleware(request: Request, call_next):
    """Middleware для проверки IP"""
    ip = get_client_ip(request)
    
    # Проверяем блокировку
    if await is_ip_blocked(ip):
        logger.warning(f"Заблокированный IP {ip} пытается получить доступ")
        return Response(
            content="Access denied",
            status_code=403,
            headers={"X-Blocked-IP": "true"}
        )
    
    # Продолжаем обработку запроса
    response = await call_next(request)
    return response

