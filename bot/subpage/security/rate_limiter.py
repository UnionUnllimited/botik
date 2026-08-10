"""
Rate limiting через Redis для защиты от DDoS
"""
import logging
import time
from typing import Optional
from fastapi import Request, Response

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

logger = logging.getLogger(__name__)

# Глобальное подключение к Redis для rate limiting
_redis_pool: Optional[aioredis.Redis] = None

# Настройки rate limiting по умолчанию
DEFAULT_RATE_LIMIT_PER_SECOND = 10
DEFAULT_RATE_LIMIT_BURST = 20
DEFAULT_RATE_LIMIT_PER_MINUTE = 100
DEFAULT_RATE_LIMIT_PER_HOUR = 1000

# Настройки блокировки при превышении
BLOCK_AFTER_FAILED_REQUESTS = 50  # Блокировать после 50 неудачных запросов
BLOCK_DURATION = 3600  # Блокировать на 1 час


async def init_rate_limiter(redis_host: str = "127.0.0.1", redis_port: int = 6379, redis_db: int = 0, redis_password: Optional[str] = None):
    """Инициализация rate limiter"""
    global _redis_pool
    
    if aioredis is None:
        logger.warning("aioredis не установлен. Rate limiting через Redis недоступен.")
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
        logger.info("Rate limiter инициализирован через Redis")
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации rate limiter: {e}")
        _redis_pool = None
        return False


def get_client_ip(request: Request) -> str:
    """Получение реального IP клиента"""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    
    if request.client:
        return request.client.host
    
    return "unknown"


async def check_rate_limit(
    ip: str,
    limit_per_second: int = DEFAULT_RATE_LIMIT_PER_SECOND,
    burst: int = DEFAULT_RATE_LIMIT_BURST,
    limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    limit_per_hour: int = DEFAULT_RATE_LIMIT_PER_HOUR
) -> tuple[bool, Optional[str]]:
    """
    Проверка rate limit для IP
    
    Returns:
        (is_allowed, error_message)
    """
    if not _redis_pool:
        # Если Redis недоступен, разрешаем запрос (fallback)
        return True, None
    
    try:
        current_time = int(time.time())
        current_minute = current_time // 60
        current_hour = current_time // 3600
        
        # Проверка лимита в секунду
        second_key = f"rate_limit:second:{ip}:{current_time}"
        second_count = await _redis_pool.incr(second_key)
        await _redis_pool.expire(second_key, 2)  # TTL 2 секунды
        
        if second_count > limit_per_second + burst:
            logger.warning(f"Rate limit превышен для {ip}: {second_count} запросов/сек (лимит: {limit_per_second})")
            await _track_failed_request(ip)
            return False, f"Too many requests per second (limit: {limit_per_second})"
        
        # Проверка лимита в минуту
        minute_key = f"rate_limit:minute:{ip}:{current_minute}"
        minute_count = await _redis_pool.incr(minute_key)
        await _redis_pool.expire(minute_key, 120)  # TTL 2 минуты
        
        if minute_count > limit_per_minute:
            logger.warning(f"Rate limit превышен для {ip}: {minute_count} запросов/мин (лимит: {limit_per_minute})")
            await _track_failed_request(ip)
            return False, f"Too many requests per minute (limit: {limit_per_minute})"
        
        # Проверка лимита в час
        hour_key = f"rate_limit:hour:{ip}:{current_hour}"
        hour_count = await _redis_pool.incr(hour_key)
        await _redis_pool.expire(hour_key, 7200)  # TTL 2 часа
        
        if hour_count > limit_per_hour:
            logger.warning(f"Rate limit превышен для {ip}: {hour_count} запросов/час (лимит: {limit_per_hour})")
            await _track_failed_request(ip)
            return False, f"Too many requests per hour (limit: {limit_per_hour})"
        
        return True, None
        
    except Exception as e:
        logger.error(f"Ошибка проверки rate limit для {ip}: {e}")
        # При ошибке разрешаем запрос (fail-open)
        return True, None


async def _track_failed_request(ip: str):
    """Отслеживание неудачных запросов для автоматической блокировки"""
    if not _redis_pool:
        return
    
    try:
        # Увеличиваем счетчик неудачных запросов
        fail_key = f"rate_limit:failures:{ip}"
        fail_count = await _redis_pool.incr(fail_key)
        await _redis_pool.expire(fail_key, 300)  # TTL 5 минут
        
        # Если превышен порог - блокируем IP
        if fail_count >= BLOCK_AFTER_FAILED_REQUESTS:
            await _redis_pool.setex(f"ip_blocked:{ip}", BLOCK_DURATION, "1")
            logger.warning(f"IP {ip} автоматически заблокирован после {fail_count} неудачных запросов")
    except Exception as e:
        logger.error(f"Ошибка отслеживания неудачных запросов для {ip}: {e}")


async def rate_limit_middleware(request: Request, call_next):
    """Middleware для rate limiting"""
    # Пропускаем статические файлы
    if request.url.path.startswith("/static/"):
        return await call_next(request)
    
    ip = get_client_ip(request)
    
    # Проверяем rate limit
    is_allowed, error_msg = await check_rate_limit(ip)
    
    if not is_allowed:
        logger.warning(f"Rate limit превышен для IP {ip}: {error_msg}")
        return Response(
            content=error_msg or "Too many requests",
            status_code=429,
            headers={
                "Retry-After": "60",
                "X-RateLimit-Limit": "100",
                "X-RateLimit-Remaining": "0"
            }
        )
    
    # Продолжаем обработку запроса
    response = await call_next(request)
    
    # Добавляем заголовки rate limit
    response.headers["X-RateLimit-Limit"] = "100"
    response.headers["X-RateLimit-Remaining"] = "99"
    
    return response

