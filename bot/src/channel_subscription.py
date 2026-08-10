from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import Dict, Tuple, Optional
from loguru import logger
import time
from app_config import app_conf
from button_helpers import btn

# Глобальный экземпляр — кеш живёт всё время работы бота
_checker_instance: Optional['ChannelSubscriptionChecker'] = None


def get_channel_checker(bot: Bot) -> 'ChannelSubscriptionChecker':
    """Возвращает синглтон ChannelSubscriptionChecker с общим кешем"""
    global _checker_instance
    if _checker_instance is None or _checker_instance.bot is not bot:
        _checker_instance = ChannelSubscriptionChecker(bot)
    return _checker_instance


class ChannelSubscriptionChecker:
    """Проверка подписки пользователя на канал"""
    
    def __init__(self, bot: Bot):
        self.bot = bot
        # Кэш: {user_id: (is_subscribed, timestamp)}
        self._cache: Dict[int, Tuple[bool, float]] = {}
        self._cache_ttl = 60  # 1 минута (TTL кэша) - уменьшено для предотвращения обхода защиты
    
    def _is_channel_id(self, channel_identifier: str) -> bool:
        """
        Определяет, является ли идентификатор ID канала (число) или username
        
        Args:
            channel_identifier: Username или ID канала
            
        Returns:
            True если это ID, False если username
        """
        # Убираем пробелы и @
        identifier = channel_identifier.strip().lstrip('@')
        
        # Проверяем, является ли числом (включая отрицательные)
        try:
            int(identifier)
            return True
        except ValueError:
            return False
    
    def _normalize_channel_identifier(self, channel_identifier: str) -> str | int:
        """
        Нормализует идентификатор канала для использования в get_chat_member
        
        Args:
            channel_identifier: Username или ID канала
            
        Returns:
            Нормализованный идентификатор (int для ID, str для username)
        """
        identifier = channel_identifier.strip()
        
        if self._is_channel_id(identifier):
            # Это ID - возвращаем как число
            return int(identifier)
        else:
            # Это username - добавляем @ если нужно
            if not identifier.startswith('@'):
                return f"@{identifier}"
            return identifier
    
    async def _get_channel_username_for_link(self, channel_identifier: str) -> str:
        """
        Получает username канала для формирования ссылки
        
        Args:
            channel_identifier: Username или ID канала
            
        Returns:
            Username канала (без @) для ссылки или ID для ссылки вида t.me/c/...
        """
        identifier = channel_identifier.strip().lstrip('@')
        
        if self._is_channel_id(identifier):
            # Это ID - пытаемся получить username через get_chat
            try:
                chat_id = int(identifier)
                chat = await self.bot.get_chat(chat_id)
                if chat.username:
                    return chat.username
                # Если username нет, используем ID для ссылки вида t.me/c/...
                # ID канала для ссылки: убираем минус и первые 3 цифры (100)
                channel_id_for_link = str(abs(chat_id))[3:]  # Убираем "100" в начале
                return f"c/{channel_id_for_link}"
            except Exception as e:
                logger.warning(f"Не удалось получить username канала {identifier}: {e}")
                # Fallback: используем ID для ссылки
                channel_id_for_link = str(abs(int(identifier)))[3:]
                return f"c/{channel_id_for_link}"
        else:
            # Это username - возвращаем без @
            return identifier.lstrip('@')
    
    async def check_subscription(
        self, 
        user_id: int, 
        channel_identifier: str
    ) -> bool:
        """
        Проверяет, подписан ли пользователь на канал
        
        Args:
            user_id: Telegram ID пользователя
            channel_identifier: Username канала (без @) или ID канала (например: -1001234567890)
            
        Returns:
            True если подписан, False если нет
        """
        if not channel_identifier:
            return True  # Если канал не указан, считаем что проверка не нужна
        
        # Проверяем кэш
        if user_id in self._cache:
            is_subscribed, cached_time = self._cache[user_id]
            if time.time() - cached_time < self._cache_ttl:
                return is_subscribed
        
        # Проверяем через Telegram API
        try:
            # Нормализуем идентификатор (ID или username)
            chat_id = self._normalize_channel_identifier(channel_identifier)
            
            # Получаем информацию о членстве
            # ВАЖНО: Бот должен быть администратором канала!
            member = await self.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            
            # Статусы подписки: member, administrator, creator
            # Статусы не подписки: left, kicked, restricted
            is_subscribed = member.status in ('member', 'administrator', 'creator')
            
            # Сохраняем в кэш
            self._cache[user_id] = (is_subscribed, time.time())
            
            return is_subscribed
            
        except Exception as e:
            err_str = str(e).lower()
            # Ошибки конфигурации канала — бот не может проверить членство.
            # Возвращаем False: если не можем проверить подписку — не пускаем.
            # "member list is inaccessible" — у канала закрыт список участников
            # "chat not found" / "channel not found" — неверный username/id
            # "bot is not a member" / "have no rights" — бот не админ канала
            config_errors = (
                'member list is inaccessible',
                'chat not found',
                'channel not found',
                'group chat was deactivated',
                'have no rights',
                'bot is not a member',
                'not enough rights',
                'forbidden',
            )
            if any(err in err_str for err in config_errors):
                logger.error(
                    f"Ошибка конфигурации канала '{channel_identifier}' для user {user_id}: {e}. "
                    f"Доступ ЗАПРЕЩЁН. Проверьте: бот добавлен администратором в канал?"
                )
                return False

            # Временные сетевые ошибки — пропускаем чтобы не блокировать пользователей
            logger.warning(f"Временная ошибка проверки подписки для {user_id} на канал {channel_identifier}: {e}")
            return True
    
    async def invalidate_cache(self, user_id: Optional[int] = None):
        """Инвалидирует кэш для пользователя или всех пользователей"""
        if user_id:
            self._cache.pop(user_id, None)
        else:
            self._cache.clear()
    
    async def get_subscription_message(self, channel_identifier: str) -> str:
        """Возвращает текст сообщения с просьбой подписаться"""
        # Получаем username для отображения
        display_name = await self._get_channel_username_for_link(channel_identifier)
        if display_name.startswith('c/'):
            # Это ID канала без username - показываем просто "канал"
            display_name = "наш канал"
        else:
            display_name = f"@{display_name}"
        
        default_message = (
            "📢 <b>Подписка на канал обязательна</b>\n\n"
            f"Для продолжения работы с ботом необходимо подписаться на {display_name}.\n\n"
            "После подписки нажмите кнопку <b>\"Проверить подписку\"</b> ниже."
        )
        
        custom_message = app_conf.get('channel_subscription_message', '')
        if custom_message:
            # Заменяем плейсхолдер {channel_username} если есть
            return custom_message.replace('{channel_username}', display_name.lstrip('@'))
        
        return default_message
    
    async def get_subscription_keyboard(self, channel_identifier: str) -> InlineKeyboardMarkup:
        """Возвращает клавиатуру с кнопками подписки и проверки"""
        # Используем существующую настройку для ссылки на канал
        channel_link = app_conf.get('bot_channel_link', '').strip()
        
        if not channel_link:
            # Если ссылка не указана в bot_channel_link, формируем автоматически
            channel_link_identifier = await self._get_channel_username_for_link(channel_identifier)
            
            if channel_link_identifier.startswith('c/'):
                # Это ID канала без username
                channel_link = f"https://t.me/{channel_link_identifier}"
            else:
                # Это username
                channel_link = f"https://t.me/{channel_link_identifier}"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [btn('btn_bot_channel', url=channel_link)],
            [
                InlineKeyboardButton(
                    text="✅ Проверить подписку",
                    callback_data="check_subscription"
                )
            ]
        ])
        
        return keyboard
