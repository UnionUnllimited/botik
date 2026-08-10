"""
Redis кэш для подписок
Используется как основной кэш для высокой производительности
"""
import json
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timezone

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

logger = logging.getLogger(__name__)

# Глобальное подключение к Redis
_redis_pool: Optional[aioredis.Redis] = None


async def init_redis(host: str = "127.0.0.1", port: int = 6379, db: int = 0, password: Optional[str] = None) -> bool:
    """Инициализация подключения к Redis"""
    global _redis_pool
    
    if aioredis is None:
        logger.warning("aioredis не установлен. Redis кэш недоступен.")
        return False
    
    try:
        _redis_pool = aioredis.Redis(
            host=host,
            port=port,
            db=db,
            password=password,
            decode_responses=False,  # Получаем bytes для совместимости
            socket_connect_timeout=2,
            socket_timeout=2,
            retry_on_timeout=True,
            health_check_interval=30
        )
        
        # Проверяем подключение
        await _redis_pool.ping()
        logger.info(f"Redis подключен: {host}:{port}/{db}")
        return True
    except Exception as e:
        logger.error(f"Ошибка подключения к Redis: {e}")
        _redis_pool = None
        return False


async def close_redis():
    """Закрытие подключения к Redis"""
    global _redis_pool
    if _redis_pool:
        try:
            await _redis_pool.close()
            logger.info("Redis подключение закрыто")
        except Exception as e:
            logger.error(f"Ошибка закрытия Redis: {e}")
        finally:
            _redis_pool = None


async def is_available() -> bool:
    """Проверка доступности Redis"""
    if _redis_pool is None:
        return False
    try:
        await _redis_pool.ping()
        return True
    except Exception:
        return False


async def get_subscription_cache(uuid: str) -> Optional[Dict[str, Any]]:
    """Получение данных подписки из Redis кэша"""
    if not await is_available():
        return None
    
    try:
        cache_key = f"sub:{uuid}"
        
        # Проверяем TTL перед получением данных
        ttl = await _redis_pool.ttl(cache_key)
        
        if ttl == -2:
            # Ключ не существует
            logger.debug(f"Ключ {cache_key} не найден в Redis (TTL: -2)")
            return None
        elif ttl == -1:
            # Ключ существует, но без TTL (не должно быть, но на всякий случай)
            logger.warning(f"⚠️  Ключ {cache_key} существует в Redis без TTL (TTL: -1)")
        else:
            logger.debug(f"Ключ {cache_key} найден в Redis, осталось TTL: {ttl}с")
        
        cached_data = await _redis_pool.get(cache_key)
        
        if cached_data is None:
            # Ключ истек или не существует
            if ttl == -2:
                logger.debug(f"Данные подписки {uuid} не найдены в Redis кэше (ключ не существует)")
            else:
                logger.debug(f"Данные подписки {uuid} не найдены в Redis кэше (TTL истек или ключ удален)")
            return None
        
        # Декодируем bytes в строку, затем парсим JSON
        if isinstance(cached_data, bytes):
            cached_data = cached_data.decode('utf-8')
        
        sub_data = json.loads(cached_data)
        
        # Логируем информацию о кэше
        is_found = sub_data.get('isFound', False)
        logger.info(f"✅ Данные подписки {uuid} получены из Redis кэша (TTL осталось: {ttl}с, isFound: {is_found})")
        return sub_data
        
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка парсинга JSON из Redis для {uuid}: {e}")
        return None
    except Exception as e:
        logger.error(f"Ошибка получения кэша из Redis для {uuid}: {e}")
        return None


async def save_subscription_cache(uuid: str, sub_data: Dict[str, Any], ttl: int = 300):
    """
    Сохранение данных подписки в Redis кэш с TTL
    
    Args:
        uuid: UUID подписки
        sub_data: Данные подписки
        ttl: Время жизни в секундах (по умолчанию 5 минут)
    """
    if not await is_available():
        return False
    
    try:
        cache_key = f"sub:{uuid}"
        
        # Сериализуем данные в JSON
        json_data = json.dumps(sub_data, ensure_ascii=False)
        
        # Определяем TTL в зависимости от статуса подписки
        # Для активных подписок - дольше TTL, для неактивных - короче
        is_found = sub_data.get('isFound', False)
        if is_found:
            # Активная подписка - используем переданный TTL (обычно 15 минут)
            actual_ttl = ttl
        else:
            # Неактивная подписка (404) - кэшируем на 1 минуту чтобы не брутфорсить
            actual_ttl = 60
        
        # Сохраняем с TTL (в секундах)
        await _redis_pool.setex(cache_key, actual_ttl, json_data)
        
        # Проверяем, что TTL установлен правильно
        remaining_ttl = await _redis_pool.ttl(cache_key)
        logger.info(f"✅ Данные подписки {uuid} сохранены в Redis кэш (TTL: {actual_ttl}с, осталось: {remaining_ttl}с, isFound: {is_found})")
        
        if remaining_ttl == -1:
            logger.warning(f"⚠️  ВНИМАНИЕ: TTL не установлен для ключа {cache_key}! Кэш не истечет автоматически.")
        elif remaining_ttl == -2:
            logger.error(f"❌ ОШИБКА: Ключ {cache_key} не найден сразу после сохранения!")
        elif remaining_ttl != actual_ttl:
            logger.warning(f"⚠️  TTL не совпадает: установлен {actual_ttl}с, фактический {remaining_ttl}с")
        
        return True
        
    except Exception as e:
        logger.error(f"Ошибка сохранения кэша в Redis для {uuid}: {e}")
        return False


async def delete_subscription_cache(uuid: str):
    """Удаление данных подписки из Redis кэша"""
    if not await is_available():
        return False
    
    try:
        cache_key = f"sub:{uuid}"
        await _redis_pool.delete(cache_key)
        logger.info(f"Данные подписки {uuid} удалены из Redis кэша")
        return True
    except Exception as e:
        logger.error(f"Ошибка удаления кэша из Redis для {uuid}: {e}")
        return False


async def get_cache_stats() -> Dict[str, Any]:
    """Получение статистики Redis кэша"""
    if not await is_available():
        return {'total': 0, 'available': False}
    
    try:
        # Подсчитываем ключи с префиксом "sub:"
        keys = await _redis_pool.keys("sub:*")
        total = len(keys)
        
        # Получаем информацию о памяти
        info = await _redis_pool.info('memory')
        memory_used = info.get('used_memory_human', '0B')
        
        return {
            'total': total,
            'available': True,
            'memory_used': memory_used
        }
    except Exception as e:
        logger.error(f"Ошибка получения статистики Redis: {e}")
        return {'total': 0, 'available': False}


async def cleanup_expired_cache():
    """Очистка истекших записей из Redis кэша"""
    # Redis автоматически удаляет записи с истекшим TTL
    # Эта функция может использоваться для ручной очистки или логирования
    if not await is_available():
        return
    
    try:
        # Можно добавить логику для очистки старых записей
        # Но обычно Redis сам управляет TTL
        logger.debug("Redis автоматически управляет TTL записей")
    except Exception as e:
        logger.error(f"Ошибка очистки Redis кэша: {e}")

