import aiosqlite
import os
from typing import Optional, List, Dict, Any, Union
from quart import current_app
import asyncio
from contextlib import asynccontextmanager

# Получаем путь к БД из конфигурации Flask
def _get_database_path() -> str:
    try:
        return current_app.config['DATABASE_PATH']
    except RuntimeError:
        # Если нет контекста приложения, используем fallback
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'vpn_bot.db')

@asynccontextmanager
async def get_db_connection():
    """Асинхронный контекст-менеджер для подключения к БД"""
    db_path = _get_database_path()
    conn = await aiosqlite.connect(db_path, timeout=30)
    try:
        # Настройки для стабильной работы
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.execute("PRAGMA busy_timeout=30000;")
        await conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
    finally:
        await conn.close()

async def async_query_db(query: str, args: tuple = (), one: bool = False) -> Union[List[Dict], Dict, None]:
    """
    Асинхронная версия query_db
    Выполняет SELECT запросы и возвращает результаты
    """
    async with get_db_connection() as db:
        db.row_factory = aiosqlite.Row  # Возвращает результаты как словари
        
        async with db.execute(query, args) as cursor:
            if one:
                row = await cursor.fetchone()
                return dict(row) if row else None
            else:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

async def async_execute_db(query: str, args: tuple = ()) -> int:
    """
    Асинхронная версия execute_db
    Выполняет INSERT, UPDATE, DELETE запросы
    Возвращает количество затронутых строк
    """
    async with get_db_connection() as db:
        async with db.execute(query, args) as cursor:
            await db.commit()
            return cursor.rowcount

async def async_execute_many(query: str, args_list: List[tuple]) -> int:
    """
    Асинхронная версия для множественных операций
    Полезно для массовых INSERT/UPDATE
    """
    async with get_db_connection() as db:
        async with db.executemany(query, args_list) as cursor:
            await db.commit()
            return cursor.rowcount

# Утилиты для работы с БД
async def async_get_table_info(table_name: str) -> List[Dict]:
    """Получить информацию о структуре таблицы"""
    return await async_query_db("PRAGMA table_info(?)", (table_name,))


# --- Кэш набора колонок для часто используемых таблиц ---
# Схема меняется только при миграциях/рестарте, поэтому держим список колонок
# в памяти на всё время жизни процесса. Это убирает ~1 PRAGMA-запрос на каждый
# рендер списка пользователей и десятки запросов на /api/* эндпоинтах.
_TABLE_COLUMNS_CACHE: Dict[str, List[str]] = {}


async def get_table_columns_cached(table_name: str) -> List[str]:
    """Вернуть список колонок таблицы, кешированный на уровне процесса.

    Безопасно использовать после старта приложения — миграции выполняются один
    раз при инициализации, и набор колонок дальше не меняется.
    """
    cached = _TABLE_COLUMNS_CACHE.get(table_name)
    if cached is not None:
        return cached
    rows = await async_query_db(f"PRAGMA table_info({table_name})", ())
    columns = [r['name'] for r in (rows or [])]
    _TABLE_COLUMNS_CACHE[table_name] = columns
    return columns


def invalidate_table_columns_cache(table_name: Optional[str] = None) -> None:
    """Сбросить кэш колонок (вызывать после ALTER TABLE / миграций)."""
    if table_name is None:
        _TABLE_COLUMNS_CACHE.clear()
    else:
        _TABLE_COLUMNS_CACHE.pop(table_name, None)

async def async_get_table_names() -> List[str]:
    """Получить список всех таблиц"""
    rows = await async_query_db("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return [row['name'] for row in rows]

async def async_count_rows(table_name: str, where_clause: str = "", args: tuple = ()) -> int:
    """Подсчитать количество строк в таблице"""
    query = f"SELECT COUNT(*) FROM {table_name}"
    if where_clause:
        query += f" WHERE {where_clause}"
    
    result = await async_query_db(query, args, one=True)
    return result['COUNT(*)'] if result else 0

async def async_get_db_setting(key: str, default: str = '') -> str:
    """Получить значение настройки из БД"""
    result = await async_query_db("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    return result['value'] if result and result.get('value') else default

# Функция для инициализации таблиц
async def async_init_tables():
    """Создать необходимые таблицы, если их нет"""
    tables_sql = [
        """
        CREATE TABLE IF NOT EXISTS news_tasks (
            task_id TEXT PRIMARY KEY,
            user_count INTEGER,
            status TEXT,
            created_at TEXT,
            success_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            error_details TEXT,
            completed_at TEXT,
            news_text TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS migration_tasks (
            task_id TEXT PRIMARY KEY,
            from_server_id INTEGER,
            to_server_id INTEGER,
            user_count INTEGER,
            status TEXT,
            created_at TEXT,
            migrated_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            error_details TEXT,
            completed_at TEXT
        )
        """
        ,
        """
        CREATE TABLE IF NOT EXISTS cleanup_tasks (
            task_id TEXT PRIMARY KEY,
            server_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running',
            success_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            server_results TEXT,
            error_details TEXT,
            created_at TEXT,
            completed_at TEXT
        )
        """
    ]
    
    for sql in tables_sql:
        await async_execute_db(sql)

# Функция для проверки состояния БД
async def async_check_db_health() -> Dict[str, Any]:
    """Проверить состояние БД"""
    try:
        # Проверяем подключение
        async with get_db_connection() as db:
            # Проверяем основные таблицы
            tables = await async_get_table_names()
            
            # Проверяем количество пользователей
            users_count = await async_count_rows('users')
            
            # Проверяем настройки
            settings_count = await async_count_rows('settings')
            
            return {
                'status': 'healthy',
                'tables': tables,
                'users_count': users_count,
                'settings_count': settings_count,
                'wal_mode': 'enabled'
            }
    except Exception as e:
        return {
            'status': 'error',
            'error': str(e),
            'wal_mode': 'unknown'
        }
