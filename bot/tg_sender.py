import asyncio
import os

from app_config import app_conf
from config import DATABASE_NAME as DB_PATH
import db_helpers
from src.telegram_bot_factory import make_aiogram_bot

# Кеш для токена бота
_BOT_TOKEN_CACHE = None
_BOT_TOKEN_CACHE_TIMESTAMP = 0
_CACHE_TTL = 3600  # 1 час

async def _async_read_bot_token_from_db() -> str:
    try:
        async with db_helpers.get_db_connection_safe() as db:
            async with db.execute("SELECT value FROM settings WHERE key = 'bot_token'") as cur:
                row = await cur.fetchone()
                return row[0] if row and row[0] else ''
    except Exception as e:
        print(f"[TG_SENDER] Ошибка чтения токена из БД: {e}")
        return ''

async def async_get_bot_token() -> str:
    """Асинхронно получает токен бота с кешированием."""
    global _BOT_TOKEN_CACHE, _BOT_TOKEN_CACHE_TIMESTAMP
    env_token = os.getenv("BOT_TOKEN")
    if env_token:
        return env_token
    import time
    current_time = time.time()
    if _BOT_TOKEN_CACHE and (current_time - _BOT_TOKEN_CACHE_TIMESTAMP) < _CACHE_TTL:
        return _BOT_TOKEN_CACHE
    token = await _async_read_bot_token_from_db()
    if token:
        _BOT_TOKEN_CACHE = token
        _BOT_TOKEN_CACHE_TIMESTAMP = current_time
    return token or ''

def get_bot_token():
    """Синхронный вариант для мест, где нет event loop (например, старт).
    В асинхронном коде используйте async_get_bot_token()."""
    global _BOT_TOKEN_CACHE, _BOT_TOKEN_CACHE_TIMESTAMP
    env_token = os.getenv("BOT_TOKEN")
    if env_token:
        return env_token
    import time
    current_time = time.time()
    if _BOT_TOKEN_CACHE and (current_time - _BOT_TOKEN_CACHE_TIMESTAMP) < _CACHE_TTL:
        return _BOT_TOKEN_CACHE
    # Если уже есть запущенный цикл, не пытаемся запускать новый — вернём кеш/пусто
    try:
        asyncio.get_running_loop()
        # Внутри работающего цикла: синхронно читать нельзя
        return _BOT_TOKEN_CACHE or ''
    except RuntimeError:
        # Цикла нет — можно безопасно выполнить асинхронный запрос
        token = asyncio.run(_async_read_bot_token_from_db())
        if token:
            _BOT_TOKEN_CACHE = token
            _BOT_TOKEN_CACHE_TIMESTAMP = current_time
        return token or ''

def clear_bot_token_cache():
    """Очищает кеш токена бота (например, при изменении настроек)"""
    global _BOT_TOKEN_CACHE, _BOT_TOKEN_CACHE_TIMESTAMP
    _BOT_TOKEN_CACHE = None
    _BOT_TOKEN_CACHE_TIMESTAMP = 0

async def send_telegram_message(
    user_id: int,
    text: str,
    reply_markup=None,
    parse_mode="HTML",
    disable_web_page_preview: bool = False,
):
    print(f"[TG_SENDER] Начинаем отправку сообщения пользователю {user_id}")
    print(f"[TG_SENDER] Текст сообщения: {text[:100]}...")
    
    bot_token = await async_get_bot_token()
    if not bot_token:
        print(f"[TG_SENDER] ОШИБКА: Bot token не найден в базе данных!")
        raise RuntimeError('Bot token not found in database!')
    
    print(f"[TG_SENDER] Bot token получен: {bot_token[:10]}...")
    
    proxy_url = (app_conf.get('telegram_proxy_url') or '').strip() or None
    bot = make_aiogram_bot(bot_token, proxy_url)
    try:
        print(f"[TG_SENDER] Отправляем сообщение через aiogram...")
        result = await bot.send_message(
            user_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        print(f"[TG_SENDER] ✅ Сообщение успешно отправлено пользователю {user_id}")
        print(f"[TG_SENDER] Результат отправки: {result}")
        return result
    except Exception as e:
        print(f"[TG_SENDER] ❌ ОШИБКА отправки сообщения пользователю {user_id}: {e}")
        print(f"[TG_SENDER] Тип ошибки: {type(e).__name__}")
        raise
    finally:
        print(f"[TG_SENDER] Закрываем сессию бота")
        await bot.session.close()


async def send_telegram_photo(
    user_id: int,
    photo: str,
    caption: str = "",
    reply_markup=None,
    parse_mode: str = "HTML",
):
    """Отправляет фото (по file_id или URL) с подписью и клавиатурой.

    Важно: Telegram file_id привязан к конкретному боту. При смене bot_token
    старые file_id перестанут работать — потребуется перезагрузить фото в
    новом боте и обновить настройку.

    Лимит caption — 1024 символа. Если caption длиннее, Telegram вернёт ошибку.
    """
    bot_token = await async_get_bot_token()
    if not bot_token:
        raise RuntimeError('Bot token not found in database!')

    proxy_url = (app_conf.get('telegram_proxy_url') or '').strip() or None
    bot = make_aiogram_bot(bot_token, proxy_url)
    try:
        return await bot.send_photo(
            user_id,
            photo=photo,
            caption=caption or None,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    finally:
        await bot.session.close()