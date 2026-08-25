import httpx
import asyncio
import logging
import os
import math
import random
import secrets
import time
from datetime import datetime, timedelta, timezone 
import uuid as py_uuid
import json
import html
import re
from typing import Optional, Dict
import pytz

# Импортируем необходимые модули aiogram для работы с Telegram Bot API
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp, MenuButtonDefault, MenuButtonCommands, BotCommand, ErrorEvent, LinkPreviewOptions
from aiogram.types.input_file import FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import CommandStart, StateFilter, ExceptionTypeFilter
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from typing import Callable, Dict, Any, Awaitable
import hashlib
from aiohttp import web
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.markdown import hcode
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound, TelegramAPIError, TelegramBadRequest
from aiogram.types import LabeledPrice, PreCheckoutQuery

# Импортируем YooKassa для работы с платежами
from yookassa import Configuration as YKConfig, Payment as YKPayment
from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder

# Импортируем YooMoney для работы с платежами
# Импортируем внутренние модули проекта
from app_config import app_conf # Менеджер настроек
from config import migrate_remnawave_db_if_needed
from button_helpers import btn # Стилизованные inline-кнопки (text/style/icon из БД)
import keyboards # Клавиатуры для Telegram
import db_helpers # Работа с базой данных
from remnawave_manager import remnawave_manager_instance # Работа с Remnawave
import admin # Админские команды и обработчики
from subscription_manager import grant_subscription
from tg_sender import get_bot_token
from src.telegram_bot_factory import make_aiogram_bot, normalize_telegram_proxy_url
from src.subscription_handlers import register_subscription_handlers, show_trial_progress, show_trial_progress_edit
from src.router_catalog import register_router_catalog_handlers, remember_client
from src.shop_outbox import start_outbox
from src.shop_sync import start_tariff_sync
from src.maintenance.register import register_maintenance
from src.channel_subscription import ChannelSubscriptionChecker, get_channel_checker
from src.notifications import start_notification_tasks
from src.tasks import start_expired_traffic_reset_task, start_stale_payments_task
from src.pay.wata import create_wata_payment_link
from src.texts import (
    REST_TEXT_DEFAULTS,
    TXT_USER_DELETED,
    TXT_BLOCKED,
    TXT_SUPPORT_FALLBACK,
    TXT_PAYMENT_GRANT_FAILED,
    TXT_PAYMENT_TRAFFIC_GRANT_FAILED,
    txt_payment_renewal,
    txt_subscription_time_header,
    txt_admin_manual_payment_notify,
    txt_yoomoney_check,
    txt_traffic_renewal_select,
    txt_traffic_renewal_confirm,
    txt_traffic_renewal_payment,
    txt_manual_traffic_renewal,
    txt_partner_program,
    txt_withdraw_request,
    setting_text,
    DEFAULT_TEXT_WEBSITE_CABINET_NO_EMAIL,
    DEFAULT_TEXT_WEBSITE_CABINET_ACTIVE,
    DEFAULT_TEXT_WEBSITE_CABINET_EXPIRED,
)
from src.core.utils import (
    format_msk_date,
    format_msk_date_long,
    format_msk_date_day,
    format_traffic_inline,
    format_traffic_section,
    get_default_limit_gb,
)
from loguru import logger
import aiosqlite
from config import DATABASE_NAME
import sys
import os
import hashlib

# Отключаем SSL-предупреждения
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Патч для edit_text -------------------------------------------------
_MEDIA_CONTENT_TYPES = {
    'photo', 'video', 'animation', 'document',
    'audio', 'voice', 'video_note', 'sticker',
}

# (chat_id, message_id) → новое Message-«преемник», на которое теперь
# проксируются все edit_text(...). Нужно потому, что хэндлеры держат
# ссылку на ИСХОДНОЕ query.message и продолжают делать .edit_text(...)
# на нём. Без проксирования каждое такое редактирование плодило бы
# новые сообщения вместо обновления уже отправленного.
_MSG_REPLACEMENTS: dict[tuple[int, int], 'Message'] = {}
_MSG_REPLACEMENTS_MAX = 2048


def _remember_replacement(old_msg, new_msg) -> None:
    try:
        key = (old_msg.chat.id, old_msg.message_id)
    except Exception:
        return
    if new_msg is None:
        return
    if len(_MSG_REPLACEMENTS) >= _MSG_REPLACEMENTS_MAX:
        for k in list(_MSG_REPLACEMENTS.keys())[: _MSG_REPLACEMENTS_MAX // 2]:
            _MSG_REPLACEMENTS.pop(k, None)
    _MSG_REPLACEMENTS[key] = new_msg


def _resolve_replacement(msg):
    seen: set[tuple[int, int]] = set()
    cur = msg
    while True:
        try:
            key = (cur.chat.id, cur.message_id)
        except Exception:
            return cur
        if key in seen:
            return cur
        seen.add(key)
        nxt = _MSG_REPLACEMENTS.get(key)
        if nxt is None:
            return cur
        cur = nxt


if not getattr(Message, '_xsstore_smart_edit_patched', False):
    _orig_edit_text = Message.edit_text

    async def _smart_edit_text(self, text, **kwargs):
        target = _resolve_replacement(self)
        ctype = (getattr(target, 'content_type', '') or '').lower()
        is_media = ctype in _MEDIA_CONTENT_TYPES
        if is_media:
            try:
                await target.delete()
            except Exception:
                pass
            new_msg = await target.answer(text, **kwargs)
            _remember_replacement(target, new_msg)
            if target is not self:
                _remember_replacement(self, new_msg)
            return new_msg
        try:
            return await _orig_edit_text(target, text, **kwargs)
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if 'message is not modified' in msg:
                return None
            if (
                "message can't be edited" in msg
                or 'message to edit not found' in msg
                or 'message_id_invalid' in msg
            ):
                try:
                    await target.delete()
                except Exception:
                    pass
                new_msg = await target.answer(text, **kwargs)
                _remember_replacement(target, new_msg)
                if target is not self:
                    _remember_replacement(self, new_msg)
                return new_msg
            raise

    Message.edit_text = _smart_edit_text
    Message._xsstore_smart_edit_patched = True

# --- Логирование в файл ---
LOGS_DIR = os.path.join(os.path.dirname(__file__), 'logs')
os.makedirs(LOGS_DIR, exist_ok=True)
logger.remove()
logger.add(sys.stdout, level="INFO")
logger.add(os.path.join(LOGS_DIR, "bot.log"), rotation="20 MB", retention="10 days", encoding="utf-8", level="INFO")

# --- Инициализация бота и диспетчера ---
bot_token = os.getenv("BOT_TOKEN", get_bot_token())
_bootstrap_proxy = normalize_telegram_proxy_url(os.getenv('TELEGRAM_PROXY_URL'))
bot = make_aiogram_bot(bot_token, _bootstrap_proxy)
bot._tg_identity = (bot_token, _bootstrap_proxy)
storage = MemoryStorage()
dp = Dispatcher(storage=storage, bot=bot)


async def apply_bot_session_from_settings(dispatcher: Dispatcher) -> None:
    """Пересоздаёт Bot при смене токена или telegram_proxy_url (после load_settings)."""
    global bot
    new_token = os.getenv('BOT_TOKEN', app_conf.get('bot_token', ''))
    if not new_token:
        return
    proxy_url = normalize_telegram_proxy_url(app_conf.get('telegram_proxy_url')) or normalize_telegram_proxy_url(
        os.getenv('TELEGRAM_PROXY_URL')
    )
    identity = (new_token, proxy_url)
    if getattr(bot, '_tg_identity', None) == identity:
        return
    try:
        await bot.session.close()
    except Exception:
        pass
    bot_instance = make_aiogram_bot(new_token, proxy_url)
    bot_instance._tg_identity = identity
    dispatcher.bot = bot_instance
    bot = bot_instance

# --- Блокировки для предотвращения race condition при выдаче триала ---
# Словарь блокировок для каждого пользователя: {user_id: asyncio.Lock}
_trial_grant_locks: Dict[int, asyncio.Lock] = {}

# --- Middleware для проверки существования пользователя в БД ---
class UserExistsMiddleware(BaseMiddleware):
    """Middleware для проверки существования пользователя в БД перед обработкой callback queries"""
    
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        # Проверяем только callback queries
        if isinstance(event, CallbackQuery):
            user_id = event.from_user.id
            callback_data = event.data or ""
            
            # Исключаем callback "captcha_answer_" и "restart_deleted_user" - они обрабатываются отдельно и могут создавать пользователя
            # Проверяем это ДО проверки пользователя в БД
            if callback_data.startswith("captcha_answer_") or callback_data == "restart_deleted_user":
                # Пропускаем эти callbacks без проверки пользователя
                return await handler(event, data)
            
            # Проверяем существование пользователя в БД
            user_data = await db_helpers.get_user(user_id)
            
            if not user_data:
                # Пользователя нет в БД - показываем сообщение с кнопкой "Старт"
                logger.info(f"[MIDDLEWARE] Пользователь {user_id} не найден в БД, callback_data={callback_data}")
                try:
                    deleted_message_text = TXT_USER_DELETED
                    
                    # Создаем клавиатуру с кнопкой "Старт"
                    start_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="Старт", callback_data="restart_deleted_user")]
                    ])
                    
                    # Пытаемся отредактировать сообщение, если это возможно
                    try:
                        await event.message.edit_text(
                            deleted_message_text,
                            reply_markup=start_keyboard,
                            parse_mode="HTML"
                        )
                    except Exception:
                        # Если не удалось отредактировать, отправляем новое сообщение
                        await event.message.answer(
                            deleted_message_text,
                            reply_markup=start_keyboard,
                            parse_mode="HTML"
                        )
                    
                    await event.answer("Для продолжения нажмите кнопку \"Старт\"", show_alert=True)
                    return  # Прерываем обработку callback
                except Exception as e:
                    logger.error(f"[MIDDLEWARE] Ошибка при обработке удаленного пользователя {user_id}: {e}", exc_info=True)
        
        # Если пользователь существует или это не callback query - продолжаем обработку
        return await handler(event, data)

# --- Middleware для rate limiting (ограничение частоты запросов) ---
class ThrottlingMiddleware(BaseMiddleware):
    """Middleware для ограничения частоты обработки сообщений и callback queries"""
    
    def __init__(self):
        # Словарь для хранения времени последних запросов: {user_id: [timestamps]}
        self._user_requests: Dict[int, list] = {}
        # Блокировка для потокобезопасности
        self._lock = asyncio.Lock()
        # Время жизни записей (очистка старых записей каждые 5 минут)
        self._last_cleanup = time.time()
        self._cleanup_interval = 300  # 5 минут
        
    async def _is_admin(self, user_id: int) -> bool:
        """Проверяет, является ли пользователь администратором"""
        from src.core.utils import is_bot_admin
        return is_bot_admin(user_id)
    
    async def _cleanup_old_requests(self, window_seconds: int):
        """Очищает старые записи из словаря запросов"""
        current_time = time.time()
        if current_time - self._last_cleanup < self._cleanup_interval:
            return
        
        async with self._lock:
            cutoff_time = current_time - window_seconds
            users_to_remove = []
            
            for user_id, timestamps in self._user_requests.items():
                # Оставляем только свежие запросы
                self._user_requests[user_id] = [ts for ts in timestamps if ts > cutoff_time]
                # Удаляем пустые списки
                if not self._user_requests[user_id]:
                    users_to_remove.append(user_id)
            
            for user_id in users_to_remove:
                del self._user_requests[user_id]
            
            self._last_cleanup = current_time
    
    async def _check_rate_limit(
        self, 
        user_id: int, 
        max_requests: int, 
        window_seconds: int,
        is_callback: bool = False
    ) -> bool:
        """
        Проверяет, не превышен ли лимит запросов.
        Возвращает True, если запрос разрешен, False - если превышен лимит.
        """
        # Админы не ограничены
        if await self._is_admin(user_id):
            return True
        
        # Очищаем старые записи периодически
        await self._cleanup_old_requests(window_seconds)
        
        current_time = time.time()
        cutoff_time = current_time - window_seconds
        
        async with self._lock:
            if user_id not in self._user_requests:
                self._user_requests[user_id] = []
            
            # Удаляем старые запросы из окна
            self._user_requests[user_id] = [
                ts for ts in self._user_requests[user_id] 
                if ts > cutoff_time
            ]
            
            # Проверяем лимит
            if len(self._user_requests[user_id]) >= max_requests:
                return False
            
            # Добавляем текущий запрос
            self._user_requests[user_id].append(current_time)
            return True
    
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        # Получаем настройки rate limiting из конфига
        rate_limit_enabled = str(app_conf.get('bot_rate_limit_enabled', '1')) == '1'
        
        if not rate_limit_enabled:
            return await handler(event, data)
        
        # Определяем тип события и параметры лимита
        is_callback = isinstance(event, CallbackQuery)
        is_message = isinstance(event, Message)
        
        if not (is_callback or is_message):
            return await handler(event, data)
        
        user_id = event.from_user.id if hasattr(event, 'from_user') else None
        if not user_id:
            return await handler(event, data)
        
        # Исключаем некоторые callback из ограничений
        if is_callback:
            callback_data = event.data or ""
            # Исключаем captcha и restart_deleted_user (они уже обрабатываются в UserExistsMiddleware)
            if callback_data.startswith("captcha_answer_") or callback_data == "restart_deleted_user":
                return await handler(event, data)

        # Ответы в начатом диалоге — не флуд: их спросили мы сами. Оформление
        # заказа это пять сообщений подряд (ФИО, телефон, город, адрес,
        # промокод), и на общем лимите клиент упирался в «слишком много»
        # раньше, чем доходил до адреса. Ограничение остаётся там, где оно
        # и нужно: на сообщениях вне диалога.
        state = data.get('state')
        if state is not None:
            try:
                if await state.get_state() is not None:
                    return await handler(event, data)
            except Exception:
                # Хранилище состояний не ответило — считаем обычным сообщением
                # и проверяем лимит. Пропустить проверку молча нельзя.
                pass

        # Получаем параметры лимита из настроек
        if is_callback:
            max_requests = int(app_conf.get('bot_rate_limit_callback_max', '30'))  # По умолчанию 30 запросов
            window_seconds = int(app_conf.get('bot_rate_limit_callback_window', '60'))  # За 60 секунд
        else:
            max_requests = int(app_conf.get('bot_rate_limit_message_max', '10'))  # По умолчанию 10 сообщений
            window_seconds = int(app_conf.get('bot_rate_limit_message_window', '60'))  # За 60 секунд
        
        # Проверяем лимит
        if not await self._check_rate_limit(user_id, max_requests, window_seconds, is_callback):
            # Лимит превышен
            if is_callback:
                try:
                    await event.answer(
                        "⏳ Слишком много запросов. Пожалуйста, подождите немного.",
                        show_alert=False
                    )
                except Exception:
                    pass
            else:
                try:
                    rate_limit_text = app_conf.get(
                        'bot_rate_limit_message_text',
                        '⏳ Слишком много сообщений. Пожалуйста, подождите немного перед следующим запросом.'
                    )
                    await event.answer(rate_limit_text)
                except Exception:
                    pass
            
            logger.debug(f"[RATE_LIMIT] Лимит превышен для пользователя {user_id} ({'callback' if is_callback else 'message'})")
            return  # Прерываем обработку
        
        # Лимит не превышен - продолжаем обработку
        return await handler(event, data)

# Регистрируем middleware
throttling_middleware = ThrottlingMiddleware()
dp.message.middleware(throttling_middleware)
dp.callback_query.middleware(throttling_middleware)
dp.callback_query.middleware(UserExistsMiddleware())
register_maintenance(dp)

# Словарь для хранения активных задач проверки платежей
active_payment_checkers = {}

# --- Вспомогательная функция для сохранения настроек в БД (асинхронно) ---
async def set_setting_value(key: str, value: str) -> None:
    try:
        # Если значение не меняется — не трогаем БД и не перезагружаем кэш
        try:
            current_val = app_conf.get(key, None)
            if current_val == value:
                return
        except Exception:
            pass
        async with db_helpers.get_db_connection_safe() as db:
            # Обновляем, если есть; иначе вставляем
            cur = await db.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
            if cur.rowcount == 0:
                await db.execute(
                    "INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)",
                    (key, value, '')
                )
            await db.commit()
        # Обновляем кэш настроек
        try:
            await app_conf.load_settings()
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"set_setting_value error for {key}: {e}")

def _payment_grant_failed_text() -> str:
    return (app_conf.get("text_payment_grant_failed") or TXT_PAYMENT_GRANT_FAILED).strip()


async def _notify_payment_grant_failed(
    telegram_user_id: int,
    *,
    registration_type: str | None = None,
) -> None:
    if not telegram_user_id:
        return
    if registration_type == "site":
        return
    try:
        await bot.send_message(
            telegram_user_id,
            _payment_grant_failed_text(),
            reply_markup=keyboards.get_back_to_main_keyboard(),
        )
    except Exception as e:
        logger.warning(
            f"Не удалось уведомить user={telegram_user_id} об ошибке выдачи подписки: {e}"
        )


def _traffic_grant_failed_text() -> str:
    return (app_conf.get("text_payment_traffic_grant_failed") or TXT_PAYMENT_TRAFFIC_GRANT_FAILED).strip()


async def _notify_traffic_grant_failed(telegram_user_id: int) -> None:
    if not telegram_user_id:
        return
    try:
        await bot.send_message(
            telegram_user_id,
            _traffic_grant_failed_text(),
            reply_markup=keyboards.get_back_to_main_keyboard(),
        )
    except Exception as e:
        logger.warning(
            f"Не удалось уведомить user={telegram_user_id} об ошибке докупки трафика: {e}"
        )


async def _apply_referral_join_bonus(invited_user_id: int, *, log_prefix: str = "") -> None:
    """Обёртка над src.referral_join_bonus.try_grant_referral_join_bonus."""
    from src.referral_join_bonus import try_grant_referral_join_bonus
    await try_grant_referral_join_bonus(
        invited_user_id=invited_user_id,
        bot=bot,
        db_helpers=db_helpers,
        app_conf=app_conf,
        keyboards=keyboards,
        grant_subscription=grant_subscription,
        resolve_limit_ip_for_user=_resolve_limit_ip_for_user,
        log_prefix=log_prefix or "Remnawave, user.first_connected",
    )


async def _apply_partner_and_referral(
    payer_user_id: int,
    payment_id: str,
    amount_rub: float,
    currency: str,
    log_prefix: str,
) -> None:
    """Тонкая обёртка над src.pay.partner.credit_partner_and_referral —
    подставляет локальные зависимости (bot, db_helpers, app_conf, keyboards,
    grant_subscription, _resolve_limit_ip_for_user). Используется из всех
    webhook-обработчиков и process_successful_payment, чтобы партнёрка/
    реферал начислялись одинаково для всех платёжных систем (YooKassa,
    Platega, CryptoBot, TG Stars, YooMoney).
    """
    from src.pay.partner import credit_partner_and_referral
    await credit_partner_and_referral(
        payer_user_id=payer_user_id,
        payment_id=payment_id,
        amount_rub=amount_rub,
        currency=currency,
        bot=bot,
        db_helpers=db_helpers,
        app_conf=app_conf,
        keyboards=keyboards,
        grant_subscription=grant_subscription,
        resolve_limit_ip_for_user=_resolve_limit_ip_for_user,
        log_prefix=log_prefix,
    )


# Определяет корректный limit_ip для пользователя, чтобы не сбрасывать в "без лимита"
async def _resolve_limit_ip_for_user(user_id: int) -> int:
    """
    Определяет корректный limit_ip для пользователя.
    Проверяет сначала активную подписку, потом последнюю подписку.
    Если limit_ip не найден или равен None, возвращает фолбэк 1.
    Если limit_ip равен 0, возвращает 0 (без лимита).
    """
    try:
        # Сначала проверяем активную подписку
        active_sub = await db_helpers.get_active_subscription(user_id)
        if active_sub:
            limit_ip = active_sub.get('limit_ip')
            # Проверяем, что limit_ip не None и является числом
            if limit_ip is not None and isinstance(limit_ip, (int, float)):
                return int(limit_ip)
        
        # Если активной подписки нет или limit_ip не найден, проверяем последнюю подписку
        last_sub = await db_helpers.get_last_subscription(user_id)
        if last_sub:
            limit_ip = last_sub.get('limit_ip')
            # Проверяем, что limit_ip не None и является числом
            if limit_ip is not None and isinstance(limit_ip, (int, float)):
                return int(limit_ip)
    except Exception as e:
        logger.warning(f"Ошибка при определении limit_ip для пользователя {user_id}: {e}")
    
    # Фолбэк — 1 устройство (если limit_ip не найден или равен None)
    return 1


# Как website/run.py (cabinet /pay): окно смены лимита при продлении
TARIFF_CHANGE_WINDOW_DAYS = 7


def _tariff_limit_same_or_higher_for_renewal(t_limit: int, user_limit: int) -> bool:
    """
    Разрешён ли тариф с лимитом t_limit клиенту с текущим user_limit при продлении
    в «окне» (без понижения). 0 в БД = безлимит (∞), считается не ниже числового.
    """
    if user_limit <= 0:
        return t_limit <= 0
    if t_limit <= 0:
        return True
    return t_limit >= user_limit


def _renewal_pick_other_limits_from_ctx(ctx: dict) -> bool:
    """Можно ли показывать выбор других лимитов (≥ текущего), без понижения."""
    if not ctx.get('has_history') or ctx.get('user_limit') is None:
        return False
    ul = int(ctx['user_limit'])
    if ul <= 0:
        return False
    if ctx.get('is_first_purchase'):
        return False
    return bool(ctx.get('renewal_window_open'))


async def _renewal_limit_ui_context(user_id: int) -> dict:
    """Подписка, лимит и окно продления (как website/run.py)."""
    ctx = {
        'has_history': False,
        'user_limit': None,
        'has_subscription': False,
        'days_until_end': None,
        'is_first_purchase': True,
        'renewal_window_open': False,
    }
    active = None
    last = None
    try:
        active = await db_helpers.get_active_subscription(user_id)
        if active:
            ctx['has_history'] = True
            lim = active.get('limit_ip')
            if lim is not None:
                ctx['user_limit'] = int(lim)
        if ctx['user_limit'] is None:
            last = await db_helpers.get_last_subscription(user_id)
            if last:
                ctx['has_history'] = True
                lim = last.get('limit_ip')
                if lim is not None:
                    ctx['user_limit'] = int(lim)
    except Exception as e:
        logger.warning(f"_renewal_limit_ui_context: подписки {user_id}: {e}")

    ul = ctx['user_limit']
    sub_end_raw = None
    try:
        if active and active.get('subscription_end_date'):
            sub_end_raw = active.get('subscription_end_date')
        else:
            if last is None:
                last = await db_helpers.get_last_subscription(user_id)
            if last and last.get('subscription_end_date'):
                sub_end_raw = last.get('subscription_end_date')
    except Exception:
        sub_end_raw = None

    has_subscription = bool(sub_end_raw)
    ctx['has_subscription'] = has_subscription
    days_until_end = None
    if has_subscription:
        try:
            end_dt = datetime.fromisoformat(str(sub_end_raw).replace('Z', '+00:00'))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            days_until_end = (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
        except Exception:
            days_until_end = None
    ctx['days_until_end'] = days_until_end

    if ul is None:
        return ctx

    uli = int(ul)
    is_first_purchase = (not has_subscription) or (uli <= 0)
    ctx['is_first_purchase'] = is_first_purchase
    if not is_first_purchase:
        ctx['renewal_window_open'] = (
            days_until_end is None or days_until_end <= TARIFF_CHANGE_WINDOW_DAYS
        )
    return ctx


async def _renewal_window_allows_other_device_limits(user_id: int) -> bool:
    """Удобная обёртка там, где контекст не предзагружен."""
    return _renewal_pick_other_limits_from_ctx(await _renewal_limit_ui_context(user_id))


async def _filter_tariffs_by_user_limit(
    user_id: int,
    filtered: list,
    *,
    ctx: Optional[dict] = None,
) -> tuple[list, int | None, bool, str]:
    """
    Тарифы для основного экрана продления: только с текущим limit_ip клиента.

    Варианты с большим числом устройств — через кнопку «📱 Изменить лимит устройств»
    (если разрешено окном продления или выключен pro-rata-режим).

    Возвращает (visible, target_limit, is_newbie, extra_html_hint).
    """
    if ctx is None:
        ctx = await _renewal_limit_ui_context(user_id)

    has_history = ctx.get('has_history', False)
    user_limit = ctx.get('user_limit')

    if has_history and user_limit is not None:
        ul = int(user_limit)

        if not ctx.get('is_first_purchase'):
            visible = [
                t for t in filtered
                if int(t.get('limit_ip', 0) or 0) == ul
            ]
            hint = ""
            if ctx.get('renewal_window_open') and ul > 0:
                hint = (
                    "\n\n<blockquote>"
                    "Другой лимит устройств — кнопка "
                    "<b>Изменить лимит</b>.\n"
                    "<i>Понизить лимит через поддержку.</i>"
                    "</blockquote>"
                )
            return visible, ul, False, hint

        if ul <= 0:
            visible = [
                t for t in filtered
                if int(t.get('limit_ip', 0) or 0) == ul
            ]
            return visible, ul, False, ""

    try:
        all_limits = sorted(set(int(t.get('limit_ip', 0) or 0) for t in filtered))
    except Exception:
        all_limits = []
    target = all_limits[0] if all_limits else 0
    visible = [t for t in filtered if int(t.get('limit_ip', 0) or 0) == target]
    return visible, target, True, ""


def _format_device_limit_label(limit: int) -> str:
    """Подпись лимита устройств для кнопок продления."""
    if limit == 0:
        return "Без лимита"
    return f"Лимит: {limit}"


def _no_tariffs_for_limit_text(target_limit: int | None) -> str:
    """Текст-заглушка, когда под лимит клиента нет активных тарифов."""
    if target_limit and target_limit > 0:
        limit_str = f"{target_limit} устройств"
    elif target_limit == 0:
        limit_str = "без лимита"
    else:
        limit_str = "ваш лимит"
    return (
        "<b>Продление подписки</b>\n"
        f"✕ Нет вариантов для лимита <b>{limit_str}</b>\n\n"
        "Напишите в поддержку — поможем подобрать срок."
    )

# --- Хэш файла для инвалидации кеша file_id ---
def _file_sha256(path: str) -> str:
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ''

# --- Состояния FSM для aiogram ---
class PromoCodeActivation(StatesGroup):
    waiting_for_code = State()

class StepByStepGuide(StatesGroup):
    step1 = State()
    step2 = State()
    step3 = State()
    step4 = State()
    step5 = State()

class BotProtection(StatesGroup):
    waiting_for_answer = State()

class WebsiteEmailLink(StatesGroup):
    waiting_email = State()
    waiting_code  = State()

# --- Вспомогательные функции ---
async def check_user_blocked(user_id: int) -> bool:
    """Проверяет, заблокирован ли пользователь"""
    try:
        user_data = await db_helpers.get_user(user_id)
        if user_data:
            is_blocked = user_data['is_blocked'] if 'is_blocked' in user_data.keys() else 0
            return bool(is_blocked)
    except Exception as e:
        logger.error(f"Ошибка проверки блокировки пользователя {user_id}: {e}")
    return False

def generate_captcha_question():
    """Генерирует простой математический вопрос для защиты от ботов"""
    operations = [
        ('+', lambda a, b: a + b),
        ('-', lambda a, b: a - b),
        ('*', lambda a, b: a * b)
    ]
    
    # Выбираем операцию
    op_symbol, op_func = random.choice(operations)
    
    # Генерируем числа (для умножения используем меньшие числа)
    if op_symbol == '*':
        a = random.randint(2, 9)
        b = random.randint(2, 9)
    elif op_symbol == '-':
        # Для вычитания гарантируем положительный результат
        a = random.randint(5, 20)
        b = random.randint(1, a - 1)
    else:  # сложение
        a = random.randint(1, 20)
        b = random.randint(1, 20)
    
    # Вычисляем правильный ответ
    correct_answer = op_func(a, b)
    
    # Формируем вопрос
    question = f"{a} {op_symbol} {b} = ?"
    
    logger.info(f"[CAPTCHA] Сгенерирован вопрос: {question}, правильный ответ: {correct_answer}")
    
    return question, str(correct_answer)

async def send_blocked_message(user_id: int, query: CallbackQuery = None):
    """Отправляет сообщение о блокировке"""
    blocked_text = TXT_BLOCKED
    support_link = app_conf.get('support_link')
    if support_link:
        blocked_text += f"\n\n<a href='{support_link}'>Написать в поддержку</a>"
    
    if query:
        try:
            await query.message.edit_text(blocked_text, disable_web_page_preview=True)
            await query.answer("Ваш аккаунт заблокирован")
        except:
            await bot.send_message(user_id, blocked_text, disable_web_page_preview=True)
    else:
        await bot.send_message(user_id, blocked_text, disable_web_page_preview=True)

async def process_successful_payment(telegram_user_id: int, payment_id: str, payment_metadata: Optional[dict] = None):
    # Проверяем блокировку пользователя
    if await check_user_blocked(telegram_user_id):
        logger.warning(f"Попытка обработки платежа {payment_id} для заблокированного пользователя {telegram_user_id}")
        await db_helpers.update_payment_status(payment_id, "blocked")
        await send_blocked_message(telegram_user_id)
        return False
        
    logger.info(f"Обработка успешного платежа {payment_id} для пользователя {telegram_user_id}")
    db_payment_info = await db_helpers.get_payment(payment_id)
    
    # Если payment_metadata не передан, пытаемся получить из БД
    if not payment_metadata and db_payment_info:
        try:
            metadata_json = db_payment_info[6] if len(db_payment_info) > 6 else None  # metadata_json находится на индексе 6
            if metadata_json:
                payment_metadata = json.loads(metadata_json)
                logger.info(f"Платеж {payment_id}: payment_metadata загружен из БД: {payment_metadata}")
        except (json.JSONDecodeError, TypeError, IndexError) as e:
            logger.warning(f"Платеж {payment_id}: не удалось загрузить payment_metadata из БД: {e}")
    
    if db_payment_info and db_payment_info[4] == "succeeded":
        logger.info(f"Платеж {payment_id} уже был обработан как 'succeeded'.")
        
        # Проверяем тип платежа для уже обработанных платежей
        payment_type = payment_metadata.get('payment_type') if payment_metadata else None
        logger.info(f"Платеж {payment_id}: payment_type из metadata = {payment_type}")
        # ── 'traffic_reset' (платный сброс трафика отдельной кнопкой) удалён.
        # Автоматический сброс при продлении подписки в «No Limit+» делается
        # в grant_subscription через reset_traffic_on_renewal=True — это
        # отдельная логика, к этому блоку отношения не имеет.

        if payment_type == 'traffic_renewal':
            # Проверяем, был ли действительно продлен трафик
            # Если нет - пытаемся продлить снова
            logger.info(f"Платеж {payment_id} уже был обработан как 'traffic_renewal'. Проверяем, был ли продлен трафик...")
            from remnawave_manager import remnawave_manager_instance
            
            user_data = await db_helpers.get_user(telegram_user_id)
            if user_data:
                user_dict = dict(user_data)
                remnawave_uuid = user_dict.get('xui_client_uuid')
                if remnawave_uuid:
                        # Получаем количество трафика для добавления из метаданных
                        traffic_to_add_gb = payment_metadata.get('traffic_to_add_gb', 0) if payment_metadata else 0
                        if traffic_to_add_gb <= 0:
                            # Если не указано в метаданных, берем из настроек
                            traffic_to_add_gb = get_default_limit_gb()
                        
                        if traffic_to_add_gb > 0:
                            logger.info(f"Платеж {payment_id}: пытаемся продлить трафик для UUID {remnawave_uuid}, добавить {traffic_to_add_gb} GB")
                            renewal_result = await remnawave_manager_instance.update_user_subscription(
                                remnawave_uuid,
                                days_to_add=0,  # Не продлеваем срок подписки
                                traffic_to_add_gb=traffic_to_add_gb,
                                apply_default_squad=True  # Применяем squad из настроек при покупке гигабайт (повторная обработка)
                            )
                            if renewal_result:
                                logger.success(f"Платеж {payment_id}: трафик успешно продлен при повторной обработке (+{traffic_to_add_gb} GB)")
                                # Получаем информацию о трафике для отображения
                                remnawave_short_uuid = user_dict.get('remnawave_short_uuid')
                                traffic_info = ""
                                if remnawave_short_uuid:
                                    try:
                                        remnawave_data = await remnawave_manager_instance.get_subscription_info(remnawave_short_uuid)
                                        if remnawave_data:
                                            traffic_info = format_traffic_inline(
                                                remnawave_data.get('trafficUsedBytes', 0),
                                                remnawave_data.get('trafficLimitBytes', 0),
                                            )
                                            if traffic_info:
                                                traffic_info = f"\n\nТрафик: {traffic_info}"
                                    except Exception as e:
                                        logger.warning(f"Не удалось получить информацию о трафике: {e}")
                                
                                # Обновляем статус платежа на 'succeeded' после успешного продления трафика
                                await db_helpers.update_payment_status(payment_id, 'succeeded')
                                
                                try:
                                    await bot.send_message(
                                        telegram_user_id,
                                        f"<b>Трафик добавлен</b>\n✓ Добавлено {traffic_to_add_gb} GB{traffic_info}",
                                        reply_markup=keyboards.get_back_to_main_keyboard()
                                    )
                                except Exception as _tg_err:
                                    logger.warning(f"Платеж {payment_id}: не удалось уведомить пользователя {telegram_user_id}: {_tg_err}")
                                return True
                            else:
                                logger.error(f"Платеж {payment_id}: не удалось продлить трафик при повторной обработке")
                                await _notify_traffic_grant_failed(telegram_user_id)
                                return False
                        else:
                            logger.error(f"Платеж {payment_id}: не указано количество трафика для продления")
            # Если не удалось продлить, просто возвращаем True (платеж уже обработан)
            logger.warning(f"Платеж {payment_id}: не удалось выполнить повторное продление трафика")
            return True
        
        # Повторно отправляем сообщение об успехе, если пользователь нажал кнопку проверки еще раз
        active_sub = await db_helpers.get_active_subscription(telegram_user_id)
        if active_sub:
            days_paid = app_conf.get('subscription_days', 30)
            if payment_metadata and 'subscription_days' in payment_metadata:
                days_paid = payment_metadata['subscription_days']

            expiry_date = active_sub['subscription_end_date']
            tpl = (app_conf.get('text_payment_success') or '').replace('{sub_link}', '')
            await bot.send_message(
                telegram_user_id,
                tpl.format(days=days_paid, expiry_date=format_msk_date(expiry_date)),
                reply_markup=keyboards.get_success_with_referral_keyboard()
            )
            # Партнёрка + реферальный бонус — единый хелпер для всех платёжек.
            try:
                amount_rub = float(db_payment_info[2] or 0)
                currency = (db_payment_info[3] or '').upper()
                await _apply_partner_and_referral(
                    payer_user_id=telegram_user_id,
                    payment_id=payment_id,
                    amount_rub=amount_rub,
                    currency=currency,
                    log_prefix="YooKassa, повтор",
                )
            except Exception as e:
                logger.error(f"Партнёрка/реферал (YooKassa, повтор) ошибка: {e}")
        return True

    await db_helpers.update_payment_status(payment_id, "succeeded")
    
    # Проверяем тип платежа: сброс трафика, продление трафика или продление подписки
    payment_type = payment_metadata.get('payment_type') if payment_metadata else None
    logger.info(f"Платеж {payment_id}: обработка нового платежа, payment_type = {payment_type}, payment_metadata = {payment_metadata}")

    if payment_type == 'traffic_renewal':
        # Обработка продления трафика для Remnawave
        from remnawave_manager import remnawave_manager_instance
        
        user_data = await db_helpers.get_user(telegram_user_id)
        if not user_data:
            logger.error(f"Пользователь {telegram_user_id} не найден")
            await bot.send_message(telegram_user_id, "✕ Пользователь не найден")
            return False
        
        # Преобразуем Row в словарь
        user_dict = dict(user_data)
        remnawave_uuid = user_dict.get('xui_client_uuid')
        if not remnawave_uuid:
            logger.error(f"Попытка продления трафика для пользователя {telegram_user_id} без UUID подписки")
            await bot.send_message(telegram_user_id, "✕ Подписка не найдена")
            return False
        
        # Получаем количество трафика для добавления из метаданных
        traffic_to_add_gb = payment_metadata.get('traffic_to_add_gb', 0)
        if traffic_to_add_gb <= 0:
            # Если не указано в метаданных, берем из настроек
            traffic_to_add_gb = get_default_limit_gb()
        
        if traffic_to_add_gb <= 0:
            logger.error(f"Не указано количество трафика для продления для пользователя {telegram_user_id}")
            await bot.send_message(telegram_user_id, "✕ Не указан объём трафика")
            return False
        
        # Продлеваем трафик (добавляем к текущему лимиту, без продления срока подписки)
        # При покупке гигабайт применяем squad из настроек (это платная операция)
        renewal_result = await remnawave_manager_instance.update_user_subscription(
            remnawave_uuid,
            days_to_add=0,  # Не продлеваем срок подписки
            traffic_to_add_gb=traffic_to_add_gb,
            apply_default_squad=True  # Применяем squad из настроек при покупке гигабайт
        )
        
        if renewal_result:
            # Получаем информацию о трафике для отображения
            remnawave_short_uuid = user_dict.get('remnawave_short_uuid')
            traffic_info = ""
            if remnawave_short_uuid:
                try:
                    remnawave_data = await remnawave_manager_instance.get_subscription_info(remnawave_short_uuid)
                    if remnawave_data:
                        inline = format_traffic_inline(
                            remnawave_data.get('trafficUsedBytes', 0),
                            remnawave_data.get('trafficLimitBytes', 0),
                        )
                        traffic_info = f"\n\nТрафик: {inline}" if inline else ""
                except Exception as e:
                    logger.warning(f"Не удалось получить информацию о трафике: {e}")
            
            # Обновляем статус платежа на 'succeeded' после успешного продления трафика
            await db_helpers.update_payment_status(payment_id, 'succeeded')
            
            try:
                await bot.send_message(
                    telegram_user_id,
                    f"<b>Трафик добавлен</b>\n✓ Добавлено {traffic_to_add_gb} GB{traffic_info}",
                    reply_markup=keyboards.get_back_to_main_keyboard()
                )
            except Exception as _tg_err:
                logger.warning(f"Платеж {payment_id}: не удалось уведомить пользователя {telegram_user_id}: {_tg_err}")
            return True
        else:
            await _notify_traffic_grant_failed(telegram_user_id)
            await db_helpers.update_payment_status(payment_id, 'failed')
            return False
    
    # Обработка продления подписки (существующая логика)
    days_to_add = app_conf.get('subscription_days', 30)
    price_to_use = None
    limit_ip = 0
    if payment_metadata and 'subscription_days' in payment_metadata:
        days_to_add = payment_metadata['subscription_days']
        price_to_use = float(payment_metadata.get('price', 0)) if 'price' in payment_metadata else None
        # Получаем лимит устройств из тарифа
        active_tariffs = await db_helpers.get_active_tariffs()
        if price_to_use is not None:
            for tariff in active_tariffs:
                if tariff['days'] == days_to_add and float(tariff['price']) == price_to_use:
                    limit_ip = tariff.get('limit_ip', 0)
                    break
        else:
            for tariff in active_tariffs:
                if tariff['days'] == days_to_add:
                    limit_ip = tariff.get('limit_ip', 0)
                    break
    
    # Получаем traffic_gb из тарифа, если указан tariff_id
    traffic_gb_to_add = 0
    if payment_metadata and 'tariff_id' in payment_metadata:
        tariff_id = payment_metadata.get('tariff_id')
        if tariff_id:
            try:
                tariff = await db_helpers.get_tariff_by_id(tariff_id)
                if tariff:
                    traffic_gb_to_add = tariff.get('traffic_gb', 0) or 0
                    if traffic_gb_to_add > 0:
                        logger.info(f"При продлении подписки будет добавлено {traffic_gb_to_add} GB из тарифа {tariff_id}")
            except Exception as e:
                logger.warning(f"Не удалось получить traffic_gb из тарифа {tariff_id}: {e}")
    
    subscription_data = await grant_subscription(telegram_user_id, days_to_add, is_trial=False, limit_ip=limit_ip, traffic_gb_to_add=traffic_gb_to_add)
    
    if subscription_data:
        # Отправляем основное сообщение об успешном платеже
        tpl = (app_conf.get('text_payment_success') or '').replace('{sub_link}', '')
        await bot.send_message(
            telegram_user_id,
            tpl.format(days=days_to_add, expiry_date=format_msk_date(subscription_data['expiry_date'])),
            reply_markup=keyboards.get_success_with_referral_keyboard()
        )
        # Партнёрка + реферальный бонус — единый хелпер.
        try:
            db_payment_info2 = await db_helpers.get_payment(payment_id)
            amount_rub = float(db_payment_info2[2] or 0) if db_payment_info2 else 0.0
            currency = (db_payment_info2[3] or '').upper() if db_payment_info2 else 'RUB'
            await _apply_partner_and_referral(
                payer_user_id=telegram_user_id,
                payment_id=payment_id,
                amount_rub=amount_rub,
                currency=currency,
                log_prefix="YooKassa",
            )
        except Exception as e:
            logger.error(f"Партнёрка/реферал (YooKassa) ошибка: {e}")
        return True
    else:
        logger.error(
            f"Не удалось выдать подписку после успешного платежа {payment_id} "
            f"(оплачен, подписка не выдана) → failed"
        )
        await db_helpers.update_payment_status(payment_id, 'failed')
        await _notify_payment_grant_failed(telegram_user_id)
        return False

# Запасные тексты главного меню. Настройку можно стереть в админке, а ключа
# может не быть в базе вовсе: у поставщика эти строки жили в поставляемой
# базе и в код не попали. None тут разваливает .format() и склейку строк,
# и падает не текст, а всё главное меню целиком.
DEFAULT_WELCOME = (
    "<b>{project_name}</b>\n\n"
    "Роутер работает сразу: включили в розетку — и зарубежные сервисы "
    "открываются как обычные."
)
DEFAULT_SUBSCRIPTION_INFO = """✓ Подписка активна до <b>{expiry_date}</b>"""
DEFAULT_SUBSCRIPTION_EXPIRED = "⚠ Подписка не активна\n\nПродлите её, чтобы роутер снова вышел в сеть."
DEFAULT_ABOUT_SERVICE = REST_TEXT_DEFAULTS['text_about_service']
DEFAULT_PROMO_SUCCESS = REST_TEXT_DEFAULTS['text_promo_code_success']


def _filter_empty_menu_fields(text: str) -> str:
    """Убирает из операторского шаблона поля без подставленного значения."""
    filtered_lines: list[str] = []
    for line in text.splitlines():
        plain_line = re.sub(r"<[^>]+>", "", line).strip()
        if plain_line == "—":
            continue
        if ":" in plain_line and plain_line.split(":", 1)[1].strip() in {"", "—"}:
            continue
        filtered_lines.append(line)
    return "\n".join(filtered_lines).strip()


CAPTION_LIMIT = 1024
"""Telegram обрезает подпись к фото на этой длине.

Меню с блоком подписки в неё обычно влезает, но не всегда: у клиента с
трафиком и длинной датой бывает длиннее. Обрезать меню нельзя — уедет
часть текста, поэтому такое меню уходит текстом с превью."""


def main_menu_photo() -> str:
    """Адрес картинки над главным меню. Пусто — картинки нет."""
    return (app_conf.get('main_menu_photo_url', '') or '').strip()


def main_menu_preview() -> LinkPreviewOptions:
    """Запасной способ показать картинку — ссылкой с большим превью.

    Используется, когда фото-сообщением не выходит: подпись длиннее предела
    или Telegram не смог забрать картинку. Выглядит как ссылка с картинкой,
    а не как пост, но лучше, чем меню вовсе без неё.
    """
    url = main_menu_photo()
    if not url:
        return LinkPreviewOptions(is_disabled=True)
    return LinkPreviewOptions(url=url, prefer_large_media=True, show_above_text=True)


async def send_main_menu_photo(user_id: int, text: str, kbd, previous) -> bool:
    """Шлёт меню фото-сообщением: картинка сверху, текст подписью.

    Так это выглядит обычным постом, а не ссылкой с превью. Цена — экран
    переезжает вниз чата: превратить текстовое сообщение в сообщение
    с фото Telegram не даёт, поэтому старое приходится удалять.

    Сначала шлём новое, потом удаляем старое. В обратном порядке сбой
    отправки оставил бы клиента вообще без меню.
    """
    url = main_menu_photo()
    if not url or len(text) > CAPTION_LIMIT:
        return False

    try:
        await bot.send_photo(chat_id=user_id, photo=url, caption=text, reply_markup=kbd)
    except Exception as e:
        if is_premium_emoji_refusal(e):
            # Премиум-эмодзи боту не отдали — шлём тот же текст без них.
            try:
                await bot.send_photo(
                    chat_id=user_id,
                    photo=url,
                    caption=without_premium_emoji(text),
                    reply_markup=kbd,
                )
            except Exception as retry_error:
                logger.warning(f"[MENU] баннер не отправился ({url}): {retry_error}")
                return False
        else:
            # Битая ссылка, недоступный домен, неподдерживаемый формат — меню
            # всё равно должно открыться, пусть и текстом.
            logger.warning(f"[MENU] баннер не отправился ({url}): {e}")
            return False

    if previous is not None:
        try:
            await previous.delete()
        except Exception:
            # Сообщение старше двух суток удалить нельзя — не беда,
            # новое меню уже отправлено.
            pass
    return True


PREMIUM_EMOJI_RE = re.compile(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", re.IGNORECASE | re.DOTALL)


def without_premium_emoji(text: str) -> str:
    """Убирает премиум-эмодзи, оставляя обычный внутри тега.

    Премиум-эмодзи в сообщениях доступны не всякому боту: Telegram отдаёт
    их только тем, кто купил дополнительный username на Fragment. Остальным
    он отвечает отказом на всё сообщение — и клиент остаётся без меню.

    Поэтому отправка идёт в два захода: сперва как написал оператор, а если
    Telegram отказал — тем же текстом без тегов. Внутри тега стоит обычный
    эмодзи, так что смысл сохраняется, теряется только вид.
    """
    return PREMIUM_EMOJI_RE.sub(r"", text or "")


def is_premium_emoji_refusal(error: Exception) -> bool:
    """Отказ Telegram именно из-за премиум-эмодзи, а не из-за чего-то ещё."""
    reason = str(error).lower()
    return "custom emoji" in reason or "custom_emoji" in reason


async def client_router_mac(user_id: int) -> str:
    """MAC роутера клиента для текстов бота. Пусто — роутера у него нет.

    Молчаливо: экран поддержки открывают, когда что-то уже не работает,
    и падать из-за недоступного каталога он не должен.
    """
    try:
        from src import shop_api
        data, error = await shop_api.my_router(user_id)
        if error:
            return ""
        router = data.get("router") or {}
        return str(router.get("mac") or "")
    except Exception as exc:  # noqa: BLE001 — причина в журнал, экран важнее
        logger.warning(f"[SUPPORT] MAC роутера не получен для {user_id}: {exc}")
        return ""


async def show_main_menu(message_or_query: Message | CallbackQuery, edit_message: bool = False):
    user_id = message_or_query.from_user.id
    user_name = (message_or_query.from_user.first_name or "")[:32]
    real_username = message_or_query.from_user.username or None

    await db_helpers.add_user(user_id, user_name, real_username) 
    user_db_data = await db_helpers.get_user(user_id)
    
    # Проверяем, заблокирован ли пользователь
    try:
        is_blocked = user_db_data['is_blocked'] if user_db_data and 'is_blocked' in user_db_data.keys() else 0
        is_blocked = bool(is_blocked)
    except (KeyError, TypeError):
        is_blocked = False
        
    if is_blocked:
        blocked_text = TXT_BLOCKED
        support_link = app_conf.get('support_link')
        if support_link:
            blocked_text += f"\n\n<a href='{support_link}'>Написать в поддержку</a>"
        
        target_message = message_or_query.message if isinstance(message_or_query, CallbackQuery) else message_or_query
        
        if edit_message and isinstance(message_or_query, CallbackQuery):
            try:
                await target_message.edit_text(blocked_text, disable_web_page_preview=True)
            except Exception as e:
                if "message is not modified" not in str(e).lower():
                    await bot.send_message(user_id, blocked_text, disable_web_page_preview=True)
        else:
            await target_message.answer(blocked_text, disable_web_page_preview=True)
        
        if isinstance(message_or_query, CallbackQuery):
            try: await message_or_query.answer("Ваш аккаунт заблокирован")
            except: pass
        return
    
    active_sub = await db_helpers.get_active_subscription(user_id)
    has_active_sub = active_sub is not None
    is_trial_used = bool(user_db_data.get('is_trial_used', 1)) if user_db_data else True

    # Получаем uuid для кнопки "Подключиться" и для кнопки меню
    sub_uuid = None
    if active_sub and active_sub.get('xui_client_uuid'):
        sub_uuid = active_sub['xui_client_uuid']
    else:
        # Если нет активной, пробуем взять последнюю (даже если истекла)
        last_sub = await db_helpers.get_last_subscription(user_id)
        if last_sub and last_sub.get('xui_client_uuid'):
            sub_uuid = last_sub['xui_client_uuid']

    # --- Установка кнопки меню (Menu Button) ---
    try:
        await bot.set_my_commands([BotCommand(command='start', description='Главное меню')], scope={'type': 'chat', 'chat_id': user_id})
        await bot.set_chat_menu_button(chat_id=user_id, menu_button=MenuButtonCommands())
    except Exception as e:
        logger.error(f"Не удалось установить кнопку меню для {user_id}: {e}")

    kbd = await keyboards.get_main_keyboard(not is_trial_used and not has_active_sub, has_active_sub, sub_uuid=sub_uuid, user_id=user_id)

    safe_user_name = html.escape(user_name)
    welcome_tpl = app_conf.get('text_welcome_message') or DEFAULT_WELCOME
    text_to_send = welcome_tpl.format(
        user_name=safe_user_name, project_name=app_conf.get('project_name') or ''
    )
    
    if active_sub:
        expiry_date = active_sub['subscription_end_date']
        formatted_expiry_date = format_msk_date_day(expiry_date)

        # Лимит устройств теперь из БД
        limit_ip = active_sub.get('limit_ip', 0) if isinstance(active_sub, dict) else 0
        limit_ip_display = str(limit_ip) if limit_ip > 0 else 'Без лимита'
        
        # Преобразуем user_db_data в словарь, если это Row
        user_dict = dict(user_db_data) if user_db_data else {}
        
        # Получаем информацию о трафике
        traffic_display = ""
        remnawave_short_uuid = user_dict.get('remnawave_short_uuid')
        if remnawave_short_uuid:
            try:
                remnawave_data = await remnawave_manager_instance.get_subscription_info(remnawave_short_uuid)
                if remnawave_data:
                    traffic_display = format_traffic_inline(
                        remnawave_data.get('trafficUsedBytes', 0),
                        remnawave_data.get('trafficLimitBytes', 0),
                    )
            except Exception as e:
                logger.warning(f"Не удалось получить информацию о трафике Remnawave для главного экрана: {e}")

        base_sub = (app_conf.get('sub_page_url') or '').strip().rstrip('/')
        uuid_sub = (active_sub.get('xui_client_uuid') or '').strip()
        sub_link = f"{base_sub}/sub/{uuid_sub}" if base_sub and uuid_sub else ""

        # Значение по умолчанию тут, а не только в базе: настройку можно
        # стереть на странице текстов, и главное меню не должно от этого
        # падать у всех, кто оплатил.
        sub_info_tpl = app_conf.get('text_subscription_info') or DEFAULT_SUBSCRIPTION_INFO
        sub_info = sub_info_tpl.format(
            status="активна",
            expiry_date=formatted_expiry_date,
            limit_ip=limit_ip_display,
            traffic=traffic_display if traffic_display else "—",
            sub_link=sub_link,
        )
        sub_info = _filter_empty_menu_fields(sub_info)
        if sub_info:
            text_to_send += "\n\n" + sub_info
    elif is_trial_used and not has_active_sub:
         # Показываем стандартный текст для пользователей без активной подписки
         text_to_send += "\n\n" + (app_conf.get('text_subscription_expired_main') or DEFAULT_SUBSCRIPTION_EXPIRED)
    elif not is_trial_used and not has_active_sub:
        # Пробного периода нет — предлагать его нельзя: клиент нажмёт и ничего
        # не получит. Условие читает ту же настройку, поэтому решение обратимо.
        if int(app_conf.get('trial_days', 3) or 0) > 0:
            text_to_send += "\n\n" + "Пробный период доступен в меню ниже."
        else:
            text_to_send += "\n\n" + "Выберите раздел ниже или напишите в поддержку."

    target_message = message_or_query.message if isinstance(message_or_query, CallbackQuery) else message_or_query
    # Меню с картинкой шлём фото-сообщением: подпись под фото выглядит
    # обычным постом, а не ссылкой с превью. Не вышло — идём прежним путём,
    # текстом, и картинка показывается превью.
    previous = target_message if edit_message and isinstance(message_or_query, CallbackQuery) else None
    if await send_main_menu_photo(user_id, text_to_send, kbd, previous):
        if isinstance(message_or_query, CallbackQuery):
            try: await message_or_query.answer()
            except: pass
        return

    preview = main_menu_preview()

    if edit_message and isinstance(message_or_query, CallbackQuery):
        try:
            await target_message.edit_text(text_to_send, reply_markup=kbd, link_preview_options=preview)
        except Exception as e:
            if is_premium_emoji_refusal(e):
                text_to_send = without_premium_emoji(text_to_send)
                try:
                    await target_message.edit_text(
                        text_to_send, reply_markup=kbd, link_preview_options=preview
                    )
                    e = None
                except Exception as retry_error:
                    e = retry_error
            if e is not None and "message is not modified" not in str(e).lower():
                logger.warning(f"Не удалось отредактировать сообщение для {user_id}: {e}. Отправка нового.")
                await bot.send_message(user_id, text_to_send, reply_markup=kbd, link_preview_options=preview)
    else:
        try:
            await target_message.answer(text_to_send, reply_markup=kbd, link_preview_options=preview)
        except Exception as e:
            if not is_premium_emoji_refusal(e):
                raise
            await target_message.answer(
                without_premium_emoji(text_to_send), reply_markup=kbd, link_preview_options=preview
            )

    if isinstance(message_or_query, CallbackQuery):
        try: await message_or_query.answer()
        except: pass

# --- Вспомогательная функция для безопасного ответа на callback query ---
async def safe_answer_callback(query: CallbackQuery, text: str = None, show_alert: bool = False, cache_time: int = None):
    """
    Безопасно отвечает на callback query, игнорируя ошибки "query is too old".
    
    Args:
        query: CallbackQuery объект
        text: Текст ответа (опционально)
        show_alert: Показывать ли alert (опционально)
        cache_time: Время кеширования ответа (опционально)
    """
    try:
        await query.answer(text=text, show_alert=show_alert, cache_time=cache_time)
    except TelegramBadRequest as e:
        error_msg = str(e).lower()
        # Игнорируем ошибки "query is too old" и "query ID is invalid"
        if 'query is too old' in error_msg or 'query id is invalid' in error_msg or 'response timeout expired' in error_msg:
            logger.debug(f"[CALLBACK] Игнорируем устаревший callback query: {e}")
            return
        # Другие ошибки пробрасываем дальше
        raise
    except Exception as e:
        # Логируем другие ошибки, но не падаем
        logger.warning(f"[CALLBACK] Ошибка при ответе на callback query: {e}")
        return

# --- Глобальный обработчик ошибок для aiogram ---
# Используем декоратор для регистрации - это более современный и правильный подход
@dp.error(ExceptionTypeFilter(Exception))
async def global_error_handler(event: ErrorEvent):
    """
    Глобальный обработчик для всех необработанных исключений.
    Ловит абсолютно все ошибки в хендлерах.
    """
    exception = event.exception
    
    # Игнорируем ошибки, которые не являются настоящими сбоями:
    #   - "query is too old"            — обработка callback заняла >15с, юзер уже отпустил кнопку
    #   - "query id is invalid"         — то же самое, другая формулировка
    #   - "response timeout expired"    — то же самое
    #   - "message is not modified"     — повторный клик по той же кнопке, контент идентичен
    if isinstance(exception, TelegramBadRequest):
        error_msg = str(exception).lower()
        _ignorable = (
            'query is too old',
            'query id is invalid',
            'response timeout expired',
            'message is not modified',
        )
        if any(s in error_msg for s in _ignorable):
            logger.debug(f"[GLOBAL_ERROR] Игнорируем безобидный TelegramBadRequest: {exception}")
            return True  # ошибка обработана, дальше не передаём
    
    # event.update - содержит объект Update, который вызвал ошибку
    # event.exception - содержит сам объект исключения
    logger.error("--- Глобальная необработанная ошибка ---")
    logger.error(f"Update: {event.update}")
    logger.error(f"Exception: {exception}")
    
    # Это выведет в лог полный traceback, что очень полезно для отладки
    logger.exception("Полный traceback:")

    return True # Сообщаем aiogram, что ошибка обработана и не нужно ее дальше передавать
async def show_wanted_product(message: Message, product_id: int | None) -> None:
    """Карточка модели, выбранной на витрине, — следом за главным меню.

    Молчим при любой заминке: клиент пришёл по ссылке из браузера, меню он
    уже увидел, и сообщение об ошибке каталога здесь только собьёт с толку —
    модели открываются кнопкой «Купить роутер».
    """
    if not product_id:
        return
    from src.router_catalog import catalog_enabled, send_product_card
    if not catalog_enabled():
        return
    try:
        error = await send_product_card(message, product_id)
    except Exception as exc:  # noqa: BLE001 — вход в бота не должен падать из-за карточки
        logger.warning(f"[CATALOG] карточка с витрины не открылась ({product_id}): {exc}")
        return
    if error:
        logger.warning(f"[CATALOG] карточка с витрины не открылась ({product_id}): {error}")


# --- Логирование входа/выхода в каждый handler ---
# Пример для handle_start, остальные по аналогии
@dp.message(CommandStart())
async def handle_start(message: Message, state: FSMContext):
    logger.info(f"[HANDLER] handle_start: вход для user_id={message.from_user.id}")
    
    # Проверяем блокировку в самом начале
    if await check_user_blocked(message.from_user.id):
        await send_blocked_message(message.from_user.id)
        logger.info(f"[HANDLER] handle_start: выход для заблокированного user_id={message.from_user.id}")
        return
    
    # --- Реферальная система и партнёрская программа: обработка /start <ref> ---
    ref_id = None
    is_partner_ref = False
    # Модель, выбранная на витрине: ссылка `?start=buy_<id>` открывает бота
    # сразу на её карточке. Показываем в самом конце, после меню, — до этого
    # клиент может застрять на капче или подписке на канал.
    wanted_product_id = None
    if message.text:
        parts = message.text.strip().split()
        if len(parts) > 1:
            arg = parts[1]
            if arg.startswith('buy_') and arg[4:].isdigit():
                wanted_product_id = int(arg[4:])
            elif arg.isdigit():
                ref_id = int(arg)
                if ref_id == message.from_user.id:
                    ref_id = None
            elif arg.startswith('par_'):
                # Партнёрская реферальная ссылка
                code = arg[4:]
                try:
                    async with db_helpers.get_db_connection_safe() as db:
                        async with db.execute("SELECT telegram_id FROM users WHERE partner_ref_code = ?", (code,)) as cursor:
                            row = await cursor.fetchone()
                            if row:
                                cand = int(row[0])
                                if cand != message.from_user.id:
                                    ref_id = cand
                                    is_partner_ref = True
                except Exception as e:
                    logger.error(f"Ошибка обработки par_ кода: {e}")
    
    user_id = message.from_user.id
    user_name = (message.from_user.first_name or "")[:32]
    real_username = message.from_user.username or None
    
    # Логируем для отладки
    logger.info(f"[HANDLER] handle_start: создание/обновление пользователя user_id={user_id}, username={real_username}, name={user_name}")
    
    await db_helpers.add_user(user_id, user_name, real_username)
    # Отмечаем клиента и в основном приложении: роутер привязывает оператор
    # по MAC при отгрузке, и строка клиента нужна там раньше заказа.
    # Фоном — вход в бот не должен ждать чужой сервис.
    remember_client(message.from_user)
    user_db_data = await db_helpers.get_user(user_id)
    
    # Проверяем, что пользователь создан правильно
    if not user_db_data:
        logger.error(f"[HANDLER] handle_start: не удалось получить данные пользователя после add_user для user_id={user_id}")
    else:
        logger.info(f"[HANDLER] handle_start: пользователь найден в БД, telegram_id={user_db_data.get('telegram_id') if isinstance(user_db_data, dict) else 'N/A'}")
    
    # Если пользователь новый и invited_by ещё не установлен, сохраняем
    if ref_id:
        # Партнёрские приглашения не ограничиваем дневным лимитом
        if not is_partner_ref:
            max_per_day = int(app_conf.get('referral_limit_per_day', 3))
            can_invite = await db_helpers.can_invite_more_today(ref_id, max_per_day=max_per_day)
            current_count = await db_helpers.get_referrals_count_today(ref_id)
            logger.info(f"Проверка лимита рефералов для {ref_id}: текущих рефералов = {current_count}, может пригласить = {can_invite}")
            if not can_invite:
                logger.warning(f"Пользователь {ref_id} превысил лимит рефералов на сегодня ({max_per_day}). Текущих рефералов: {current_count}")
                ref_id = None
        
        current_invited_by = await db_helpers.get_invited_by(message.from_user.id)
        current_method = await db_helpers.get_invited_by_method(message.from_user.id)
        if ref_id:
            if is_partner_ref:
                if not current_invited_by:
                    await db_helpers.set_invited_by_with_method(message.from_user.id, ref_id, 'partner')
                    logger.info(f"Установлена ПАРТНЁРСКАЯ связь: {message.from_user.id} приглашен пользователем {ref_id}")
                elif current_invited_by == ref_id and current_method != 'partner':
                    # Дообновляем метод для существующей связи
                    await db_helpers.set_invited_by_with_method(message.from_user.id, ref_id, 'partner')
                    logger.info(f"Обновлён метод связи на 'partner' для {message.from_user.id}, пригласивший {ref_id}")
            else:
                if not current_invited_by:
                    await db_helpers.set_invited_by_with_method(message.from_user.id, ref_id, 'referral')
                    logger.info(f"Установлена реферальная связь (referral): {message.from_user.id} приглашен пользователем {ref_id}")
        elif not can_invite:
            logger.info(f"Реферальная связь НЕ установлена для {message.from_user.id} из-за превышения лимита пользователем {ref_id}")
    
    is_trial_used = bool(user_db_data.get('is_trial_used', 1)) if user_db_data else True
    active_sub = await db_helpers.get_active_subscription(message.from_user.id)
    
    # --- Методы защиты при регистрации для новых пользователей ---
    bot_protection_enabled = str(app_conf.get('bot_protection_enabled', '0')) == '1'
    channel_subscription_enabled = str(app_conf.get('channel_subscription_enabled', '0')) == '1'
    channel_identifier = app_conf.get('channel_subscription_username', '').strip()
    
    # Если пользователь новый (не использовал триал) и у него нет активной подписки
    if not is_trial_used and not active_sub:
        # Вариант 1: Защита от ботов
        if bot_protection_enabled:
            # Генерируем контрольный вопрос
            question, correct_answer = generate_captcha_question()
            
            # Генерируем неправильные ответы
            wrong_answers = []
            correct_num = int(correct_answer)
            attempts = 0
            while len(wrong_answers) < 3 and attempts < 10:
                # Генерируем числа близкие к правильному ответу
                wrong_answer = correct_num + random.randint(-5, 5)
                if wrong_answer != correct_num and wrong_answer > 0 and str(wrong_answer) not in wrong_answers:
                    wrong_answers.append(str(wrong_answer))
                attempts += 1
            
            # Если не удалось сгенерировать достаточно неправильных ответов, добавляем случайные
            while len(wrong_answers) < 3:
                random_answer = random.randint(1, 50)
                if str(random_answer) != correct_answer and str(random_answer) not in wrong_answers:
                    wrong_answers.append(str(random_answer))
            
            # Сохраняем данные в состоянии
            await state.update_data(
                ref_id=ref_id,
                correct_answer=correct_answer,
                question=question
            )
            
            # Отправляем вопрос пользователю с кнопками (видео будет отправлено после успешного прохождения капчи)
            protection_text = app_conf.get(
                'bot_protection_text', REST_TEXT_DEFAULTS['bot_protection_text']
            )
            kbd = keyboards.get_captcha_keyboard(correct_answer, wrong_answers)
            await message.answer(
                protection_text.format(question=question),
                reply_markup=kbd
            )
            
            # Устанавливаем состояние ожидания ответа
            await state.set_state(BotProtection.waiting_for_answer)
            logger.info(f"[HANDLER] handle_start: отправлен контрольный вопрос для user_id={message.from_user.id}")
            return
        
        # Вариант 2: Подписка на канал
        elif channel_subscription_enabled and channel_identifier:
            # Проверяем подписку на канал
            checker = get_channel_checker(bot)
            is_subscribed = await checker.check_subscription(message.from_user.id, channel_identifier)
            
            if not is_subscribed:
                # Пользователь не подписан - показываем сообщение с просьбой подписаться
                message_text = await checker.get_subscription_message(channel_identifier)
                keyboard = await checker.get_subscription_keyboard(channel_identifier)
                
                # Сохраняем ref_id в состоянии для последующей выдачи триала
                await state.update_data(ref_id=ref_id)
                
                await message.answer(
                    message_text,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                logger.info(f"[HANDLER] handle_start: показано сообщение о подписке на канал для user_id={message.from_user.id}")
                return
            else:
                # Пользователь уже подписан - проверяем анти-накрутку ПЕРЕД выдачей триала
                logger.info(f"[HANDLER] handle_start: пользователь {message.from_user.id} уже подписан на канал, проверяем анти-накрутку")
                
                # Анти-накрутка: если у пользователя уже есть активная подписка или триал уже использован — не выдаём повторно
                try:
                    active_sub_check = await db_helpers.get_active_subscription(message.from_user.id)
                except Exception:
                    active_sub_check = None
                try:
                    user_row_check = await db_helpers.get_user(message.from_user.id)
                    is_trial_used_check = bool(user_row_check['is_trial_used']) if user_row_check and 'is_trial_used' in user_row_check.keys() else False
                except Exception:
                    is_trial_used_check = False
                
                if active_sub_check or is_trial_used_check:
                    # У пользователя уже есть подписка или триал использован - показываем главное меню
                    logger.info(f"[HANDLER] handle_start: триал не выдан повторно для user_id={message.from_user.id} (active_sub={bool(active_sub_check)}, is_trial_used={is_trial_used_check})")
                    await show_main_menu(message)
                    return
                
                # Пользователь подписан и может получить триал - выдаем триал автоматически
                logger.info(f"[HANDLER] handle_start: пользователь {message.from_user.id} уже подписан на канал, выдаем триал автоматически")
                
                # Получаем блокировку для этого пользователя (защита от race condition)
                user_id = message.from_user.id
                if user_id not in _trial_grant_locks:
                    _trial_grant_locks[user_id] = asyncio.Lock()
                lock = _trial_grant_locks[user_id]
                
                # Используем блокировку для предотвращения одновременной выдачи триала
                async with lock:
                    # Double-check анти-накрутки внутри блокировки
                    try:
                        active_sub_check2 = await db_helpers.get_active_subscription(user_id)
                    except Exception:
                        active_sub_check2 = None
                    try:
                        user_row_check2 = await db_helpers.get_user(user_id)
                        is_trial_used_check2 = bool(user_row_check2['is_trial_used']) if user_row_check2 and 'is_trial_used' in user_row_check2.keys() else False
                    except Exception:
                        is_trial_used_check2 = False
                    
                    if active_sub_check2 or is_trial_used_check2:
                        logger.info(f"[HANDLER] handle_start: триал не выдан повторно для user_id={user_id} (double-check: active_sub={bool(active_sub_check2)}, is_trial_used={is_trial_used_check2})")
                        await show_main_menu(message)
                        return
                    
                    # Показываем последовательность прогресса
                    # Выдаем триал
                    # Пробный период выключен настройкой trial_days=0. Роутер покупают,
                    # и «активация на 0 дней» — это зависший экран вместо меню.
                    if int(app_conf.get('trial_days', 3) or 0) <= 0:
                        await show_main_menu(message)
                        return

                    trial_days = app_conf.get('trial_days', 3)
                    trial_limit_ip = app_conf.get('trial_limit_ip', 1)

                    await show_trial_progress(message, trial_days)

                    subscription_data = await grant_subscription(user_id, trial_days, is_trial=True, limit_ip=trial_limit_ip)
                    
                    # Логируем результат для отладки
                    if subscription_data:
                        uuid_value = subscription_data.get('xui_client_uuid') or subscription_data.get('remnawave_short_uuid') or subscription_data.get('remnawave_user_uuid') or 'N/A'
                        logger.info(f"[HANDLER] handle_start (channel sub): grant_subscription вернул данные для user_id={user_id}, UUID={uuid_value}, sub_link={subscription_data.get('sub_link', 'N/A')}")
                    else:
                        logger.error(f"[HANDLER] handle_start (channel sub): grant_subscription вернул None для user_id={user_id}")
                    
                    if subscription_data:
                        # --- Реферальная система: связь (join-бонус — по вебхуку user.first_connected) ---
                        if ref_id:
                            method = await db_helpers.get_invited_by_method(user_id)
                            if method == 'partner':
                                logger.info(f"Установлена ПАРТНЁРСКАЯ связь: {user_id} приглашен пользователем {ref_id}")
                            else:
                                await db_helpers.set_invited_by(user_id, ref_id)
                                logger.info(f"Установлена реферальная связь: {user_id} приглашен пользователем {ref_id}")
                        
                        try:
                            # Сформируем текст из БД-шаблона text_trial_success (если задан)
                            try:
                                formatted_expiry = format_msk_date_long(subscription_data['expiry_date'])
                            except Exception:
                                formatted_expiry = ""
                            tpl = (app_conf.get('text_trial_success') or '').replace('{sub_link}', '')
                            
                            # Формируем текст для сообщения/видео
                            text_message = ''
                            if tpl and tpl.strip():
                                try:
                                    text_message = tpl.format(days=trial_days, expiry_date=formatted_expiry)
                                except Exception as e:
                                    logger.warning(f"Ошибка форматирования text_trial_success: {e}, шаблон: {tpl}")
                                    text_message = REST_TEXT_DEFAULTS['text_trial_success'].format(
                                        days=trial_days, expiry_date=formatted_expiry
                                    )
                            else:
                                logger.debug(f"text_trial_success не найден или пуст, используем дефолтный текст")
                                text_message = REST_TEXT_DEFAULTS['text_trial_success'].format(
                                    days=trial_days, expiry_date=formatted_expiry
                                )
                            
                            # Отправляем видео-инструкцию вместе с текстом успешной активации (если видео доступно)
                            video_path = os.path.join(os.path.dirname(__file__), 'ins.mp4')
                            video_sent = False
                            try:
                                cached_file_id = app_conf.get('ins_video_file_id', '')
                                file_exists = os.path.isfile(video_path)
                                
                                if file_exists or cached_file_id:
                                    if file_exists:
                                        current_hash = _file_sha256(video_path)
                                        saved_hash = app_conf.get('ins_video_hash', '')
                                        if saved_hash and current_hash != saved_hash:
                                            cached_file_id = ''
                                    
                                    if cached_file_id:
                                        try:
                                            await message.answer_video(
                                                video=cached_file_id,
                                                caption=text_message
                                            )
                                            video_sent = True
                                        except Exception as e:
                                            logger.warning(f"Не удалось отправить видео по file_id: {e}")
                                            cached_file_id = ''
                                    
                                    if not cached_file_id and file_exists:
                                        try:
                                            msg_video = await message.answer_video(
                                                video=FSInputFile(video_path),
                                                caption=text_message
                                            )
                                            video_sent = True
                                            try:
                                                if msg_video and getattr(msg_video, 'video', None) and msg_video.video.file_id:
                                                    await set_setting_value('ins_video_file_id', msg_video.video.file_id)
                                                    if file_exists:
                                                        current_hash = _file_sha256(video_path)
                                                        if current_hash:
                                                            await set_setting_value('ins_video_hash', current_hash)
                                            except Exception:
                                                pass
                                        except Exception as e:
                                            logger.warning(f"Не удалось отправить видео ins.mp4: {e}")
                                else:
                                    logger.debug(f"Видео файл не найден и cached_file_id отсутствует, отправим только текст")
                            except Exception as e:
                                logger.warning(f"Ошибка при отправке видео: {e}")
                            
                            # Если видео не отправилось, редактируем сообщение с прогрессом в финальный текст
                            if not video_sent:
                                try:
                                    await progress_msg.edit_text(
                                        text_message,
                                        disable_web_page_preview=True
                                    )
                                except Exception as e:
                                    logger.warning(f"Не удалось отредактировать сообщение: {e}")
                            await asyncio.sleep(1.4)
                        except Exception:
                            pass
                        await show_main_menu(message)
                        logger.info(f"[HANDLER] handle_start: триал выдан автоматически (пользователь уже подписан) и показано главное меню для user_id={user_id}")
                        return
                    else:
                        logger.error(f"[HANDLER] handle_start: не удалось выдать триал для user_id={user_id}")
                        # Показываем сообщение об ошибке
                        await message.answer(
                            app_conf.get(
                                'text_error_creating_user',
                                REST_TEXT_DEFAULTS['text_error_creating_user'],
                            ),
                            reply_markup=keyboards.get_back_to_main_keyboard()
                        )
                        return
        
        # Вариант 3: Без защиты - выдаем триал автоматически
        if not bot_protection_enabled and not channel_subscription_enabled:
            logger.info(f"[HANDLER] handle_start: защита отключена, выдаем триал автоматически для user_id={message.from_user.id}")
            
            # Получаем блокировку для этого пользователя (защита от race condition)
            user_id = message.from_user.id
            if user_id not in _trial_grant_locks:
                _trial_grant_locks[user_id] = asyncio.Lock()
            lock = _trial_grant_locks[user_id]
            
            # Используем блокировку для предотвращения одновременной выдачи триала
            async with lock:
                # Double-check анти-накрутки внутри блокировки
                try:
                    active_sub_check3 = await db_helpers.get_active_subscription(user_id)
                except Exception:
                    active_sub_check3 = None
                try:
                    user_row_check3 = await db_helpers.get_user(user_id)
                    is_trial_used_check3 = bool(user_row_check3['is_trial_used']) if user_row_check3 and 'is_trial_used' in user_row_check3.keys() else False
                except Exception:
                    is_trial_used_check3 = False
                
                if active_sub_check3 or is_trial_used_check3:
                    logger.info(f"[HANDLER] handle_start: триал не выдан повторно для user_id={user_id} (double-check: active_sub={bool(active_sub_check3)}, is_trial_used={is_trial_used_check3})")
                    await show_main_menu(message)
                    return
                
                # Выдаем триал
                # Пробный период выключен настройкой trial_days=0. Роутер покупают,
                # и «активация на 0 дней» — это зависший экран вместо меню.
                if int(app_conf.get('trial_days', 3) or 0) <= 0:
                    await show_main_menu(message)
                    return

                trial_days = app_conf.get('trial_days', 3)
                trial_limit_ip = app_conf.get('trial_limit_ip', 1)

                await show_trial_progress(message, trial_days)

                subscription_data = await grant_subscription(user_id, trial_days, is_trial=True, limit_ip=trial_limit_ip)
                
                # Логируем результат для отладки
                if subscription_data:
                    uuid_value = subscription_data.get('xui_client_uuid') or subscription_data.get('remnawave_short_uuid') or subscription_data.get('remnawave_user_uuid') or 'N/A'
                    logger.info(f"[HANDLER] handle_start (no protection): grant_subscription вернул данные для user_id={user_id}, UUID={uuid_value}, sub_link={subscription_data.get('sub_link', 'N/A')}")
                else:
                    logger.error(f"[HANDLER] handle_start (no protection): grant_subscription вернул None для user_id={user_id}")
                
                if subscription_data:
                    # --- Реферальная система: связь (join-бонус — по вебхуку user.first_connected) ---
                    if ref_id:
                        method = await db_helpers.get_invited_by_method(user_id)
                        if method == 'partner':
                            logger.info(f"Установлена ПАРТНЁРСКАЯ связь: {user_id} приглашен пользователем {ref_id}")
                        else:
                            await db_helpers.set_invited_by(user_id, ref_id)
                            logger.info(f"Установлена реферальная связь: {user_id} приглашен пользователем {ref_id}")
                    
                    try:
                        # Сформируем текст из БД-шаблона text_trial_success (если задан)
                        try:
                            formatted_expiry = format_msk_date_long(subscription_data['expiry_date'])
                        except Exception:
                            formatted_expiry = ""
                        tpl = (app_conf.get('text_trial_success') or '').replace('{sub_link}', '')
                        
                        # Формируем текст для сообщения/видео
                        text_message = ''
                        if tpl and tpl.strip():
                            try:
                                text_message = tpl.format(days=trial_days, expiry_date=formatted_expiry)
                            except Exception as e:
                                logger.warning(f"Ошибка форматирования text_trial_success: {e}, шаблон: {tpl}")
                                text_message = REST_TEXT_DEFAULTS['text_trial_success'].format(
                                    days=trial_days, expiry_date=formatted_expiry
                                )
                        else:
                            logger.debug(f"text_trial_success не найден или пуст, используем дефолтный текст")
                            text_message = REST_TEXT_DEFAULTS['text_trial_success'].format(
                                days=trial_days, expiry_date=formatted_expiry
                            )
                        
                        # Отправляем видео-инструкцию вместе с текстом успешной активации (если видео доступно)
                        video_path = os.path.join(os.path.dirname(__file__), 'ins.mp4')
                        video_sent = False
                        try:
                            cached_file_id = app_conf.get('ins_video_file_id', '')
                            file_exists = os.path.isfile(video_path)
                            
                            if file_exists or cached_file_id:
                                if file_exists:
                                    current_hash = _file_sha256(video_path)
                                    saved_hash = app_conf.get('ins_video_hash', '')
                                    if saved_hash and current_hash != saved_hash:
                                        cached_file_id = ''
                                
                                if cached_file_id:
                                    try:
                                        await message.answer_video(
                                            video=cached_file_id,
                                            caption=text_message
                                        )
                                        video_sent = True
                                    except Exception as e:
                                        logger.warning(f"Не удалось отправить видео по file_id: {e}")
                                        cached_file_id = ''
                                
                                if not cached_file_id and file_exists:
                                    try:
                                        msg_video = await message.answer_video(
                                            video=FSInputFile(video_path),
                                            caption=text_message
                                        )
                                        video_sent = True
                                        try:
                                            if msg_video and getattr(msg_video, 'video', None) and msg_video.video.file_id:
                                                await set_setting_value('ins_video_file_id', msg_video.video.file_id)
                                                if file_exists:
                                                    current_hash = _file_sha256(video_path)
                                                    if current_hash:
                                                        await set_setting_value('ins_video_hash', current_hash)
                                        except Exception:
                                            pass
                                    except Exception as e:
                                        logger.warning(f"Не удалось отправить видео ins.mp4: {e}")
                            else:
                                logger.debug(f"Видео файл не найден и cached_file_id отсутствует, отправим только текст")
                        except Exception as e:
                            logger.warning(f"Ошибка при отправке видео: {e}")
                        
                        # Если видео не отправилось, редактируем сообщение с прогрессом в финальный текст
                        if not video_sent:
                            try:
                                await progress_msg.edit_text(
                                    text_message,
                                    disable_web_page_preview=True
                                )
                            except Exception as e:
                                logger.warning(f"Не удалось отредактировать сообщение: {e}")
                        await asyncio.sleep(1.4)
                    except Exception:
                        pass
                    await show_main_menu(message)
                    logger.info(f"[HANDLER] handle_start: триал выдан автоматически и показано главное меню для user_id={user_id}")
                    return
                else:
                    logger.error(f"[HANDLER] handle_start: не удалось выдать триал для user_id={user_id}")
                    # Показываем сообщение об ошибке
                    await message.answer(
                        app_conf.get(
                            'text_error_creating_user',
                            REST_TEXT_DEFAULTS['text_error_creating_user'],
                        ),
                        reply_markup=keyboards.get_back_to_main_keyboard()
                    )
                    return
    
    # Если пользователь уже прошел защиту или у него есть подписка, продолжаем как обычно
    await show_main_menu(message)
    await show_wanted_product(message, wanted_product_id)
    logger.info(f"[HANDLER] handle_start: выход для user_id={message.from_user.id}")

@dp.callback_query(F.data.startswith("captcha_answer_"))
async def handle_captcha_answer(query: CallbackQuery, state: FSMContext):
    """Обработчик ответов на контрольный вопрос"""
    logger.info(f"[HANDLER] handle_captcha_answer: вход для user_id={query.from_user.id}")
    
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        logger.info(f"[HANDLER] handle_captcha_answer: выход для заблокированного user_id={query.from_user.id}")
        return
    
    # Получаем данные из состояния
    data = await state.get_data()
    correct_answer = data.get('correct_answer')
    ref_id = data.get('ref_id')
    
    # ИСПРАВЛЕНИЕ: Если ref_id нет в состоянии, но реферальная связь уже установлена в БД - берем из БД
    if not ref_id:
        current_invited_by = await db_helpers.get_invited_by(query.from_user.id)
        if current_invited_by:
            ref_id = current_invited_by
            logger.info(f"[REFERRAL] ref_id восстановлен из БД: {ref_id} для пользователя {query.from_user.id}")
    
    # Получаем ответ пользователя
    user_answer = query.data.replace("captcha_answer_", "")
    
    # Получаем блокировку для этого пользователя (защита от race condition)
    user_id = query.from_user.id
    if user_id not in _trial_grant_locks:
        _trial_grant_locks[user_id] = asyncio.Lock()
    lock = _trial_grant_locks[user_id]
    
    if user_answer == correct_answer:
        # Используем блокировку для предотвращения одновременной выдачи триала
        async with lock:
            # Анти-накрутка: если у пользователя уже есть активная подписка или триал уже использован — не выдаём повторно (double-check внутри блокировки)
            try:
                active_sub = await db_helpers.get_active_subscription(user_id)
            except Exception:
                active_sub = None
            try:
                user_row = await db_helpers.get_user(user_id)
                is_trial_used_now = bool(user_row['is_trial_used']) if user_row and 'is_trial_used' in user_row.keys() else False
            except Exception:
                is_trial_used_now = False
            if active_sub or is_trial_used_now:
                already_text = 'ℹ️ У вас уже активна подписка или пробный период был использован ранее, вернитесь в главное меню'
                try:
                    await query.message.edit_text(already_text, reply_markup=keyboards.get_back_to_main_keyboard())
                except Exception:
                    try:
                        await query.message.edit_caption(already_text, reply_markup=keyboards.get_back_to_main_keyboard())
                    except Exception:
                        pass
                try:
                    await state.clear()
                except Exception:
                    pass
                logger.info(f"[HANDLER] handle_captcha_answer: триал не выдан повторно для user_id={user_id} (active_sub={bool(active_sub)}, is_trial_used={is_trial_used_now})")
                return
            
            # Сразу отвечаем на callback query, чтобы кнопка не висела в загрузке
            try:
                await query.answer("✓ Всё верно. Создаём подписку")
            except Exception:
                pass
            
            # Показываем прогресс ДО создания клиента
            # Пробный период выключен настройкой trial_days=0. Роутер покупают,
            # и «активация на 0 дней» — это зависший экран вместо меню.
            if int(app_conf.get('trial_days', 3) or 0) <= 0:
                await show_main_menu(query.message)
                return

            trial_days = app_conf.get('trial_days', 3)
            await show_trial_progress_edit(query.message, trial_days)
            trial_limit_ip = app_conf.get('trial_limit_ip', 1)
            subscription_data = await grant_subscription(user_id, trial_days, is_trial=True, limit_ip=trial_limit_ip)
            
            # Логируем результат для отладки
            if subscription_data:
                logger.info(f"[HANDLER] handle_captcha_answer: grant_subscription вернул данные для user_id={user_id}, UUID={subscription_data.get('xui_client_uuid') or subscription_data.get('remnawave_short_uuid') or subscription_data.get('remnawave_user_uuid') or 'N/A'}, sub_link={subscription_data.get('sub_link', 'N/A')}")
            else:
                logger.error(f"[HANDLER] handle_captcha_answer: grant_subscription вернул None для user_id={user_id}")
            
            if subscription_data:
                try:
                    formatted_expiry = format_msk_date_long(subscription_data['expiry_date'])
                except Exception:
                    formatted_expiry = ""
                tpl = (app_conf.get('text_trial_success') or '').replace('{sub_link}', '')
                logger.debug(f"[TRIAL] text_trial_success получен: {bool(tpl)}, длина: {len(tpl) if tpl else 0}")
                
                # Формируем текст для сообщения/видео
                text_message = ''
                if tpl and tpl.strip():
                    try:
                        text_message = tpl.format(days=trial_days, expiry_date=formatted_expiry)
                        logger.debug(f"[TRIAL] text_message сформирован из text_trial_success, длина: {len(text_message)}")
                    except Exception as e:
                        logger.warning(f"Ошибка форматирования text_trial_success: {e}, шаблон: {tpl}")
                        text_message = REST_TEXT_DEFAULTS['text_trial_success'].format(
                            days=trial_days, expiry_date=formatted_expiry
                        )
                else:
                    logger.debug(f"text_trial_success не найден или пуст, используем дефолтный текст")
                    text_message = REST_TEXT_DEFAULTS['text_trial_success'].format(
                        days=trial_days, expiry_date=formatted_expiry
                    )
                
                # Отправляем видео-инструкцию вместе с текстом успешной активации (если видео доступно)
                video_path = os.path.join(os.path.dirname(__file__), 'ins.mp4')
                video_sent = False
                try:
                    cached_file_id = app_conf.get('ins_video_file_id', '')
                    file_exists = os.path.isfile(video_path)
                    
                    if file_exists or cached_file_id:
                        if file_exists:
                            current_hash = _file_sha256(video_path)
                            saved_hash = app_conf.get('ins_video_hash', '')
                            if saved_hash and current_hash != saved_hash:
                                cached_file_id = ''
                        
                        if cached_file_id:
                            try:
                                # Удаляем сообщение с прогрессом перед отправкой видео
                                try:
                                    await bot.delete_message(chat_id=query.from_user.id, message_id=query.message.message_id)
                                except Exception:
                                    pass
                                await bot.send_video(
                                    chat_id=query.from_user.id,
                                    video=cached_file_id,
                                    caption=text_message
                                )
                                video_sent = True
                            except Exception as e:
                                logger.warning(f"Не удалось отправить видео по file_id: {e}")
                                cached_file_id = ''
                        
                        if not cached_file_id and file_exists:
                            try:
                                # Удаляем сообщение с прогрессом перед отправкой видео
                                try:
                                    await bot.delete_message(chat_id=query.from_user.id, message_id=query.message.message_id)
                                except Exception:
                                    pass
                                msg_video = await bot.send_video(
                                    chat_id=query.from_user.id,
                                    video=FSInputFile(video_path),
                                    caption=text_message
                                )
                                video_sent = True
                                try:
                                    if msg_video and getattr(msg_video, 'video', None) and msg_video.video.file_id:
                                        await set_setting_value('ins_video_file_id', msg_video.video.file_id)
                                        if file_exists:
                                            current_hash = _file_sha256(video_path)
                                            if current_hash:
                                                await set_setting_value('ins_video_hash', current_hash)
                                except Exception:
                                    pass
                            except Exception as e:
                                logger.warning(f"Не удалось отправить видео ins.mp4: {e}")
                    else:
                        logger.debug(f"Видео файл не найден и cached_file_id отсутствует, отправим только текст")
                except Exception as e:
                    logger.warning(f"Ошибка при отправке видео: {e}")
                
                # Если видео не отправилось, редактируем сообщение с прогрессом в финальный текст
                if not video_sent:
                    try:
                        await query.message.edit_text(
                            text_message,
                            disable_web_page_preview=True
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось отредактировать сообщение, отправляем новое: {e}")
                        try:
                            await bot.send_message(
                                chat_id=user_id,
                                text=text_message,
                                disable_web_page_preview=True
                            )
                        except Exception:
                            pass
                await asyncio.sleep(1.4)
                
                # --- Реферальная система: связь (join-бонус — по вебхуку user.first_connected) ---
                if ref_id:
                    method = await db_helpers.get_invited_by_method(user_id)
                    if method == 'partner':
                        logger.info(f"Партнёрская привязка: {user_id} приглашен пользователем {ref_id}")
                    else:
                        current_inv = await db_helpers.get_invited_by(user_id)
                        if not current_inv:
                            await db_helpers.set_invited_by(user_id, ref_id)
                            logger.info(f"Установлена реферальная связь: {user_id} приглашен пользователем {ref_id}")
                
                # Очищаем состояние и показываем главное меню
                await state.clear()
                try:
                    # Отправляем главное меню отдельным сообщением, чтобы финальное "Готово" осталось в истории
                    # show_main_menu сам отвечает на callback query, поэтому не вызываем query.answer() здесь
                    await show_main_menu(query)
                except Exception as e:
                    logger.error(f"[HANDLER] handle_captcha_answer: ошибка при показе главного меню для {user_id}: {e}")
                    # Пытаемся ответить на callback query даже при ошибке
                    try:
                        await query.answer()
                    except:
                        pass
                logger.info(f"[HANDLER] handle_captcha_answer: триал выдан, показано главное меню для user_id={user_id}")
            else:
                logger.error(f"[HANDLER] handle_captcha_answer: Не удалось создать пробный XUI пользователя для {user_id} после прохождения защиты (grant_subscription вернул None или пустой UUID)")
                # Проверяем, есть ли UUID в БД (возможно, подписка создалась, но функция вернула None)
                try:
                    user_db_data = await db_helpers.get_user(user_id)
                    if user_db_data:
                        uuid_from_db = user_db_data.get('xui_client_uuid') or user_db_data.get('remnawave_short_uuid') or user_db_data.get('remnawave_user_uuid')
                        if uuid_from_db:
                            logger.warning(f"[HANDLER] handle_captcha_answer: UUID найден в БД ({uuid_from_db}), но grant_subscription вернул None. Возможно, подписка создана, но произошла ошибка при возврате результата.")
                            # Пытаемся получить данные подписки из БД
                            last_sub = await db_helpers.get_last_subscription(user_id)
                            if last_sub:
                                logger.info(f"[HANDLER] handle_captcha_answer: Найдена подписка в БД, показываем главное меню")
                                await state.clear()
                                await show_main_menu(query)
                                return
                except Exception as e:
                    logger.error(f"[HANDLER] handle_captcha_answer: Ошибка при проверке UUID в БД: {e}")
                
                try:
                    await query.message.edit_text(app_conf.get('text_error_creating_user'), reply_markup=keyboards.get_back_to_main_keyboard())
                except Exception:
                    try:
                        await query.message.edit_caption(app_conf.get('text_error_creating_user'), reply_markup=keyboards.get_back_to_main_keyboard())
                    except Exception:
                        pass
                # Отвечаем на callback query, чтобы избежать зависания
                try:
                    await query.answer("✕ Не удалось создать подписку", show_alert=True)
                except Exception:
                    pass
                # Очищаем состояние и выходим, не показывая главное меню
                await state.clear()
                return
    else:
        # Неправильный ответ - генерируем новый вопрос
        question, correct_answer = generate_captcha_question()
        
        # Генерируем неправильные ответы
        wrong_answers = []
        correct_num = int(correct_answer)
        attempts = 0
        while len(wrong_answers) < 3 and attempts < 10:
            # Генерируем числа близкие к правильному ответу
            wrong_answer = correct_num + random.randint(-5, 5)
            if wrong_answer != correct_num and wrong_answer > 0 and str(wrong_answer) not in wrong_answers:
                wrong_answers.append(str(wrong_answer))
            attempts += 1
        
        # Если не удалось сгенерировать достаточно неправильных ответов, добавляем случайные
        while len(wrong_answers) < 3:
            random_answer = random.randint(1, 50)
            if str(random_answer) != correct_answer and str(random_answer) not in wrong_answers:
                wrong_answers.append(str(random_answer))
        
        # Обновляем данные в состоянии (сохраняем ref_id)
        await state.update_data(
            ref_id=ref_id,  # Сохраняем ref_id для следующей попытки
            correct_answer=correct_answer,
            question=question
        )
        
        # Отправляем новый вопрос
        wrong_text = app_conf.get(
            'bot_protection_wrong_text', REST_TEXT_DEFAULTS['bot_protection_wrong_text']
        )
        try:
            await query.message.edit_text(
                wrong_text.format(question=question),
                reply_markup=keyboards.get_captcha_keyboard(correct_answer, wrong_answers)
            )
        except Exception:
            try:
                await query.message.edit_caption(
                    wrong_text.format(question=question),
                    reply_markup=keyboards.get_captcha_keyboard(correct_answer, wrong_answers)
                )
            except Exception:
                pass
        logger.info(f"[HANDLER] handle_captcha_answer: новый вопрос отправлен для user_id={query.from_user.id}")
    
    logger.info(f"[HANDLER] handle_captcha_answer: выход для user_id={query.from_user.id}")

@dp.callback_query(F.data == "check_subscription")
async def check_subscription_callback(query: CallbackQuery, state: FSMContext):
    """Обработчик проверки подписки на канал - выдача триала после подписки"""
    logger.info(f"[HANDLER] check_subscription_callback: вход для user_id={query.from_user.id}")
    
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        logger.info(f"[HANDLER] check_subscription_callback: выход для заблокированного user_id={query.from_user.id}")
        return
    
    # Получаем настройки
    channel_subscription_enabled = str(app_conf.get('channel_subscription_enabled', '0')) == '1'
    channel_identifier = app_conf.get('channel_subscription_username', '').strip()
    
    if not channel_subscription_enabled or not channel_identifier:
        await query.answer("Настройка подписки не активна", show_alert=True)
        return
    
    # Получаем данные из состояния (ref_id для реферальной системы)
    data = await state.get_data()
    ref_id = data.get('ref_id')
    
    # Восстанавливаем ref_id из БД если нет в состоянии
    if not ref_id:
        current_invited_by = await db_helpers.get_invited_by(query.from_user.id)
        if current_invited_by:
            ref_id = current_invited_by
            logger.info(f"[REFERRAL] ref_id восстановлен из БД: {ref_id} для пользователя {query.from_user.id}")
    
    # Проверяем подписку (инвалидируем кэш перед проверкой)
    checker = get_channel_checker(bot)
    await checker.invalidate_cache(query.from_user.id)
    is_subscribed = await checker.check_subscription(query.from_user.id, channel_identifier)
    
    if not is_subscribed:
        # Пользователь все еще не подписан
        message_text = await checker.get_subscription_message(channel_identifier)
        keyboard = await checker.get_subscription_keyboard(channel_identifier)
        
        try:
            await query.message.edit_text(
                message_text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            await query.answer("Упс, похоже вы не подписались", show_alert=True)
        except Exception as e:
            logger.error(f"Ошибка при проверке подписки для {query.from_user.id}: {e}")
            await query.answer("Ошибка проверки подписки", show_alert=True)
        logger.info(f"[HANDLER] check_subscription_callback: пользователь {query.from_user.id} не подписан на канал")
        return
    
    # Получаем блокировку для этого пользователя (защита от race condition)
    user_id = query.from_user.id
    if user_id not in _trial_grant_locks:
        _trial_grant_locks[user_id] = asyncio.Lock()
    lock = _trial_grant_locks[user_id]
    
    # Используем блокировку для предотвращения одновременной выдачи триала
    async with lock:
        # Пользователь подписан - проверяем анти-накрутку (double-check внутри блокировки)
        try:
            active_sub = await db_helpers.get_active_subscription(user_id)
        except Exception:
            active_sub = None
        try:
            user_row = await db_helpers.get_user(user_id)
            is_trial_used_now = bool(user_row['is_trial_used']) if user_row and 'is_trial_used' in user_row.keys() else False
        except Exception:
            is_trial_used_now = False
        
        if active_sub or is_trial_used_now:
            already_text = 'ℹ️ У вас уже активна подписка или пробный период был использован ранее, вернитесь в главное меню'
            try:
                await query.message.edit_text(already_text, reply_markup=keyboards.get_back_to_main_keyboard())
            except Exception:
                try:
                    await query.message.edit_caption(already_text, reply_markup=keyboards.get_back_to_main_keyboard())
                except Exception:
                    pass
            try:
                await state.clear()
            except Exception:
                pass
            logger.info(f"[HANDLER] check_subscription_callback: триал не выдан повторно для user_id={user_id} (active_sub={bool(active_sub)}, is_trial_used={is_trial_used_now})")
            return
        
        # Пользователь подписан и может получить триал - выдаем триал
        try:
            await query.answer("✓ Проверка пройдена. Создаём подписку")
        except Exception:
            pass
        
        # Показываем прогресс ДО создания клиента
        try:
            await query.message.edit_text("<b>Подключение</b>\n○ Выбираем сервер.")
            await asyncio.sleep(0.3)
            await query.message.edit_text("<b>Подключение</b>\n○ Выбираем сервер..")
            await asyncio.sleep(0.3)
            await query.message.edit_text("<b>Подключение</b>\n○ Выбираем сервер...")
            await asyncio.sleep(0.3)
            await query.message.edit_text("⏳ Создаем вашу подписку...")
        except Exception:
            pass
        
        # Выдаем триал
        # Пробный период выключен настройкой trial_days=0. Роутер покупают,
        # и «активация на 0 дней» — это зависший экран вместо меню.
        if int(app_conf.get('trial_days', 3) or 0) <= 0:
            await show_main_menu(query.message)
            return

        trial_days = app_conf.get('trial_days', 3)
        trial_limit_ip = app_conf.get('trial_limit_ip', 1)
        subscription_data = await grant_subscription(user_id, trial_days, is_trial=True, limit_ip=trial_limit_ip)
        
        # Логируем результат для отладки
        if subscription_data:
            uuid_value = subscription_data.get('xui_client_uuid') or subscription_data.get('remnawave_short_uuid') or subscription_data.get('remnawave_user_uuid') or 'N/A'
            logger.info(f"[HANDLER] check_subscription_callback: grant_subscription вернул данные для user_id={user_id}, UUID={uuid_value}, sub_link={subscription_data.get('sub_link', 'N/A')}")
        else:
            logger.error(f"[HANDLER] check_subscription_callback: grant_subscription вернул None для user_id={user_id}")
        
        if subscription_data:
            # --- Реферальная система: связь (join-бонус — по вебхуку user.first_connected) ---
            if ref_id:
                method = await db_helpers.get_invited_by_method(user_id)
                if method == 'partner':
                    logger.info(f"Партнёрская привязка: {user_id} приглашен пользователем {ref_id}")
                else:
                    current_inv = await db_helpers.get_invited_by(user_id)
                    if not current_inv:
                        await db_helpers.set_invited_by(user_id, ref_id)
                        logger.info(f"Установлена реферальная связь: {user_id} приглашен пользователем {ref_id}")
            
            # Формируем сообщение об успехе
            try:
                formatted_expiry = format_msk_date_long(subscription_data['expiry_date'])
            except Exception:
                formatted_expiry = ""
            tpl = (app_conf.get('text_trial_success') or '').replace('{sub_link}', '')
            
            # Формируем текст для сообщения/видео
            text_message = ''
            if tpl and tpl.strip():
                try:
                    text_message = tpl.format(days=trial_days, expiry_date=formatted_expiry)
                except Exception as e:
                    logger.warning(f"Ошибка форматирования text_trial_success: {e}, шаблон: {tpl}")
                    text_message = REST_TEXT_DEFAULTS['text_trial_success'].format(
                        days=trial_days, expiry_date=formatted_expiry
                    )
            else:
                text_message = REST_TEXT_DEFAULTS['text_trial_success'].format(
                    days=trial_days, expiry_date=formatted_expiry
                )
            
            # Отправляем видео-инструкцию (если доступно)
            video_path = os.path.join(os.path.dirname(__file__), 'ins.mp4')
            video_sent = False
            try:
                cached_file_id = app_conf.get('ins_video_file_id', '')
                file_exists = os.path.isfile(video_path)
                
                if file_exists or cached_file_id:
                    if file_exists:
                        current_hash = _file_sha256(video_path)
                        saved_hash = app_conf.get('ins_video_hash', '')
                        if saved_hash and current_hash != saved_hash:
                            cached_file_id = ''
                    
                    if cached_file_id:
                        try:
                            try:
                                await bot.delete_message(chat_id=user_id, message_id=query.message.message_id)
                            except Exception:
                                pass
                            await bot.send_video(
                                chat_id=user_id,
                                video=cached_file_id,
                                caption=text_message
                            )
                            video_sent = True
                        except Exception as e:
                            logger.warning(f"Не удалось отправить видео по file_id: {e}")
                            cached_file_id = ''
                    
                    if not cached_file_id and file_exists:
                        try:
                            try:
                                await bot.delete_message(chat_id=user_id, message_id=query.message.message_id)
                            except Exception:
                                pass
                            msg_video = await bot.send_video(
                                chat_id=user_id,
                                video=FSInputFile(video_path),
                                caption=text_message
                            )
                            video_sent = True
                            try:
                                if msg_video and getattr(msg_video, 'video', None) and msg_video.video.file_id:
                                    await set_setting_value('ins_video_file_id', msg_video.video.file_id)
                                    if file_exists:
                                        current_hash = _file_sha256(video_path)
                                        if current_hash:
                                            await set_setting_value('ins_video_hash', current_hash)
                            except Exception:
                                pass
                        except Exception as e:
                            logger.warning(f"Не удалось отправить видео ins.mp4: {e}")
            except Exception as e:
                logger.warning(f"Ошибка при отправке видео: {e}")
            
            # Если видео не отправилось, редактируем сообщение с прогрессом в финальный текст
            if not video_sent:
                try:
                    await query.message.edit_text(
                        text_message,
                        disable_web_page_preview=True
                    )
                except Exception as e:
                    logger.warning(f"Не удалось отредактировать сообщение, отправляем новое: {e}")
                    try:
                        await bot.send_message(
                            chat_id=user_id,
                            text=text_message,
                            disable_web_page_preview=True
                        )
                    except Exception:
                        pass
            await asyncio.sleep(1.4)
            
            # Очищаем состояние и показываем главное меню
            await state.clear()
            try:
                await show_main_menu(query)
            except Exception as e:
                logger.error(f"[HANDLER] check_subscription_callback: ошибка при показе главного меню для {user_id}: {e}")
                try:
                    await query.answer()
                except:
                    pass
            logger.info(f"[HANDLER] check_subscription_callback: триал выдан, показано главное меню для user_id={user_id}")
        else:
            logger.error(f"Не удалось создать пробный XUI пользователя для {user_id} после проверки подписки")
            try:
                await query.message.edit_text(app_conf.get('text_error_creating_user'), reply_markup=keyboards.get_back_to_main_keyboard())
            except Exception:
                try:
                    await query.message.edit_caption(app_conf.get('text_error_creating_user'), reply_markup=keyboards.get_back_to_main_keyboard())
                except Exception:
                    pass
            try:
                await query.answer("✕ Не удалось создать подписку", show_alert=True)
            except Exception:
                pass
            await state.clear()
            logger.info(f"[HANDLER] check_subscription_callback: выход с ошибкой для user_id={user_id}")

@dp.callback_query(F.data == "restart_deleted_user")
async def cq_restart_deleted_user(query: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'Старт' для удаленных пользователей - создает новое сообщение с правильным from_user"""
    user_id = query.from_user.id
    logger.info(f"[HANDLER] cq_restart_deleted_user: вход для user_id={user_id}")
    
    # Проверяем, что from_user установлен правильно
    if not query.from_user or not query.message:
        logger.error(f"[HANDLER] cq_restart_deleted_user: query.from_user или query.message отсутствует")
        try:
            await query.answer("✕ Не удалось определить пользователя")
        except:
            pass
        return
    
    await query.answer()
    
    # Проверяем блокировку
    if await check_user_blocked(user_id):
        await send_blocked_message(user_id, query)
        return
    
    # Проверяем, что message.from_user совпадает с query.from_user
    if query.message.from_user.id != user_id:
        logger.warning(f"[HANDLER] cq_restart_deleted_user: несоответствие ID - query.from_user.id={user_id}, message.from_user.id={query.message.from_user.id}. Создаем новый объект Message.")
        # Создаем новый объект Message с правильным from_user (используем model_copy для копирования frozen объекта)
        try:
            from aiogram.types import Message
            # Создаем новый Message с правильным from_user, копируя все остальные поля
            new_message = query.message.model_copy(update={'from_user': query.from_user})
            logger.info(f"[HANDLER] cq_restart_deleted_user: создан новый Message с правильным from_user.id={new_message.from_user.id}")
            await handle_start(new_message, state)
        except Exception as e:
            logger.error(f"[HANDLER] cq_restart_deleted_user: ошибка при создании нового Message: {e}")
            # Fallback: отправляем команду /start напрямую пользователю
            try:
                await bot.send_message(user_id, "/start")
            except Exception as e2:
                logger.error(f"[HANDLER] cq_restart_deleted_user: ошибка при отправке /start: {e2}")
    else:
        # ID совпадают, можно использовать query.message напрямую
        logger.info(f"[HANDLER] cq_restart_deleted_user: вызываем handle_start для user_id={user_id}")
        await handle_start(query.message, state)

@dp.callback_query(F.data == "back_to_main")
async def cq_back_to_main(query: CallbackQuery, state: FSMContext):
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
        
    await state.clear()
    await show_main_menu(query, edit_message=True)

@dp.callback_query(F.data == "about_service")
async def cq_about_service(query: CallbackQuery):
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
        
    kbd = keyboards.get_about_service_keyboard()
        
    await query.message.edit_text(
        (app_conf.get('text_about_service') or DEFAULT_ABOUT_SERVICE).format(
            project_name=app_conf.get('project_name') or ''
        ),
        reply_markup=kbd
    )
    await query.answer()

@dp.callback_query(F.data == "support")
async def cq_support(query: CallbackQuery):
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
    
    user_id = query.from_user.id
    support_link = app_conf.get('support_link', '')
    
    # Формируем сообщение с инструкцией
    user_id_text = str(user_id)
    # Получаем текст поддержки из настроек или используем значение по умолчанию
    support_text_template = app_conf.get('text_support', TXT_SUPPORT_FALLBACK)

    # MAC роутера — первое, что спрашивает оператор: по нему находится
    # и клиент, и его подписка. Роутера может не быть вовсе (человек ещё
    # не купил), поэтому {router_mac} тогда пуст, а {router_line} — готовая
    # строка, которая в этом случае исчезает целиком, не оставляя
    # висящего «Роутер:» без значения.
    router_mac = await client_router_mac(user_id)
    router_line = f"Роутер: <code>{html.escape(router_mac)}</code>" if router_mac else ""

    values = {
        'user_id': html.escape(user_id_text),
        'router_mac': html.escape(router_mac),
        'router_line': router_line,
    }
    try:
        support_text = support_text_template.format(**values)
    except (KeyError, ValueError, IndexError):
        # Шаблон правит оператор, и незнакомая переменная в нём — вопрос
        # времени. Экран поддержки падать из-за этого не должен: подставляем
        # известные значения по одному, остальное оставляем как написано.
        support_text = support_text_template
        for name, value in values.items():
            support_text = support_text.replace('{' + name + '}', value)
    
    # Создаем клавиатуру
    builder = InlineKeyboardBuilder()
    
    # Кастомная ссылка поддержки (если установлена, отображается над основной кнопкой)
    support_custom_link = app_conf.get('support_custom_link', '').strip()
    if support_custom_link:
        builder.row(btn('btn_support_custom_link', url=support_custom_link))

    # Кнопка со ссылкой на поддержку (если есть)
    if support_link:
        builder.row(btn('btn_support_link', url=support_link))

    # Кнопка "Назад"
    builder.row(btn('btn_back_to_main', callback_data='back_to_main'))
    
    try:
        await query.message.edit_text(
            support_text,
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения поддержки: {e}")
        await query.message.answer(
            support_text,
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )
    
    await query.answer()

@dp.callback_query(F.data == "payment_history")
async def cq_payment_history(query: CallbackQuery):
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
        
    user_id = query.from_user.id
    
    # Получаем только успешные платежи пользователя
    payments = await db_helpers.get_user_successful_payments(user_id)
    
    if not payments:
        await query.message.edit_text(
            "<b>История платежей</b>\n"
            "○ Успешных платежей пока нет",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [btn('btn_back', callback_data='renew_choose_payment')]
            ])
        )
        await query.answer()
        return
    
    # Формируем текст с историей платежей
    text = "<b>История платежей</b>\n✓ Последние оплаты\n\n"
    
    for i, payment in enumerate(payments[:10], 1):  # Показываем последние 10 платежей
        payment_id, telegram_id, amount, currency, status, created_at, metadata_json = payment
        
        # Форматируем дату
        try:
            created_date = datetime.fromisoformat(created_at)
            date_str = format_msk_date(created_date, '%d.%m.%Y %H:%M')
        except:
            date_str = "Неизвестно"
        
        text += f"{i}. ✓ <b>{amount} {currency}</b>\n"
        text += f"   ID: <code>{payment_id}</code>\n"
        text += f"   Дата: {date_str}\n\n"
    
    if len(payments) > 10:
        text += f"... и еще {len(payments) - 10} платежей"
    
    kbd = InlineKeyboardMarkup(inline_keyboard=[
        [btn('btn_back', callback_data='renew_choose_payment')]
    ])
    
    await query.message.edit_text(text, reply_markup=kbd)
    await query.answer()

@dp.callback_query(F.data.startswith("my_referrals"))
async def cq_my_referrals(query: CallbackQuery):
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return

    user_id = query.from_user.id
    # Получаем всех рефералов
    rows = await db_helpers.get_referrals(user_id)

    if not rows:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        back_kbd = InlineKeyboardMarkup(inline_keyboard=[[btn('btn_back', callback_data='referral_program')]])
        await query.message.edit_text(
            "<b>Мои рефералы</b>\n○ Приглашённых пока нет", reply_markup=back_kbd
        )
        await query.answer()
        return

    # Пагинация по 5
    page_size = 5
    # Пробуем извлечь страницу из callback_data вида my_referrals:2
    current_page = 1
    if ":" in (query.data or ""):
        try:
            current_page = max(1, int(query.data.split(":", 1)[1]))
        except Exception:
            current_page = 1

    total = len(rows)
    total_pages = (total + page_size - 1) // page_size
    start = (current_page - 1) * page_size
    end = start + page_size
    page_rows = rows[start:end]

    # Формируем текст
    lines = ["<b>Мои рефералы</b>", f"✓ Всего: {total}", ""]
    for r in page_rows:
        tid = r.get('telegram_id') if isinstance(r, dict) else r[0]
        uname_raw = r.get('username') if isinstance(r, dict) else (r[1] or '')
        uname = (uname_raw or '')[:8]
        created = r.get('created_at') if isinstance(r, dict) else (r[2] or '')
        uname_disp = f"Ник: {uname}" if uname else "Никнейм: пусто"
        lines.append(f"• ID: {hcode(str(tid))} | {uname_disp}")
    lines.append("")
    lines.append(f"Стр. {current_page}/{total_pages}")

    # Клавиатура: Назад + пагинация при необходимости
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    buttons = []
    nav = []
    if current_page > 1:
        nav.append(InlineKeyboardButton(text="‹", callback_data=f"my_referrals:{current_page-1}"))
    if current_page < total_pages:
        nav.append(InlineKeyboardButton(text="▸", callback_data=f"my_referrals:{current_page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([btn('btn_back', callback_data='referral_program')])
    kbd = InlineKeyboardMarkup(inline_keyboard=buttons)

    await query.message.edit_text("\n".join(lines), reply_markup=kbd)
    await query.answer()

async def handle_cryptobot_invoice_paid(payment_id: str, payload_str: str) -> None:
    """Оплаченный инвойс CryptoBot: webhook @CryptoBot или кнопка «Проверить». Идемпотентно."""
    logger.info(
        f"CryptoBot: зачисление payment_id={payment_id}, len(payload)={len(payload_str or '')}"
    )
    db_payment = await db_helpers.get_payment(payment_id)
    if not db_payment:
        logger.error(f"CryptoBot: платёж {payment_id} не найден в БД")
        return
    if db_payment[4] == "succeeded":
        logger.debug(f"CryptoBot: {payment_id} уже succeeded — пропуск")
        return

    if not await db_helpers.try_mark_payment_as_processing(payment_id):
        logger.info(f"CryptoBot: {payment_id} уже обрабатывается или обработан")
        return

    tg_uid = int(db_payment[1] or 0)

    ps = (payload_str or "").strip()
    if not ps and db_payment[6]:
        try:
            meta = json.loads(db_payment[6])
            ps = (meta.get("payload") or "").strip()
        except Exception as e:
            logger.warning(f"CryptoBot: не удалось прочитать metadata_json {payment_id}: {e}")

    if not ps:
        logger.error(f"CryptoBot: пустой merchant payload для {payment_id}")
        await db_helpers.update_payment_status(payment_id, "canceled")
        if tg_uid:
            try:
                await bot.send_message(
                    tg_uid,
                    "Ошибка обработки платежа: не найден payload. Обратитесь в поддержку.",
                    reply_markup=keyboards.get_back_to_main_keyboard(),
                )
            except Exception:
                pass
        return

    # ── Продление трафика ────────────────────────────────────────────────────
    if "traffic_renewal" in ps:
        try:
            parts = ps.split("|")
            paid_user_id = int(parts[0])
        except Exception:
            logger.error(f"CryptoBot: некорректный payload трафика {ps!r} для {payment_id}")
            await db_helpers.update_payment_status(payment_id, "canceled")
            return

        db_payment = await db_helpers.get_payment(payment_id)
        if db_payment and db_payment[4] == "succeeded":
            return

        payment_metadata_db = {}
        if db_payment and db_payment[6]:
            try:
                payment_metadata_db = json.loads(db_payment[6])
            except Exception:
                pass

        payment_metadata_db["payment_type"] = "traffic_renewal"
        # Не выставляем 'succeeded' заранее — иначе process_successful_payment
        # уйдёт в retry-ветку (line ~813) с логом «уже был обработан».
        # Статус ставится сам в основном пути (line ~943/1063) после успеха.
        await process_successful_payment(paid_user_id, payment_id, payment_metadata_db)
        return

    # ── Продление подписки (формат user|tariff|days|limit_ip) ─────────────────
    try:
        user_id_str, tariff_id_str, days_str, limit_ip_str = ps.split("|")
        paid_user_id = int(user_id_str)
        days = int(days_str)
        limit_ip = int(limit_ip_str)
        tariff_id = int(tariff_id_str)
    except Exception:
        logger.error(f"CryptoBot: некорректный payload подписки {ps!r} для {payment_id}")
        await db_helpers.update_payment_status(payment_id, "canceled")
        return

    db_payment = await db_helpers.get_payment(payment_id)
    if db_payment and db_payment[4] == "succeeded":
        return

    traffic_gb_to_add = 0
    tariff = None
    try:
        tariff = await db_helpers.get_tariff_by_id(tariff_id)
        if tariff:
            traffic_gb_to_add = tariff.get("traffic_gb", 0) or 0
            if traffic_gb_to_add > 0:
                logger.info(
                    f"CryptoBot: к подписке добавим {traffic_gb_to_add} GB (тариф {tariff_id})"
                )
    except Exception as e:
        logger.warning(f"CryptoBot: не удалось получить traffic_gb тарифа {tariff_id}: {e}")

    try:
        subscription_data = await grant_subscription(
            paid_user_id, days, is_trial=False, limit_ip=limit_ip, traffic_gb_to_add=traffic_gb_to_add
        )
        if subscription_data:
            await db_helpers.update_payment_status(payment_id, "succeeded")
        else:
            logger.error(
                f"CryptoBot: grant_subscription вернул None для {payment_id} "
                f"(оплачен, подписка не выдана) → failed"
            )
            await db_helpers.update_payment_status(payment_id, "failed")
    except Exception as e:
        current = await db_helpers.get_payment(payment_id)
        current_status = current[4] if current and len(current) > 4 else None
        if current_status != "succeeded":
            logger.error(
                f"CryptoBot: ошибка grant_subscription для {payment_id}: {e}",
                exc_info=True,
            )
            await db_helpers.update_payment_status(payment_id, "failed")
        subscription_data = None

    if subscription_data:
        try:
            tariff_price = float((tariff or {}).get("price") or 0)
            await _apply_partner_and_referral(
                payer_user_id=paid_user_id,
                payment_id=payment_id,
                amount_rub=tariff_price,
                currency="RUB",
                log_prefix="CryptoBot, webhook",
            )
        except Exception as e:
            logger.error(f"Партнёрка/реферал (CryptoBot, webhook) ошибка: {e}")

    try:
        if subscription_data:
            expiry_date = subscription_data.get("expiry_date")
            if expiry_date and isinstance(expiry_date, datetime):
                expiry_date_str = format_msk_date(expiry_date)
            else:
                if expiry_date:
                    logger.warning(
                        f"CryptoBot: expiry_date не datetime для {payment_id}: {type(expiry_date)}"
                    )
                else:
                    logger.warning(
                        f"CryptoBot: нет expiry_date в subscription_data для {payment_id}"
                    )
                expiry_date_str = format_msk_date(
                    datetime.now(timezone.utc) + timedelta(days=days)
                )
                logger.warning(f"CryptoBot: fallback дата окончания для {payment_id}")

            remnawave_traffic_info = subscription_data.get("remnawave_traffic_info")
            traffic_info_text = ""
            if remnawave_traffic_info:
                added_gb = remnawave_traffic_info.get("added_gb")
                if added_gb:
                    traffic_info_text = f"\n\nТрафик: добавлено {added_gb} GB"

            tpl = (app_conf.get("text_payment_success") or "").replace("{sub_link}", "")
            success_message = tpl.format(days=days, expiry_date=expiry_date_str) + traffic_info_text

            await bot.send_message(
                paid_user_id,
                success_message,
                reply_markup=keyboards.get_success_with_referral_keyboard(),
            )
            logger.info(f"CryptoBot: уведомление об оплате отправлено user={paid_user_id}")
        else:
            await _notify_payment_grant_failed(paid_user_id)
            logger.warning(f"CryptoBot: подписка не выдана для {payment_id}, уведомление об ошибке")
    except Exception as e:
        logger.error(
            f"CryptoBot: ошибка уведомления пользователю {paid_user_id}: {e}",
            exc_info=True,
        )
        if subscription_data:
            try:
                await bot.send_message(
                    paid_user_id,
                    f"✓ Платёж обработан. Подписка продлена на {days} дней.",
                    reply_markup=keyboards.get_back_to_main_keyboard(),
                )
            except Exception as e2:
                logger.error(f"CryptoBot: не удалось отправить базовое сообщение: {e2}")

async def auto_check_payment_status(payment_id: str, user_id: int, payment_metadata: dict):
    logger.info(f"[AUTO_CHECK] Функция auto_check_payment_status вызвана для платежа {payment_id}, пользователя {user_id}, metadata: {payment_metadata}")
    start_time = datetime.now(timezone.utc)
    
    # Определяем тип платежа по ID в начале функции
    payment_type = None
    poll_interval = 10  # Интервал по умолчанию
    
    if payment_id.startswith("YOOMONEY_"):
        payment_type = "YooMoney"
        max_duration = timedelta(minutes=20)  # Увеличиваем время для YooMoney
    elif payment_id.startswith("CRYPTO_"):
        payment_type = "CryptoBot"
        max_duration = timedelta(minutes=60)
        poll_interval = 10  # не используется: CryptoBot — только webhook
    elif payment_id.startswith("TGSTAR_"):
        payment_type = "TG Star"
        max_duration = timedelta(minutes=4)
    elif payment_id.startswith("PLATEGA_"):
        payment_type = "Platega"
        max_duration = timedelta(minutes=15)
    else:
        payment_type = "YooKassa"
        max_duration = timedelta(minutes=15)
    
    logger.info(f"Запуск автопроверки для платежа {payment_id}, пользователя {user_id}.")
    logger.info(f"Автопроверка: Платеж {payment_id} - {payment_type}")

    try:
        iteration = 0
        while datetime.now(timezone.utc) - start_time < max_duration:
            iteration += 1
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            logger.info(f"Автопроверка {payment_id}: итерация {iteration}, прошло {elapsed:.1f} сек, тип: {payment_type}")
            
            db_payment_info = await db_helpers.get_payment(payment_id)
            if not db_payment_info:
                logger.warning(f"Автопроверка: Платеж {payment_id} не найден в БД. Остановка.")
                return
            if db_payment_info[4] != "pending":
                logger.info(f"Автопроверка: Платеж {payment_id} больше не 'pending' (статус: {db_payment_info[4]}). Остановка.")
                return

            # Проверяем статус платежа в зависимости от типа
            if payment_type == "YooMoney":
                # Для YooMoney НЕ используем автопроверку - платежи обрабатываются через webhook
                logger.info(f"Автопроверка: Платеж {payment_id} - YooMoney, автопроверка отключена (webhook)")
                return
            elif payment_type == "YooKassa":
                # Для YooKassa НЕ используем автопроверку - платежи обрабатываются через webhook
                logger.info(f"Автопроверка: Платеж {payment_id} - YooKassa, автопроверка отключена (webhook)")
                return
            elif payment_type == "Platega":
                # Для Platega НЕ используем автопроверку - платежи обрабатываются через webhook
                logger.info(f"Автопроверка: Платеж {payment_id} - Platega, автопроверка отключена (webhook)")
                return
                    
            elif payment_type == "CryptoBot":
                logger.info(
                    f"Автопроверка: {payment_id} — CryptoBot обрабатывается только через webhook, polling отключён"
                )
                return

            elif payment_type == "TG Star":
                # Для TG Star пока просто ждем (можно добавить проверку через API)
                logger.info(f"Автопроверка: Платеж {payment_id} - TG Star, ожидание...")
                await asyncio.sleep(20)  # Увеличиваем интервал для TG Star
                continue
                
            await asyncio.sleep(poll_interval)
        
        logger.warning(f"Автопроверка: Таймаут для платежа {payment_id}. Остался 'pending'.")
    except Exception as e:
        logger.error(f"Автопроверка: критическая ошибка для платежа {payment_id}: {e}", exc_info=True)
    finally:
        if payment_id in active_payment_checkers: 
            del active_payment_checkers[payment_id]
            logger.info(f"Автопроверка: задача для платежа {payment_id} удалена из active_payment_checkers")
        logger.info(f"Автопроверка для платежа {payment_id} завершена.")

@dp.callback_query(F.data == "activate_promo_code_prompt")
async def cq_activate_promo_code_prompt(query: CallbackQuery, state: FSMContext):
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
        
    await query.message.edit_text(app_conf.get('text_promo_code_prompt'), reply_markup=keyboards.get_back_to_main_keyboard())
    await state.set_state(PromoCodeActivation.waiting_for_code)
    await query.answer()

@dp.message(PromoCodeActivation.waiting_for_code)
async def process_promo_code_activation(message: Message, state: FSMContext):
    # Проверяем блокировку
    if await check_user_blocked(message.from_user.id):
        await state.clear()
        await send_blocked_message(message.from_user.id)
        return
        
    await state.clear()
    code = message.text.strip().upper()
    redeem = await db_helpers.redeem_promo_code(code, message.from_user.id)
    if not redeem.get('ok'):
        reason = redeem.get('error')
        if reason in ('not_found',):
            return await message.answer(app_conf.get('text_promo_code_invalid'), reply_markup=keyboards.get_back_to_main_keyboard())
        if reason in ('inactive', 'limit_reached', 'already_used_by_user'):
            return await message.answer(app_conf.get('text_promo_code_already_used'), reply_markup=keyboards.get_back_to_main_keyboard())
        return await message.answer(
            app_conf.get('text_error_general') or REST_TEXT_DEFAULTS['text_error_general'],
            reply_markup=keyboards.get_back_to_main_keyboard(),
        )

    days_to_add = redeem.get('days') or app_conf.get('promo_code_subscription_days', 30)
    # Получаем текущий лимит устройств пользователя
    # Используем _resolve_limit_ip_for_user для правильной обработки случая с истекшей подпиской
    # Эта функция проверяет сначала активную подписку, потом последнюю подписку, и только потом возвращает фолбэк
    current_limit_ip = await _resolve_limit_ip_for_user(message.from_user.id)
    # При применении промокода НЕ сбрасываем трафик
    subscription_data = await grant_subscription(message.from_user.id, days_to_add, is_trial=False, limit_ip=current_limit_ip, reset_traffic_on_renewal=False)

    if subscription_data:
        # Отдельной фиксации не требуется — redeem уже учёл использование и лимиты
        await message.answer(
            (app_conf.get('text_promo_code_success') or DEFAULT_PROMO_SUCCESS).format(
                code=code, days=days_to_add, expiry_date=format_msk_date(subscription_data['expiry_date'])
            ),
            reply_markup=keyboards.get_back_to_main_keyboard()
        )
        await show_main_menu(message)
    else:
        await message.answer(app_conf.get('text_error_creating_user'), reply_markup=keyboards.get_back_to_main_keyboard())

@dp.callback_query(F.data.startswith("check_payment_"))
async def cq_check_payment(query: CallbackQuery):
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
        
    payment_id = query.data.split("_")[2]
    await query.answer(app_conf.get('text_payment_checking'), show_alert=False)

    payment_db_data = await db_helpers.get_payment(payment_id)
    payment_metadata_from_db = json.loads(payment_db_data[6]) if payment_db_data and payment_db_data[6] else None

    try:
        # Обертываем синхронный вызов YooKassa в executor с таймаутом
        loop = asyncio.get_event_loop()
        payment_info_yk = await asyncio.wait_for(
            loop.run_in_executor(None, YKPayment.find_one, payment_id),
            timeout=10.0  # Таймаут 10 секунд для проверки платежа
        )
    except asyncio.TimeoutError:
        logger.error(f"Таймаут при запросе статуса платежа YooKassa {payment_id}")
        await safe_answer_callback(query, "⚠ Сервис временно недоступен. Попробуйте позже.", show_alert=True)
        return
    except Exception as e:
        logger.error(f"Ошибка ручного запроса Yookassa для {payment_id}: {e}")
        await safe_answer_callback(query, "⚠ Временная ошибка. Попробуйте позже.", show_alert=True)
        return

    if not payment_info_yk:
        return await query.message.edit_text(app_conf.get('text_payment_not_found'), reply_markup=keyboards.get_back_to_main_keyboard())

    if payment_info_yk.status == "succeeded":
        await process_successful_payment(query.from_user.id, payment_id, payment_metadata_from_db)
    elif payment_info_yk.status == "pending":
        await query.answer(app_conf.get('text_payment_pending'), show_alert=True) 
    elif payment_info_yk.status in ["canceled", "failed"]:
        await db_helpers.update_payment_status(payment_id, "canceled")
        await query.message.edit_text(app_conf.get('text_payment_canceled_or_failed'), reply_markup=keyboards.get_back_to_main_keyboard())


@dp.callback_query(F.data.startswith("renew_tariff_yookassa_"))
async def cq_renew_tariff_yookassa(query: CallbackQuery):
    """Обрабатывает нажатие на конкретный тариф для оплаты через YooKassa."""
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
        
    try:
        # Извлекаем ID тарифа из callback_data (например, из "renew_tariff_yookassa_1")
        tariff_id = int(query.data.split("_")[-1])
        tariff = await db_helpers.get_tariff_by_id(tariff_id)

        if not tariff:
            await query.answer("Ошибка: тариф не найден.", show_alert=True)
            logger.warning(f"Не найден тариф с ID {tariff_id} для пользователя {query.from_user.id}")
            return
    except (ValueError, IndexError):
        logger.error(f"Неверный формат callback_data для YooKassa: {query.data}")
        await query.answer("Ошибка в параметрах кнопки.", show_alert=True)
        return

    user_id = query.from_user.id
    idempotence_key = str(py_uuid.uuid4())
    
    days = tariff['days']
    price = float(tariff['price'])
    currency = tariff['currency']

    last_sub = await db_helpers.get_last_subscription(user_id)
    current_uuid = last_sub['xui_client_uuid'] if last_sub else None
    current_server_id = last_sub['current_server_id'] if last_sub else None

    payment_metadata = {
        "telegram_user_id": user_id, "subscription_days": days,
        "price": price, "limit_ip": tariff.get('limit_ip', 0),
        "tariff_id": tariff_id,
        "registration_type": "bot",  # симметрично с website (там "site")
        "bot_payment_uuid": idempotence_key, "is_renewal": bool(last_sub),
        "current_uuid": current_uuid, "current_server_id": current_server_id
    }

    email_domain = 'gmail.com'
    email = f"tg{user_id}@{email_domain}"

    # Флаг только СБП
    is_sbp_only = app_conf.get('yookassa_sbp_only', '0') == '1'

    builder = PaymentRequestBuilder()
    builder.set_amount({"value": f"{price:.2f}", "currency": currency}) \
        .set_capture(True) \
        .set_confirmation({"type": "redirect", "return_url": f"https://t.me/{(await bot.get_me()).username}"}) \
        .set_description(f"Оплата подписки: {tariff['name']}") \
        .set_metadata(payment_metadata) \
        .set_receipt({
            "customer": {"email": email},
            "items": [{
                "description": f"Подписка на telegram ({tariff['name']})",
                "quantity": "1.00",
                "amount": {"value": f"{price:.2f}", "currency": currency},
                "vat_code": 1,  # 1 = НДС не облагается
                "payment_mode": "full_payment",
                "payment_subject": "service"
            }]
        })

    # Применяем только СБП, если включено
    if is_sbp_only:
        try:
            builder.set_payment_method_data({"type": "sbp"})
        except Exception:
            pass
    
    try:
        # Обертываем синхронный вызов YooKassa в executor с таймаутом, чтобы не блокировать event loop
        loop = asyncio.get_event_loop()
        payment_response = await asyncio.wait_for(
            loop.run_in_executor(None, YKPayment.create, builder.build(), idempotence_key),
            timeout=5.0  # Таймаут 5 секунд для создания платежа
        )
        if payment_response.confirmation and payment_response.confirmation.confirmation_url:
            yk_payment_id = payment_response.id
            await db_helpers.add_payment(
                payment_id=yk_payment_id, telegram_id=user_id, amount=price,
                currency=currency, metadata_json=json.dumps(payment_metadata)
            )
            price_str = int(price) if price == int(price) else f"{price:.2f}"
            limit_ip = int(tariff.get('limit_ip', 0) or 0)
            limit_ip_display = "∞" if limit_ip == 0 else str(limit_ip)
            description_text = ""
            if tariff.get('description') and tariff['description'].strip():
                description_text = f"\n{tariff['description'].strip()}\n"
            
            # Обновляем сообщение, показывая кнопку оплаты
            await query.message.edit_text(
                txt_payment_renewal(days, limit_ip_display, price_str, currency, description_text),
                reply_markup=keyboards.get_payment_keyboard(
                    payment_id=yk_payment_id, 
                    payment_url=payment_response.confirmation.confirmation_url
                ),
                disable_web_page_preview=True,
                parse_mode="HTML"
            )
            
            # YooKassa теперь использует только webhook, polling не нужен
            logger.info(f"YooKassa: создан платеж {yk_payment_id} для пользователя {user_id}. Ожидаем webhook.")
        else:
            raise Exception("В ответе от YooKassa нет ссылки на оплату")
    except asyncio.TimeoutError:
        logger.error(f"Таймаут при создании платежа YooKassa для user {user_id}")
        await query.message.edit_text(
            "⚠ Сервис оплаты временно недоступен. Попробуйте позже или выберите другой способ.",
            reply_markup=keyboards.get_back_to_main_keyboard()
        )
        await safe_answer_callback(query)
    except Exception as e:
        error_msg = str(e).lower()
        logger.error(f"Ошибка создания платежа YooKassa для user {user_id}: {e}")
        
        # Более понятное сообщение для пользователя
        if 'timeout' in error_msg or 'connection' in error_msg or 'network' in error_msg:
            user_message = "⚠ Сервис оплаты временно недоступен. Попробуйте позже или выберите другой способ."
        else:
            user_message = (
                app_conf.get('text_error_general')
                or REST_TEXT_DEFAULTS['text_error_general']
            )
        
        await query.message.edit_text(
            user_message, 
            reply_markup=keyboards.get_back_to_main_keyboard()
        )
        await safe_answer_callback(query)

# --- Новый блок: выбор способа оплаты ---
@dp.callback_query(F.data == "renew_choose_payment")
async def cq_renew_choose_payment(query: CallbackQuery):
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return

    # Продление одно и наше. Ни одна наша кнопка сюда больше не ведёт, но
    # у клиентов в чатах висят сообщения, отправленные раньше: нажатие
    # на такую увело бы в родную ветку и двинуло срок учётке `tg{id}` —
    # подписке для приложения на телефоне. Клиент заплатил бы, а роутер
    # отключился по старой дате. Поэтому старая кнопка приводит сюда же,
    # куда и новая.
    from src.router_catalog import catalog_enabled, render_renew

    if catalog_enabled():
        await render_renew(query)
        return


    # Проверяем, включены ли основные методы оплаты
    yookassa_enabled = app_conf.get('show_payment_yookassa', '1') == '1'
    cryptobot_enabled = app_conf.get('show_payment_cryptobot', '1') == '1'
    yoomoney_enabled = app_conf.get('show_payment_yoomoney', '1') == '1'
    manual_enabled = app_conf.get('show_payment_manual', '0') == '1'

    # Получаем информацию о подписке (активной или последней, даже если истекла)
    active_sub = await db_helpers.get_active_subscription(query.from_user.id)
    last_sub = await db_helpers.get_last_subscription(query.from_user.id)
    
    # Используем активную подписку, если есть, иначе последнюю (даже если истекла)
    sub_info = active_sub if active_sub else last_sub
    
    # Формируем верхнюю часть сообщения с информацией о подписке
    header_text = ""
    if sub_info:
        # Оставшиеся дни до окончания
        expiry_date = sub_info['subscription_end_date']
        if expiry_date:
            if isinstance(expiry_date, str):
                expiry_date = datetime.fromisoformat(expiry_date)
            if expiry_date.tzinfo is None:
                expiry_date = expiry_date.replace(tzinfo=timezone.utc)
            
            now_utc = datetime.now(timezone.utc)
            if expiry_date.tzinfo:
                time_diff = expiry_date - now_utc
            else:
                time_diff = expiry_date.replace(tzinfo=timezone.utc) - now_utc
            
            # Проверяем, не истекла ли подписка (используем total_seconds для точности)
            total_seconds = time_diff.total_seconds()
            if total_seconds > 0:
                # Подписка еще активна
                days_left = time_diff.days
                hours_left = int((time_diff.seconds) / 3600)
                minutes_left = int((time_diff.seconds % 3600) / 60)
                
                # Формируем текст с информацией о подписке
                if days_left > 0:
                    days_text = f"{days_left} дн."
                    if hours_left > 0 and days_left < 3:
                        days_text += f" {hours_left} ч."
                elif hours_left > 0:
                    days_text = f"{hours_left} ч."
                    if minutes_left > 0:
                        days_text += f" {minutes_left} мин."
                elif minutes_left > 0:
                    days_text = f"{minutes_left} мин."
                else:
                    days_text = "Меньше минуты"
            else:
                days_text = "✕ Истекла"
        else:
            days_text = "✕ Истекла"
        
        # Лимит устройств
        limit_ip = sub_info.get('limit_ip', 0) if isinstance(sub_info, dict) else 0
        limit_ip_display = "∞" if limit_ip == 0 else str(limit_ip)
        
        header_text = txt_subscription_time_header(days_text, limit_ip_display)
        
    # Формируем список кнопок методов оплаты на основе настроек и payment_methods_order
    from payment_methods import build_bot_payment_keyboard_rows

    def _renew_cb(mid: str):
        return {
            'yookassa': 'renew_choose_yookassa',
            'tgstar': 'renew_choose_tgstar',
            'cryptobot': 'renew_choose_cryptobot',
            'yoomoney': 'renew_choose_yoomoney',
            'manual': 'renew_choose_manual',
            'platega': 'renew_choose_platega',
            'wata': 'renew_choose_wata',
            'promo': 'activate_promo_code_prompt',
        }.get(mid)

    payment_buttons = build_bot_payment_keyboard_rows(_renew_cb)

    # Кнопка «История платежей» — стилизуемая через реестр (см. button_registry.py)
    payment_buttons.append([btn('btn_payment_history', callback_data='payment_history')])

    # Кнопка "Назад"
    payment_buttons.append([btn('btn_back_to_main', callback_data='back_to_main')])
    
    kbd = InlineKeyboardMarkup(inline_keyboard=payment_buttons)
    
    # Формируем полный текст сообщения
    message_text = (
        "<b>Продление подписки</b>\n○ Выберите способ оплаты\n\n" + header_text
    )
    
    await query.message.edit_text(message_text, reply_markup=kbd, parse_mode="HTML")
    await safe_answer_callback(query)

@dp.callback_query(F.data == "renew_choose_platega")
async def cq_renew_choose_platega(query: CallbackQuery):
    """Тарифы Platega — тот же экран и фильтры, что у остальных способов оплаты."""
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
    await _render_renew_tariffs_for_method(query, 'platega')


# Обратная совместимость: старые callback_data renew_platega_method_X → редирект к выбору тарифа Platega
@dp.callback_query(F.data.startswith("renew_platega_method_"))
async def cq_renew_platega_method_legacy(query: CallbackQuery):
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
    await cq_renew_choose_platega(query)

@dp.callback_query(F.data.startswith("renew_tariff_platega_"))
async def cq_renew_tariff_platega(query: CallbackQuery):
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return

    try:
        # Формат: renew_tariff_platega_{tariff_id}
        # (поддерживается также старый: renew_tariff_platega_{method_id}_{tariff_id} — берём последний компонент)
        parts = query.data.split("_")
        tariff_id = int(parts[-1])

        tariff = await db_helpers.get_tariff_by_id(tariff_id)
        if not tariff:
            await query.answer("Тариф не найден.", show_alert=True)
            return
    except Exception as e:
        logger.error(f"Ошибка парсинга callback_data для Platega: {e}, data: {query.data}")
        await query.answer("Ошибка параметров.", show_alert=True)
        return

    # Разрешаем только тарифы Platega/both/all
    method_ok = ('platega', 'both', 'all', None)
    if tariff.get('payment_method') not in method_ok:
        await query.answer("Этот тариф недоступен для Platega.", show_alert=True)
        return

    user_id = query.from_user.id
    days = int(tariff['days'])
    try:
        price = float(tariff['price'])
    except Exception:
        price = float(app_conf.get('subscription_price', 0))
    currency = tariff.get('currency') or app_conf.get('subscription_currency', 'RUB')

    merchant_id = app_conf.get('platega_merchant_id')
    api_secret = app_conf.get('platega_api_secret')
    if not merchant_id or not api_secret:
        await query.message.edit_text("Platega: не настроены MerchantId или Secret в настройках.", reply_markup=keyboards.get_back_to_main_keyboard())
        await query.answer()
        return

    limit_ip = int(tariff.get('limit_ip') or app_conf.get('subscription_limit_ip', 0))

    payment_metadata = {
        "telegram_user_id": user_id,
        "subscription_days": days,
        "price": price,
        "limit_ip": limit_ip,
        "tariff_id": tariff_id,
        "registration_type": "bot",
        "cms_name": "platega",
    }

    bot_username = (await bot.get_me()).username
    return_url = f"https://t.me/{bot_username}"
    failed_url = f"https://t.me/{bot_username}"

    # v2: метод выбирает плательщик на странице Platega; transactionId генерирует Platega.
    payload = {
        "paymentDetails": {"amount": price, "currency": currency},
        "description": f"Оплата подписки: {tariff['name']}",
        "return": return_url,
        "failedUrl": failed_url,
        "payload": json.dumps(payment_metadata, ensure_ascii=False),
    }

    logger.info(f"Platega v2: создание платежа для пользователя {user_id}, тариф {tariff_id}")

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://app.platega.io/v2/transaction/process",
                headers={
                    "Content-Type": "application/json",
                    "X-MerchantId": str(merchant_id),
                    "X-Secret": str(api_secret),
                },
                json=payload
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.error(f"Platega v2: ошибка создания транзакции: {e}")
        await query.message.edit_text(
            app_conf.get('text_error_general') or REST_TEXT_DEFAULTS['text_error_general'],
            reply_markup=keyboards.get_back_to_main_keyboard(),
        )
        await query.answer()
        return

    tx_id = data.get('transactionId') or data.get('id')
    redirect_url = data.get('url') or data.get('redirect') or data.get('paymentLink')
    if not tx_id or not redirect_url:
        logger.error(f"Platega v2: неполный ответ: {data}")
        await query.message.edit_text(
            app_conf.get('text_error_general') or REST_TEXT_DEFAULTS['text_error_general'],
            reply_markup=keyboards.get_back_to_main_keyboard(),
        )
        await query.answer()
        return

    payment_id = f"PLATEGA_{tx_id}"

    try:
        await db_helpers.add_payment(payment_id, user_id, float(price), currency, json.dumps(payment_metadata, ensure_ascii=False))
    except Exception as e:
        logger.error(f"Platega: не удалось сохранить платеж в БД: {e}")

    # Кнопка оплатить (текст/стиль из реестра btn_payment_pay_link)
    kbd = InlineKeyboardMarkup(inline_keyboard=[
        [btn('btn_payment_pay_link', url=redirect_url)],
        [btn('btn_back_to_main', callback_data='back_to_main')]
    ])
    try:
        price_val = float(price)
        price_str = int(price_val) if price_val.is_integer() else price_val
    except Exception:
        price_str = price
    limit_ip_display = "∞" if limit_ip == 0 else str(limit_ip)
    description_text = ""
    if tariff.get('description') and tariff['description'].strip():
        description_text = f"\n{tariff['description'].strip()}\n"
    
    await query.message.edit_text(
        txt_payment_renewal(days, limit_ip_display, price_str, currency, description_text),
        reply_markup=kbd,
        parse_mode="HTML",
        disable_web_page_preview=True
    )

    # Platega теперь использует только webhook, polling не нужен
    logger.info(f"Platega: создан платеж {payment_id} для пользователя {user_id}. Ожидаем webhook.")
    await query.answer()


@dp.callback_query(F.data.startswith("renew_tariff_wata_"))
async def cq_renew_tariff_wata(query: CallbackQuery):
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return

    try:
        parts = query.data.split("_")
        tariff_id = int(parts[-1])
        tariff = await db_helpers.get_tariff_by_id(tariff_id)
        if not tariff:
            await query.answer("Тариф не найден.", show_alert=True)
            return
    except Exception as e:
        logger.error(f"Wata: парсинг callback {query.data}: {e}")
        await query.answer("Ошибка параметров.", show_alert=True)
        return

    method_ok = ("wata", "both", "all", None)
    if tariff.get("payment_method") not in method_ok:
        await query.answer("Этот тариф недоступен для Wata.", show_alert=True)
        return

    user_id = query.from_user.id
    days = int(tariff["days"])
    try:
        price = float(tariff["price"])
    except Exception:
        price = float(app_conf.get("subscription_price", 0))
    currency = tariff.get("currency") or app_conf.get("subscription_currency", "RUB")

    access_token = (app_conf.get("wata_access_token") or "").strip() or (
        os.getenv("WATA_ACCESS_TOKEN") or ""
    ).strip()
    terminal_public_id = (app_conf.get("wata_terminal_public_id") or "").strip() or (
        os.getenv("WATA_TERMINAL_PUBLIC_ID") or ""
    ).strip()

    if not access_token:
        await query.message.edit_text(
            "Wata: не задан Access Token (настройки или WATA_ACCESS_TOKEN).",
            reply_markup=keyboards.get_back_to_main_keyboard(),
        )
        await query.answer()
        return

    limit_ip = int(tariff.get("limit_ip") or app_conf.get("subscription_limit_ip", 0))

    payment_metadata = {
        "telegram_user_id": user_id,
        "subscription_days": days,
        "price": price,
        "limit_ip": limit_ip,
        "tariff_id": tariff_id,
        "registration_type": "bot",
        "cms_name": "wata",
        "payment_method": "Wata",
    }

    payment_id = f"WATA_{py_uuid.uuid4()}"

    bot_username = (await bot.get_me()).username
    return_url = f"https://t.me/{bot_username}"
    extra = {"publicId": terminal_public_id} if terminal_public_id else None

    logger.info(f"Wata: создание ссылки user={user_id} tariff={tariff_id} payment_id={payment_id}")

    result = await create_wata_payment_link(
        access_token,
        amount=price,
        currency=currency,
        order_id=payment_id,
        description=f"Оплата подписки: {tariff['name']}",
        link_type="OneTime",
        success_redirect_url=return_url,
        fail_redirect_url=return_url,
        extra_json=extra,
    )

    if not result.get("ok"):
        err = result.get("error") or "unknown"
        logger.error(f"Wata: create link failed: {err}")
        await query.message.edit_text(
            app_conf.get("text_error_general") or REST_TEXT_DEFAULTS['text_error_general'],
            reply_markup=keyboards.get_back_to_main_keyboard(),
        )
        await query.answer()
        return

    pay_url = result.get("url")
    if not pay_url:
        await query.message.edit_text(
            app_conf.get("text_error_general") or REST_TEXT_DEFAULTS['text_error_general'],
            reply_markup=keyboards.get_back_to_main_keyboard(),
        )
        await query.answer()
        return

    try:
        await db_helpers.add_payment(
            payment_id,
            user_id,
            float(price),
            currency,
            json.dumps(payment_metadata, ensure_ascii=False),
        )
    except Exception as e:
        logger.error(f"Wata: не удалось сохранить платеж в БД: {e}")

    kbd = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn('btn_payment_pay_link', url=pay_url)],
            [btn("btn_back_to_main", callback_data="back_to_main")],
        ]
    )
    try:
        price_val = float(price)
        price_str = int(price_val) if price_val.is_integer() else price_val
    except Exception:
        price_str = price
    limit_ip_display = "∞" if limit_ip == 0 else str(limit_ip)
    description_text = ""
    if tariff.get("description") and str(tariff["description"]).strip():
        description_text = f"\n{str(tariff['description']).strip()}\n"

    await query.message.edit_text(
        txt_payment_renewal(days, limit_ip_display, price_str, currency, description_text),
        reply_markup=kbd,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    logger.info(f"Wata: платеж {payment_id} для пользователя {user_id}, ожидаем webhook.")
    await query.answer()


@dp.callback_query(F.data == "renew_choose_manual")
async def cq_renew_choose_manual(query: CallbackQuery):
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
    uid = query.from_user.id
    link30 = app_conf.get('manual_transfer_link_30')
    link60 = app_conf.get('manual_transfer_link_60')
    link90 = app_conf.get('manual_transfer_link_90')
    text = (
        "<b>Оплата CloudTips</b>\n○ Выберите срок\n\n"
        f"Ваш ID: <code>{uid}</code>\n"
        "Укажите ID в комментарии к оплате."
    )
    kb_rows = []
    if link30:
        kb_rows.append([btn('btn_renew_30', url=link30)])
    if link60:
        kb_rows.append([btn('btn_renew_60', url=link60)])
    if link90:
        kb_rows.append([btn('btn_renew_90', url=link90)])
    # Кнопка подтверждения оплаты переводом
    kb_rows.append([InlineKeyboardButton(text="Я оплатил", callback_data="manual_i_paid")])
    kb_rows.append([btn('btn_back', callback_data='renew_choose_payment')])
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await query.answer()

@dp.callback_query(F.data == "manual_i_paid")
async def cq_manual_i_paid(query: CallbackQuery):
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
    user_id = query.from_user.id
    username = query.from_user.username or "без username"
    support_link = app_conf.get('support_link')
    # Сообщение пользователю
    msg = (
        "<b>Заявка принята</b>\n✓ Проверим оплату\n\n"
        "Подписка обновится в течение 30 минут.\n"
        + (f"Если продление не произошло — обратитесь в поддержку: {support_link}" if support_link else "Если продление не произошло — обратитесь в поддержку.")
    )
    try:
        await query.message.edit_text(msg, reply_markup=keyboards.get_back_to_main_keyboard())
    except Exception:
        await bot.send_message(user_id, msg, reply_markup=keyboards.get_back_to_main_keyboard())
    await query.answer()

    # Уведомление администраторам
    admin_ids = []
    admins_str = app_conf.get('admin_ids')
    if admins_str:
        try:
            admin_ids = [int(x.strip()) for x in admins_str.split(',') if x.strip().isdigit()]
        except Exception:
            admin_ids = []
    # Фоллбэк: один получатель из настроек бэкапа (если admin_ids не задан)
    if not admin_ids:
        try:
            async with db_helpers.get_db_connection_safe() as db:
                async with db.execute("SELECT admin_telegram_id FROM backup_settings LIMIT 1") as cur:
                    row = await cur.fetchone()
                    if row and row[0]:
                        try:
                            admin_ids = [int(str(row[0]).strip())]
                        except Exception:
                            admin_ids = []
        except Exception as _e:
            logger.warning(f"manual_i_paid: не удалось получить admin из backup_settings: {_e}")

    admin_text = txt_admin_manual_payment_notify(user_id, username)
    if not admin_ids:
        logger.warning("manual_i_paid: список админов пуст — уведомление никому не отправлено")
    else:
        for aid in admin_ids:
            try:
                await bot.send_message(aid, admin_text, disable_web_page_preview=True)
            except Exception as send_err:
                logger.warning(f"manual_i_paid: не удалось отправить админу {aid}: {send_err}")

async def _render_renew_tariffs_for_method(query: CallbackQuery, method: str):
    """Показывает клиенту тарифы под его текущий limit_ip для выбранного метода
    оплаты (yookassa / cryptobot / tgstar / yoomoney / manual / ...).

    Используется как из основного хендлера cq_renew_choose_method, так и из
    legacy-редиректов (renew_increase_limit_*, renew_limit_*)."""
    tariffs = await db_helpers.get_active_tariffs()
    method_ok = (method, 'both', 'all', None)
    filtered = [t for t in tariffs if t.get('payment_method') in method_ok]
    if not filtered:
        await query.message.edit_text(
            "Нет доступных тарифов для выбранного способа оплаты.",
            reply_markup=keyboards.get_back_to_main_keyboard(),
        )
        await query.answer()
        return

    ctx_rlim = await _renewal_limit_ui_context(query.from_user.id)

    # Фильтруем тарифы под текущий limit_ip пользователя.
    # Для новичков (нет подписок в истории) — fallback к минимальному лимиту.
    visible, target_limit, _, tariff_hint_html = await _filter_tariffs_by_user_limit(
        query.from_user.id, filtered, ctx=ctx_rlim,
    )

    if not visible:
        kbd_empty = InlineKeyboardMarkup(inline_keyboard=[[
            btn('btn_back', callback_data='renew_choose_payment')
        ]])
        await query.message.edit_text(
            _no_tariffs_for_limit_text(target_limit),
            reply_markup=kbd_empty,
            parse_mode='HTML',
        )
        await query.answer()
        return

    try:
        visible.sort(key=lambda t: (float(t.get('price', 0) or 0), int(t.get('days', 0) or 0)))
    except Exception:
        pass

    rows = []
    for t in visible:
        try:
            price_val = float(t['price'])
            price_str = int(price_val) if price_val.is_integer() else f"{price_val:g}"
        except Exception:
            price_str = t.get('price', '-')

        days = int(t.get('days', 0) or 0)

        rows.append([
            InlineKeyboardButton(
                text=f"{days} дней · {price_str} {t['currency']}",
                callback_data=f"renew_tariff_{method}_{t['id']}",
            )
        ])

    try:
        unique_limits = {int(t.get('limit_ip', 0) or 0) for t in filtered}
    except Exception:
        unique_limits = set()

    show_change_limit_btn = len(unique_limits) > 1

    if show_change_limit_btn:
        rows.append([InlineKeyboardButton(
            text="Изменить лимит",
            callback_data=f"renew_increase_limit_{method}",
        )])

    rows.append([btn('btn_back', callback_data='renew_choose_payment')])
    kbd = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await query.message.edit_text(
            "<b>Выберите подходящий тариф:</b>" + tariff_hint_html,
            reply_markup=kbd,
            parse_mode='HTML',
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await query.answer()


# --- Показываем тарифы для выбранного способа ---
@dp.callback_query(F.data.startswith("renew_choose_"))
async def cq_renew_choose_method(query: CallbackQuery):
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return

    method = query.data.replace("renew_choose_", "")
    await _render_renew_tariffs_for_method(query, method)


@dp.callback_query(F.data.startswith("renew_increase_limit_"))
async def cq_renew_increase_limit(query: CallbackQuery):
    """Экран выбора лимита устройств → затем тарифы с этим лимитом.

    Показывается при нескольких лимитах в тарифах. Окно продления и pro-rata
    ушли вместе с расширением лимита: у роутера за подпиской домашняя сеть.
    """
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return

    # Парсим callback_data: renew_increase_limit_<method>
    # Поддерживается legacy: renew_increase_limit_platega_<method_id> (method_id игнорируем).
    method = "yookassa"
    try:
        payload = query.data.replace("renew_increase_limit_", "", 1)
        parts = payload.split("_") if payload else []
        if parts:
            method = parts[0] or "yookassa"
    except Exception:
        pass

    try:
        tariffs = await db_helpers.get_active_tariffs()
    except Exception as e:
        logger.warning(f"renew_increase_limit: get_active_tariffs failed: {e}")
        tariffs = []

    method_ok = (method, 'both', 'all', None)
    filtered = [t for t in tariffs if t.get('payment_method') in method_ok]
    if not filtered:
        await query.message.edit_text(
            "Нет доступных тарифов для выбранного способа оплаты.",
            reply_markup=keyboards.get_back_to_main_keyboard(),
        )
        await query.answer()
        return

    # Текущий лимит пользователя — чтобы пометить «текущий ⭐️».
    try:
        user_current_limit = await _resolve_limit_ip_for_user(query.from_user.id)
    except Exception:
        user_current_limit = None

    # Считаем минимальную цену в каждой группе для информативной подписи.
    by_limit: dict[int, float] = {}
    for t in filtered:
        try:
            lim = int(t.get('limit_ip', 0) or 0)
            price = float(t.get('price', 0) or 0)
        except Exception:
            continue
        if lim not in by_limit or price < by_limit[lim]:
            by_limit[lim] = price

    if not by_limit:
        await query.message.edit_text(
            "Нет доступных тарифов для выбранного способа оплаты.",
            reply_markup=keyboards.get_back_to_main_keyboard(),
        )
        await query.answer()
        return

    if len(by_limit) <= 1:
        # Группировать нечего — возвращаемся в обычный список тарифов.
        if method == "platega":
            await cq_renew_choose_platega(query)
        else:
            await _render_renew_tariffs_for_method(query, method)
        return

    # Сортируем: лимит=0 (∞) уезжает в конец, остальное по возрастанию.
    sorted_limits = sorted(by_limit.keys(), key=lambda x: (x == 0, x))

    if upgrade_on and renewal_pick:
        ctx_lim = await _renewal_limit_ui_context(query.from_user.id)
        cur_lim = int(ctx_lim['user_limit'] or 0)
        sorted_limits = [
            lim for lim in sorted_limits
            if _tariff_limit_same_or_higher_for_renewal(int(lim), cur_lim)
        ]
        if len(sorted_limits) <= 1:
            if method == "platega":
                await cq_renew_choose_platega(query)
            else:
                await _render_renew_tariffs_for_method(query, method)
            return

    rows = []
    for lim in sorted_limits:
        label = _format_device_limit_label(lim)
        if user_current_limit is not None and lim == user_current_limit:
            label += " · текущий"

        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"renew_limit_{method}_{lim}",
        )])

    back_callback = (
        "renew_choose_platega" if method == "platega"
        else f"renew_choose_{method}"
    )
    rows.append([btn('btn_back', callback_data=back_callback)])

    kbd = InlineKeyboardMarkup(inline_keyboard=rows)
    await query.message.edit_text(
        "Выберите желаемый лимит устройств:",
        reply_markup=kbd,
    )
    await query.answer()


# --- Платный сброс трафика отдельной кнопкой удалён из бота. ---
# Автоматический сброс при продлении подписки в "No Limit+" остаётся
# в grant_subscription через reset_traffic_on_renewal=True.


# --- Обработчики для платного продления трафика Remnawave ---

def _traffic_renewal_payment_text(
    traffic_gb: int | float,
    price_str: str,
    currency: str = 'RUB',
) -> str:
    """Единый текст экрана оплаты докупки трафика для всех провайдеров."""
    return txt_traffic_renewal_payment(
        traffic_gb,
        price_str,
        currency,
        template=app_conf.get('text_traffic_renewal_payment'),
    )


@dp.callback_query(F.data == "traffic_renewal_choose_payment")
async def cq_traffic_renewal_choose_payment(query: CallbackQuery):
    """Выбор метода оплаты для продления трафика"""
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
    
    user_id = query.from_user.id
    
    # Проверяем, что функция включена
    if app_conf.get('traffic_renewal_enabled', '0') != '1':
        await query.answer("Продление трафика временно недоступно", show_alert=True)
        return
    
    # Проверяем, что пользователь имеет Remnawave подписку
    user_data = await db_helpers.get_user(user_id)
    if not user_data:
        await query.answer("Пользователь не найден", show_alert=True)
        return
    
    # Преобразуем Row в словарь
    user_dict = dict(user_data)
    if not user_dict.get('xui_client_uuid'):
        await query.answer("Для покупки трафика нужна активная подписка", show_alert=True)
        return
    
    # Получаем информацию о трафике
    remnawave_short_uuid = user_dict.get('remnawave_short_uuid')
    traffic_info_text = ""
    traffic_limit_gb = 0
    traffic_used_gb = 0
    traffic_remaining_gb = 0
    
    if remnawave_short_uuid:
        try:
            remnawave_data = await remnawave_manager_instance.get_subscription_info(remnawave_short_uuid)
            if remnawave_data:
                traffic_info_text = format_traffic_section(
                    remnawave_data.get('trafficUsedBytes', 0),
                    remnawave_data.get('trafficLimitBytes', 0),
                )
        except Exception as e:
            logger.warning(f"Не удалось получить информацию о трафике: {e}")
    
    # Получаем тарифы докупки трафика из базы данных
    traffic_topup_tariffs = await db_helpers.get_active_traffic_topup_tariffs()
    
    if not traffic_topup_tariffs:
        await query.answer("Тарифы докупки трафика не настроены", show_alert=True)
        return
    
    header_text = txt_traffic_renewal_select(
        traffic_info_text,
        template=app_conf.get('text_traffic_renewal_select'),
    )
    
    # Формируем кнопки для каждого тарифа
    payment_buttons = []
    for tariff in traffic_topup_tariffs:
        tariff_id = tariff.get('id')
        tariff_gb = tariff.get('traffic_gb', 0)
        tariff_price = tariff.get('price', 0)
        price_str = int(tariff_price) if tariff_price == int(tariff_price) else f"{tariff_price:.2f}"
        button_text = f"{tariff_gb} GB · {price_str} ₽"
        payment_buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"traffic_renewal_choose_tariff_{tariff_id}")])
    
    # Добавляем кнопку "Назад"
    payment_buttons.append([btn('btn_back_to_main', callback_data='back_to_main')])
    
    kbd = InlineKeyboardMarkup(inline_keyboard=payment_buttons)
    await query.message.edit_text(
        header_text,
        reply_markup=kbd,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await safe_answer_callback(query)


@dp.callback_query(F.data.startswith("traffic_renewal_choose_tariff_"))
async def cq_traffic_renewal_choose_tariff(query: CallbackQuery):
    """Выбор тарифа докупки трафика и показ способов оплаты"""
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
    
    try:
        tariff_id = int(query.data.split("_")[-1])
    except Exception:
        await query.answer("Ошибка параметров.", show_alert=True)
        return
    
    user_id = query.from_user.id
    
    # Получаем тариф из базы данных
    tariff = await db_helpers.get_traffic_topup_tariff_by_id(tariff_id)
    if not tariff:
        await query.answer("Тариф не найден", show_alert=True)
        return
    
    tariff_dict = dict(tariff)
    if not tariff_dict.get('is_active'):
        await query.answer("Тариф неактивен", show_alert=True)
        return
    
    tariff_gb = tariff_dict.get('traffic_gb', 0)
    tariff_price = tariff_dict.get('price', 0)
    tariff_name = tariff_dict.get('name', f'{tariff_gb} GB')
    
    # Получаем информацию о текущем трафике
    user_data = await db_helpers.get_user(user_id)
    if not user_data:
        await query.answer("Пользователь не найден", show_alert=True)
        return
    
    user_dict = dict(user_data)
    remnawave_short_uuid = user_dict.get('remnawave_short_uuid')
    traffic_limit_gb = 0
    
    if remnawave_short_uuid:
        try:
            remnawave_data = await remnawave_manager_instance.get_subscription_info(remnawave_short_uuid)
            if remnawave_data:
                traffic_limit_bytes = remnawave_data.get('trafficLimitBytes', 0)
                if traffic_limit_bytes > 0:
                    traffic_limit_gb = traffic_limit_bytes / (1024 ** 3)
        except Exception:
            pass
    
    new_traffic_limit_gb = traffic_limit_gb + tariff_gb if traffic_limit_gb > 0 else tariff_gb
    price_str = int(tariff_price) if tariff_price == int(tariff_price) else f"{tariff_price:.2f}"
    
    header_text = txt_traffic_renewal_confirm(
        tariff_name,
        tariff_gb,
        new_traffic_limit_gb,
        price_str,
        template=app_conf.get('text_traffic_renewal_confirm'),
    )
    
    # Формируем список кнопок методов оплаты (порядок — payment_methods_order).
    from payment_methods import build_bot_payment_keyboard_rows

    def _traffic_cb(mid: str):
        return {
            'yookassa': f"traffic_renewal_tariff_{tariff_id}_yookassa",
            'tgstar': f"traffic_renewal_tariff_{tariff_id}_tgstar",
            'cryptobot': f"traffic_renewal_tariff_{tariff_id}_cryptobot",
            'yoomoney': f"traffic_renewal_tariff_{tariff_id}_yoomoney",
            'manual': f"traffic_renewal_tariff_{tariff_id}_manual",
            'platega': f"traffic_renewal_tariff_{tariff_id}_platega",
            'wata': f"traffic_renewal_tariff_{tariff_id}_wata",
        }.get(mid)

    payment_buttons = build_bot_payment_keyboard_rows(_traffic_cb, exclude=frozenset({'promo', 'manual'}))

    payment_buttons.append([btn('btn_back', callback_data='traffic_renewal_choose_payment')])
    
    kbd = InlineKeyboardMarkup(inline_keyboard=payment_buttons)
    await query.message.edit_text(
        header_text,
        reply_markup=kbd,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await safe_answer_callback(query)


@dp.callback_query((F.data == "traffic_renewal_choose_yookassa") | (F.data.startswith("traffic_renewal_tariff_") & F.data.endswith("_yookassa")))
async def cq_traffic_renewal_choose_yookassa(query: CallbackQuery):
    """Создание платежа YooKassa для продления трафика"""
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
    
    user_id = query.from_user.id
    
    # Проверяем условия
    if app_conf.get('traffic_renewal_enabled', '0') != '1':
        await query.answer("Продление трафика временно недоступно", show_alert=True)
        return
    
    user_data = await db_helpers.get_user(user_id)
    if not user_data:
        await query.answer("Пользователь не найден", show_alert=True)
        return
    
    # Преобразуем Row в словарь
    user_dict = dict(user_data)
    if not user_dict.get('xui_client_uuid'):
        await query.answer("Для покупки трафика нужна активная подписка", show_alert=True)
        return
    
    # Проверяем, есть ли ID тарифа в callback_data
    tariff_id = None
    tariff_gb = None
    tariff_price = None
    
    if query.data.startswith("traffic_renewal_tariff_"):
        # Новый формат с тарифом
        try:
            parts = query.data.split("_")
            tariff_id = int(parts[3])
            tariff = await db_helpers.get_traffic_topup_tariff_by_id(tariff_id)
            if not tariff:
                await query.answer("Тариф не найден", show_alert=True)
                return
            tariff_dict = dict(tariff)
            if not tariff_dict.get('is_active'):
                await query.answer("Тариф неактивен", show_alert=True)
                return
            tariff_gb = tariff_dict.get('traffic_gb', 0)
            tariff_price = tariff_dict.get('price', 0)
        except Exception as e:
            logger.error(f"Ошибка при получении тарифа: {e}")
            await query.answer("Ошибка при получении тарифа", show_alert=True)
            return
    
    if tariff_id is None:
        # Старая логика - используем настройки
        default_traffic_limit_gb = get_default_limit_gb()
        
        if default_traffic_limit_gb <= 0:
            await query.answer("Лимит трафика по умолчанию не установлен", show_alert=True)
            return
        
        tariff_gb = default_traffic_limit_gb
        tariff_price = float('100')
    
    price = tariff_price
    currency = 'RUB'
    idempotence_key = str(py_uuid.uuid4())
    
    payment_metadata = {
        "payment_type": "traffic_renewal",
        "telegram_user_id": user_id,
        "price": price,
        "traffic_to_add_gb": tariff_gb,
        "registration_type": "bot",  # симметрично с website (там "site")
        "payment_method": "YooKassa"
    }
    if tariff_id:
        payment_metadata["tariff_id"] = tariff_id
    
    email_domain = 'gmail.com'
    email = f"tg{user_id}@{email_domain}"
    
    # Флаг только СБП
    is_sbp_only = app_conf.get('yookassa_sbp_only', '0') == '1'
    
    builder = PaymentRequestBuilder()
    builder.set_amount({"value": f"{price:.2f}", "currency": currency}) \
        .set_capture(True) \
        .set_confirmation({"type": "redirect", "return_url": app_conf.get('yookassa_return_url', 'https://t.me/')}) \
        .set_description(f"Продление трафика (+{tariff_gb} GB)") \
        .set_metadata({"order_id": idempotence_key, "user_id": str(user_id), "payment_metadata": json.dumps(payment_metadata)}) \
        .set_receipt({
            "customer": {"email": email},
            "items": [{
                "description": f"Продление трафика (+{tariff_gb} GB)",
                "quantity": "1.00",
                "amount": {"value": f"{price:.2f}", "currency": currency},
                "vat_code": 1,
                "payment_mode": "full_payment",
                "payment_subject": "service"
            }]
        })
    
    if is_sbp_only:
        try:
            builder.set_payment_method_data({"type": "sbp"})
        except Exception:
            pass
    
    try:
        loop = asyncio.get_event_loop()
        payment_response = await asyncio.wait_for(
            loop.run_in_executor(None, YKPayment.create, builder.build(), idempotence_key),
            timeout=5.0
        )
        if payment_response.confirmation and payment_response.confirmation.confirmation_url:
            yk_payment_id = payment_response.id
            await db_helpers.add_payment(
                payment_id=yk_payment_id, telegram_id=user_id, amount=price,
                currency=currency, metadata_json=json.dumps(payment_metadata)
            )
            price_str = int(price) if price == int(price) else f"{price:.2f}"
            
            await query.message.edit_text(
                _traffic_renewal_payment_text(tariff_gb, price_str, currency),
                reply_markup=keyboards.get_payment_keyboard(yk_payment_id, payment_response.confirmation.confirmation_url),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await safe_answer_callback(query)
        else:
            await query.answer("Ошибка создания платежа", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка создания платежа YooKassa для продления трафика: {e}")
        await query.answer("Ошибка создания платежа. Попробуйте позже.", show_alert=True)


@dp.callback_query((F.data == "traffic_renewal_choose_platega") | (F.data.startswith("traffic_renewal_tariff_") & F.data.endswith("_platega")))
async def cq_traffic_renewal_choose_platega(query: CallbackQuery):
    """Создание платежа Platega для продления трафика"""
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
    
    user_id = query.from_user.id
    
    # Проверяем условия
    if app_conf.get('traffic_renewal_enabled', '0') != '1':
        await query.answer("Продление трафика временно недоступно", show_alert=True)
        return
    
    user_data = await db_helpers.get_user(user_id)
    if not user_data:
        await query.answer("Пользователь не найден", show_alert=True)
        return
    
    # Преобразуем Row в словарь
    user_dict = dict(user_data)
    if not user_dict.get('xui_client_uuid'):
        await query.answer("Для покупки трафика нужна активная подписка", show_alert=True)
        return
    
    # Проверяем, есть ли ID тарифа в callback_data
    tariff_id = None
    tariff_gb = None
    tariff_price = None
    
    if query.data.startswith("traffic_renewal_tariff_"):
        # Новый формат с тарифом
        try:
            parts = query.data.split("_")
            tariff_id = int(parts[3])
            tariff = await db_helpers.get_traffic_topup_tariff_by_id(tariff_id)
            if not tariff:
                await query.answer("Тариф не найден", show_alert=True)
                return
            tariff_dict = dict(tariff)
            if not tariff_dict.get('is_active'):
                await query.answer("Тариф неактивен", show_alert=True)
                return
            tariff_gb = tariff_dict.get('traffic_gb', 0)
            tariff_price = tariff_dict.get('price', 0)
        except Exception as e:
            logger.error(f"Ошибка при получении тарифа: {e}")
            await query.answer("Ошибка при получении тарифа", show_alert=True)
            return
    
    if tariff_id is None:
        # Старая логика - используем настройки
        default_traffic_limit_gb = get_default_limit_gb()
        
        if default_traffic_limit_gb <= 0:
            await query.answer("Лимит трафика по умолчанию не установлен", show_alert=True)
            return
        
        tariff_gb = default_traffic_limit_gb
        tariff_price = float('100')
    
    price = tariff_price
    currency = 'RUB'

    # v2: метод выбирает плательщик на странице Platega → платёж сразу
    await _create_traffic_renewal_platega_payment(query, user_id, price, currency, tariff_gb, tariff_id)


async def _create_traffic_renewal_platega_payment(query: CallbackQuery, user_id: int, price: float, currency: str, traffic_to_add_gb: float, tariff_id: int = None):
    """Создание платежа Platega v2 для продления трафика."""
    merchant_id = app_conf.get('platega_merchant_id')
    api_secret = app_conf.get('platega_api_secret')

    if not merchant_id or not api_secret:
        await query.answer("Platega не настроен", show_alert=True)
        return

    payment_metadata = {
        "payment_type": "traffic_renewal",
        "telegram_user_id": user_id,
        "price": price,
        "traffic_to_add_gb": traffic_to_add_gb,
        "registration_type": "bot",
        "cms_name": "platega",
    }
    if tariff_id:
        payment_metadata["tariff_id"] = tariff_id

    bot_username = (await bot.get_me()).username
    return_url = f"https://t.me/{bot_username}"
    failed_url = f"https://t.me/{bot_username}"

    payload = {
        "paymentDetails": {"amount": price, "currency": currency},
        "description": f"Продление трафика (+{traffic_to_add_gb} GB)",
        "return": return_url,
        "failedUrl": failed_url,
        "payload": json.dumps(payment_metadata, ensure_ascii=False),
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://app.platega.io/v2/transaction/process",
                headers={
                    "Content-Type": "application/json",
                    "X-MerchantId": str(merchant_id),
                    "X-Secret": str(api_secret),
                },
                json=payload
            )
            response.raise_for_status()
            data_resp = response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"Platega v2 traffic_renewal: HTTP {e.response.status_code}: {e.response.text}")
        await query.answer("Ошибка создания платежа. Попробуйте позже.", show_alert=True)
        return
    except Exception as e:
        logger.error(f"Platega v2 traffic_renewal: ошибка: {e}", exc_info=True)
        await query.answer("Ошибка создания платежа. Попробуйте позже.", show_alert=True)
        return

    tx_id = data_resp.get('transactionId') or data_resp.get('id')
    redirect_url = data_resp.get('url') or data_resp.get('redirect') or data_resp.get('paymentLink')

    if not tx_id or not redirect_url:
        logger.error(f"Platega v2 traffic_renewal: неполный ответ: {data_resp}")
        await query.message.edit_text(
            app_conf.get('text_error_general') or REST_TEXT_DEFAULTS['text_error_general'],
            reply_markup=keyboards.get_back_to_main_keyboard()
        )
        await query.answer()
        return

    payment_id = f"PLATEGA_{tx_id}"

    await db_helpers.add_payment(
        payment_id, user_id, float(price), currency,
        json.dumps(payment_metadata, ensure_ascii=False)
    )

    price_str = int(price) if price == int(price) else f"{price:.2f}"

    kbd = InlineKeyboardMarkup(inline_keyboard=[
        [btn('btn_payment_pay_link', url=redirect_url)],
        [btn('btn_back_to_main', callback_data='back_to_main')]
    ])

    await query.message.edit_text(
        _traffic_renewal_payment_text(traffic_to_add_gb, price_str, currency),
        reply_markup=kbd,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    logger.info(f"Platega v2: создан платеж {payment_id} для продления трафика пользователя {user_id}. Ожидаем webhook.")
    await safe_answer_callback(query)


@dp.callback_query((F.data == "traffic_renewal_choose_wata") | (F.data.startswith("traffic_renewal_tariff_") & F.data.endswith("_wata")))
async def cq_traffic_renewal_choose_wata(query: CallbackQuery):
    """Создание платежа Wata для докупки трафика."""
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return

    user_id = query.from_user.id

    if app_conf.get('traffic_renewal_enabled', '0') != '1':
        await query.answer("Продление трафика временно недоступно", show_alert=True)
        return

    user_data = await db_helpers.get_user(user_id)
    if not user_data:
        await query.answer("Пользователь не найден", show_alert=True)
        return

    user_dict = dict(user_data)
    if not user_dict.get('xui_client_uuid'):
        await query.answer("Для покупки трафика нужна активная подписка", show_alert=True)
        return

    access_token = (app_conf.get('wata_access_token') or '').strip() \
        or (os.getenv('WATA_ACCESS_TOKEN') or '').strip()
    terminal_public_id = (app_conf.get('wata_terminal_public_id') or '').strip() \
        or (os.getenv('WATA_TERMINAL_PUBLIC_ID') or '').strip()
    if not access_token:
        await query.answer("Wata не настроена", show_alert=True)
        return

    # Тариф из callback_data: traffic_renewal_tariff_<tariff_id>_wata
    tariff_id = None
    tariff_gb = None
    tariff_price = None
    if query.data.startswith("traffic_renewal_tariff_"):
        try:
            parts = query.data.split("_")
            tariff_id = int(parts[3])
            tariff = await db_helpers.get_traffic_topup_tariff_by_id(tariff_id)
            if not tariff:
                await query.answer("Тариф не найден", show_alert=True)
                return
            tariff_dict = dict(tariff)
            if not tariff_dict.get('is_active'):
                await query.answer("Тариф неактивен", show_alert=True)
                return
            tariff_gb = tariff_dict.get('traffic_gb', 0)
            tariff_price = tariff_dict.get('price', 0)
        except Exception as e:
            logger.error(f"Wata traffic_renewal: парсинг тарифа: {e}")
            await query.answer("Ошибка при получении тарифа", show_alert=True)
            return

    if tariff_id is None:
        default_traffic_limit_gb = get_default_limit_gb()
        if default_traffic_limit_gb <= 0:
            await query.answer("Лимит трафика по умолчанию не установлен", show_alert=True)
            return
        tariff_gb = default_traffic_limit_gb
        tariff_price = float('100')

    price = float(tariff_price)
    currency = 'RUB'

    bot_username = (await bot.get_me()).username
    return_url = f"https://t.me/{bot_username}"

    from src.pay import create_wata_payment_traffic_renewal
    result = await create_wata_payment_traffic_renewal(
        access_token=str(access_token),
        user_id=user_id,
        price=price,
        traffic_to_add_gb=float(tariff_gb),
        return_url=return_url,
        currency=currency,
        tariff_id=tariff_id,
        terminal_public_id=str(terminal_public_id),
    )

    if not result:
        await query.message.edit_text(
            app_conf.get('text_error_general') or REST_TEXT_DEFAULTS['text_error_general'],
            reply_markup=keyboards.get_back_to_main_keyboard(),
        )
        await query.answer()
        return

    payment_id, pay_url = result
    price_str = int(price) if price == int(price) else f"{price:.2f}"

    kbd = InlineKeyboardMarkup(inline_keyboard=[
        [btn('btn_payment_pay_link', url=pay_url)],
        [btn('btn_back_to_main', callback_data='back_to_main')]
    ])
    await query.message.edit_text(
        _traffic_renewal_payment_text(tariff_gb, price_str, currency),
        reply_markup=kbd,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    logger.info(
        f"Wata: создан платеж {payment_id} для продления трафика "
        f"пользователя {user_id} (+{tariff_gb} GB). Ожидаем webhook."
    )
    await safe_answer_callback(query)


@dp.callback_query((F.data == "traffic_renewal_choose_cryptobot") | (F.data.startswith("traffic_renewal_tariff_") & F.data.endswith("_cryptobot")))
async def cq_traffic_renewal_choose_cryptobot(query: CallbackQuery):
    """Создание платежа CryptoBot для продления трафика"""
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
    
    user_id = query.from_user.id
    
    # Проверяем условия
    if app_conf.get('traffic_renewal_enabled', '0') != '1':
        await query.answer("Продление трафика временно недоступно", show_alert=True)
        return
    
    user_data = await db_helpers.get_user(user_id)
    if not user_data:
        await query.answer("Пользователь не найден", show_alert=True)
        return
    
    # Преобразуем Row в словарь
    user_dict = dict(user_data)
    if not user_dict.get('xui_client_uuid'):
        await query.answer("Для покупки трафика нужна активная подписка", show_alert=True)
        return
    
    token = app_conf.get('cryptobot_token')
    if not token:
        await query.answer("CryptoBot не настроен", show_alert=True)
        return
    
    # Проверяем, есть ли ID тарифа в callback_data
    tariff_id = None
    tariff_gb = None
    tariff_price = None
    
    if query.data.startswith("traffic_renewal_tariff_"):
        # Новый формат с тарифом
        try:
            parts = query.data.split("_")
            tariff_id = int(parts[3])
            tariff = await db_helpers.get_traffic_topup_tariff_by_id(tariff_id)
            if not tariff:
                await query.answer("Тариф не найден", show_alert=True)
                return
            tariff_dict = dict(tariff)
            if not tariff_dict.get('is_active'):
                await query.answer("Тариф неактивен", show_alert=True)
                return
            tariff_gb = tariff_dict.get('traffic_gb', 0)
            tariff_price = tariff_dict.get('price', 0)
        except Exception as e:
            logger.error(f"Ошибка при получении тарифа: {e}")
            await query.answer("Ошибка при получении тарифа", show_alert=True)
            return
    
    if tariff_id is None:
        # Старая логика - используем настройки
        default_traffic_limit_gb = get_default_limit_gb()
        
        if default_traffic_limit_gb <= 0:
            await query.answer("Лимит трафика по умолчанию не установлен", show_alert=True)
            return
        
        tariff_gb = default_traffic_limit_gb
        tariff_price = float('100')
    
    price = tariff_price

    # CryptoBot — только RUB-инвойсы.
    from src.pay import cryptobot_fiat_invoice_body
    invoice_amount_part = cryptobot_fiat_invoice_body(price)

    # Формируем payload с учетом тарифа
    if tariff_id:
        payload = f"{user_id}|traffic_renewal_tariff_{tariff_id}|0|0"
    else:
        payload = f"{user_id}|traffic_renewal|0|0"
    
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                'https://pay.crypt.bot/api/createInvoice',
                headers={'Crypto-Pay-API-Token': token},
                json={
                    **invoice_amount_part,
                    'description': f'Продление трафика (+{tariff_gb} GB)',
                    'payload': payload,
                    # CryptoBot допускает только viewItem|openChannel|openBot|callback.
                    'paid_btn_name': 'openBot',
                    'paid_btn_url': app_conf.get('connect_page_url', 'https://t.me/your_bot')
                }
            )
            response.raise_for_status()
            data = response.json()
            
        if data.get('ok') and data.get('result'):
            invoice = data['result']
            invoice_id = invoice.get('invoice_id')
            pay_url = invoice.get('pay_url')
            
            payment_id = f"CRYPTO_TRAFFIC_RENEWAL_{invoice_id}"
            payment_metadata = {
                "payment_type": "traffic_renewal",
                "telegram_user_id": user_id,
                "price": price,
                "traffic_to_add_gb": tariff_gb,
                "payment_method": "CryptoBot",
                "payload": payload,
            }
            if tariff_id:
                payment_metadata["tariff_id"] = tariff_id
            
            await db_helpers.add_payment(
                payment_id, user_id, float(price), 'RUB',
                json.dumps(payment_metadata, ensure_ascii=False)
            )
            
            price_str = int(price) if price == int(price) else f"{price:.2f}"
            await query.message.edit_text(
                _traffic_renewal_payment_text(tariff_gb, price_str, 'RUB'),
                reply_markup=keyboards.get_cryptobot_payment_keyboard(pay_url, invoice_id),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await safe_answer_callback(query)
        else:
            await query.answer("Ошибка создания платежа", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка создания платежа CryptoBot для продления трафика: {e}")
        await query.answer("Ошибка создания платежа. Попробуйте позже.", show_alert=True)


@dp.callback_query((F.data == "traffic_renewal_choose_yoomoney") | (F.data.startswith("traffic_renewal_tariff_") & F.data.endswith("_yoomoney")))
async def cq_traffic_renewal_choose_yoomoney(query: CallbackQuery):
    """Создание платежа YooMoney для продления трафика"""
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
    
    user_id = query.from_user.id
    
    # Проверяем условия
    if app_conf.get('traffic_renewal_enabled', '0') != '1':
        await query.answer("Продление трафика временно недоступно", show_alert=True)
        return
    
    user_data = await db_helpers.get_user(user_id)
    if not user_data:
        await query.answer("Пользователь не найден", show_alert=True)
        return
    
    # Преобразуем Row в словарь
    user_dict = dict(user_data)
    if not user_dict.get('xui_client_uuid'):
        await query.answer("Для покупки трафика нужна активная подписка", show_alert=True)
        return
    
    account = app_conf.get('yoomoney_account')
    if not account:
        await query.answer("YooMoney не настроен", show_alert=True)
        return
    
    # Проверяем, есть ли ID тарифа в callback_data
    tariff_id = None
    tariff_gb = None
    tariff_price = None
    
    if query.data.startswith("traffic_renewal_tariff_"):
        # Новый формат с тарифом
        try:
            parts = query.data.split("_")
            tariff_id = int(parts[3])
            tariff = await db_helpers.get_traffic_topup_tariff_by_id(tariff_id)
            if not tariff:
                await query.answer("Тариф не найден", show_alert=True)
                return
            tariff_dict = dict(tariff)
            if not tariff_dict.get('is_active'):
                await query.answer("Тариф неактивен", show_alert=True)
                return
            tariff_gb = tariff_dict.get('traffic_gb', 0)
            tariff_price = tariff_dict.get('price', 0)
        except Exception as e:
            logger.error(f"Ошибка при получении тарифа: {e}")
            await query.answer("Ошибка при получении тарифа", show_alert=True)
            return
    
    if tariff_id is None:
        # Старая логика - используем настройки
        default_traffic_limit_gb = get_default_limit_gb()
        
        if default_traffic_limit_gb <= 0:
            await query.answer("Лимит трафика по умолчанию не установлен", show_alert=True)
            return
        
        tariff_gb = default_traffic_limit_gb
        tariff_price = float('100')
    
    price = tariff_price
    currency = 'RUB'
    
    payment_id = f"YOOMONEY_TRAFFIC_RENEWAL_{int(time.time())}_{user_id}"
    
    payment_metadata = {
        "payment_type": "traffic_renewal",
        "telegram_user_id": user_id,
        "price": price,
        "traffic_to_add_gb": tariff_gb,
        "payment_method": "YooMoney"
    }
    if tariff_id:
        payment_metadata["tariff_id"] = tariff_id
    
    from src.pay import create_yoomoney_quickpay
    payment_url = await create_yoomoney_quickpay(
        account=account,
        telegram_id=user_id,
        payment_id=payment_id,
        amount=float(price),
        currency=currency,
        target_text=f"Продление трафика (+{tariff_gb} GB)",
        metadata=payment_metadata,
    )
    if not payment_url:
        await query.answer("Ошибка создания платежа. Попробуйте позже.", show_alert=True)
        return

    price_str = int(price) if price == int(price) else f"{price:.2f}"
    await query.message.edit_text(
        _traffic_renewal_payment_text(tariff_gb, price_str, currency),
        reply_markup=keyboards.get_yoomoney_payment_keyboard(payment_url, payment_id),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await safe_answer_callback(query)


@dp.callback_query((F.data == "traffic_renewal_choose_tgstar") | (F.data.startswith("traffic_renewal_tariff_") & F.data.endswith("_tgstar")))
async def cq_traffic_renewal_choose_tgstar(query: CallbackQuery):
    """Создание платежа TG Star для продления трафика"""
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
    
    user_id = query.from_user.id
    
    # Проверяем условия
    if app_conf.get('traffic_renewal_enabled', '0') != '1':
        await query.answer("Продление трафика временно недоступно", show_alert=True)
        return
    
    user_data = await db_helpers.get_user(user_id)
    if not user_data:
        await query.answer("Пользователь не найден", show_alert=True)
        return
    
    # Преобразуем Row в словарь
    user_dict = dict(user_data)
    if not user_dict.get('xui_client_uuid'):
        await query.answer("Для покупки трафика нужна активная подписка", show_alert=True)
        return
    
    # provider_token для XTR (Telegram Stars) НЕ требуется с осени 2024 —
    # Telegram принимает Stars-инвойсы напрямую. Поле в админке оставляем
    # опциональным; если пусто — отправляем пустую строку, send_invoice примет.
    provider_token = app_conf.get('tgstar_provider_token') or ''

    # Проверяем, есть ли ID тарифа в callback_data
    tariff_id = None
    tariff_gb = None
    tariff_price = None
    
    if query.data.startswith("traffic_renewal_tariff_"):
        # Новый формат с тарифом
        try:
            parts = query.data.split("_")
            tariff_id = int(parts[3])
            tariff = await db_helpers.get_traffic_topup_tariff_by_id(tariff_id)
            if not tariff:
                await query.answer("Тариф не найден", show_alert=True)
                return
            tariff_dict = dict(tariff)
            if not tariff_dict.get('is_active'):
                await query.answer("Тариф неактивен", show_alert=True)
                return
            tariff_gb = tariff_dict.get('traffic_gb', 0)
            tariff_price = tariff_dict.get('price', 0)
        except Exception as e:
            logger.error(f"Ошибка при получении тарифа: {e}")
            await query.answer("Ошибка при получении тарифа", show_alert=True)
            return
    
    if tariff_id is None:
        # Старая логика - используем настройки
        default_traffic_limit_gb = get_default_limit_gb()
        
        if default_traffic_limit_gb <= 0:
            await query.answer("Лимит трафика по умолчанию не установлен", show_alert=True)
            return
        
        tariff_gb = default_traffic_limit_gb
        tariff_price = float('100')
    
    price = tariff_price
    # traffic_topup_tariffs цены хранятся в ₽ — конвертируем по курсу.
    from src.pay import rub_to_stars
    price_xtr = rub_to_stars(price, app_conf)
    rub_str = int(float(price)) if float(price) == int(float(price)) else f"{float(price):.2f}"

    # Формируем payload с учетом тарифа
    if tariff_id:
        payload = f"tgstar_traffic_renewal_tariff_{tariff_id}_{user_id}"
    else:
        payload = f"tgstar_traffic_renewal_{user_id}"
    
    try:
        await query.message.delete()
    except Exception:
        pass
    
    await bot.send_invoice(
        chat_id=user_id,
        title=f"Продление трафика (+{tariff_gb} GB)",
        description=(
            f"Продление трафика (+{tariff_gb} GB)\n\n"
            f"{rub_str} ₽ ≈ {price_xtr} ⭐"
        ),
        payload=payload,
        provider_token=provider_token,
        currency='XTR',
        prices=[LabeledPrice(label=f"Продление трафика (+{tariff_gb} GB)", amount=price_xtr)],
        start_parameter="traffic_renewal"
    )
    
    await bot.send_message(
        chat_id=user_id,
        text="⬆️ Счет для оплаты создан.\n\nЕсли вы передумали, нажмите кнопку ниже, чтобы вернуться.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn('btn_back', callback_data='traffic_renewal_choose_payment')]
        ])
    )
    await query.answer()


@dp.callback_query((F.data == "traffic_renewal_choose_manual") | (F.data.startswith("traffic_renewal_tariff_") & F.data.endswith("_manual")))
async def cq_traffic_renewal_choose_manual(query: CallbackQuery):
    """Инструкции для ручной оплаты продления трафика"""
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
    
    user_id = query.from_user.id
    
    # Проверяем условия
    if app_conf.get('traffic_renewal_enabled', '0') != '1':
        await query.answer("Продление трафика временно недоступно", show_alert=True)
        return
    
    user_data = await db_helpers.get_user(user_id)
    if not user_data:
        await query.answer("Пользователь не найден", show_alert=True)
        return
    
    # Преобразуем Row в словарь
    user_dict = dict(user_data)
    if not user_dict.get('xui_client_uuid'):
        await query.answer("Для покупки трафика нужна активная подписка", show_alert=True)
        return
    
    # Получаем количество трафика для добавления
    default_traffic_limit_gb = get_default_limit_gb()
    
    if default_traffic_limit_gb <= 0:
        await query.answer("Лимит трафика по умолчанию не установлен", show_alert=True)
        return
    
    price = float('100')
    price_str = int(price) if price == int(price) else f"{price:.2f}"
    support_link = app_conf.get('support_link', '')
    
    text = txt_manual_traffic_renewal(default_traffic_limit_gb, price_str, user_id)
    
    kb_rows = [
        [InlineKeyboardButton(text="Я оплатил", callback_data="traffic_renewal_manual_i_paid")],
        [btn('btn_back', callback_data='traffic_renewal_choose_payment')]
    ]
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    await query.answer()


@dp.callback_query(F.data == "traffic_renewal_manual_i_paid")
async def cq_traffic_renewal_manual_i_paid(query: CallbackQuery):
    """Подтверждение ручной оплаты продления трафика"""
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
    
    user_id = query.from_user.id
    support_link = app_conf.get('support_link', '')
    
    msg = (
        "<b>Заявка принята</b>\n✓ Проверим оплату\n\n"
        "Трафик добавится в течение 30 минут.\n"
    )
    if support_link:
        msg += f"Если обработка не произошла — обратитесь в поддержку: {support_link}"
    else:
        msg += "Если обработка не произошла — обратитесь в поддержку."
    
    try:
        await query.message.edit_text(msg, reply_markup=keyboards.get_back_to_main_keyboard())
    except Exception:
        await bot.send_message(user_id, msg, reply_markup=keyboards.get_back_to_main_keyboard())
    await query.answer()


@dp.callback_query(F.data.startswith("renew_limit_"))
async def cq_renew_choose_by_limit(query: CallbackQuery):
    """После выбора лимита в renew_increase_limit — список тарифов с этим лимитом."""
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return

    # Парсим callback_data: renew_limit_<method>_<limit>
    # Поддерживается legacy: renew_limit_platega_<method_id>_<limit>.
    method = "yookassa"
    limit_required = 0
    try:
        payload = query.data.replace("renew_limit_", "", 1)
        parts = payload.split("_")
        if len(parts) == 3 and parts[0] == "platega":
            method = "platega"
            limit_required = int(parts[2])
        elif len(parts) >= 2:
            method, lim_str = payload.rsplit("_", 1)
            limit_required = int(lim_str)
    except Exception as e:
        logger.warning(f"renew_limit: parse failed for {query.data}: {e}")

    try:
        tariffs = await db_helpers.get_active_tariffs()
    except Exception as e:
        logger.warning(f"renew_limit: get_active_tariffs failed: {e}")
        tariffs = []

    method_ok = (method, 'both', 'all', None)
    filtered = [
        t for t in tariffs
        if t.get('payment_method') in method_ok
        and int(t.get('limit_ip', 0) or 0) == limit_required
    ]

    if not filtered:
        # Под выбранный лимит ничего нет — откатимся к списку всех лимитов.
        await cq_renew_increase_limit.__wrapped__(query)  # type: ignore[attr-defined]
        return

    try:
        filtered.sort(key=lambda t: (float(t.get('price', 0) or 0), int(t.get('days', 0) or 0)))
    except Exception:
        pass

    rows = []
    for t in filtered:
        try:
            price_val = float(t['price'])
            price_str = int(price_val) if price_val.is_integer() else f"{price_val:g}"
        except Exception:
            price_str = t.get('price', '-')
        days = int(t.get('days', 0) or 0)
        currency = t.get('currency', 'RUB')

        if method == "platega":
            callback_data = f"renew_tariff_platega_{t['id']}"
        else:
            callback_data = f"renew_tariff_{method}_{t['id']}"

        rows.append([InlineKeyboardButton(
            text=f"{days} дней · {price_str} {currency}",
            callback_data=callback_data,
        )])

    rows.append([btn('btn_back', callback_data=f"renew_increase_limit_{method}")])

    limit_label = _format_device_limit_label(limit_required)

    kbd = InlineKeyboardMarkup(inline_keyboard=rows)
    await query.message.edit_text(
        f"<b>Тарифы: {limit_label}</b>\n\nВыберите тариф для оплаты:",
        reply_markup=kbd,
        parse_mode='HTML',
    )
    await query.answer()

@dp.callback_query(F.data.startswith("rd:"))
async def cq_renew_choose_devices(query: CallbackQuery):
    # Упразднено: больше не группируем по лимиту устройств. Перенаправляем на выбор метода.
    method = 'yookassa'
    try:
        _, method, _ = query.data.split(":", 2)
    except Exception:
        pass
    await cq_renew_choose_method.__wrapped__(query)  # type: ignore

@dp.callback_query(F.data.startswith("renew_tariff_tgstar_"))
async def cq_renew_tariff_tgstar(query: CallbackQuery):
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
        
    tariff_id = int(query.data.split("_")[-1])
    tariffs = await db_helpers.get_active_tariffs()
    tariff = next((t for t in tariffs if t['id'] == tariff_id), None)

    if not tariff:
        await query.answer("Тариф не найден", show_alert=True)
        return

    provider_token = app_conf.get('tgstar_provider_token')

    # TG Stars — цена тарифа всегда в ₽, конвертируем по курсу tgstar_rub_per_star.
    from src.pay import rub_to_stars
    rub_value = float(tariff['price'])
    stars_amount = rub_to_stars(rub_value, app_conf)
    prices = [LabeledPrice(label=tariff['name'], amount=stars_amount)]

    description = tariff.get('description') or tariff.get('name') or 'Оплата тарифа'
    rub_str = int(rub_value) if rub_value == int(rub_value) else f"{rub_value:.2f}"
    description = f"{description}\n\n{rub_str} ₽ ≈ {stars_amount} ⭐"

    try:
        await query.message.delete()
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение перед отправкой инвойса: {e}")


    await bot.send_invoice(
        chat_id=query.from_user.id,
        title=tariff['name'],
        description=description,
        payload=f"tgstar_{tariff_id}_{query.from_user.id}",
        provider_token=provider_token,
        currency='XTR',
        prices=prices,
        start_parameter="tgstar"
    )

    await bot.send_message(
        chat_id=query.from_user.id,
        text="<b>Счёт создан</b>\n○ Ожидает оплаты\n\nВернуться к выбору срока можно по кнопке ниже.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="‹ К выбору срока", callback_data="renew_choose_tgstar")]
        ])
    )
    
    await query.answer()
    

@dp.callback_query(F.data.startswith("renew_tariff_yoomoney_"))
async def cq_renew_tariff_yoomoney(query: CallbackQuery):
    """Создает счет в YooMoney и показывает кнопки для оплаты и проверки."""
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
        
    account = app_conf.get('yoomoney_account')
    
    logger.info(f"Используется YooMoney счет: '{account}'" if account else "YooMoney счет НЕ НАЙДЕН")

    if not account:
        logger.error("YooMoney: номер счета не найден в настройках!")
        await query.answer("Оплата через YooMoney временно недоступна.", show_alert=True)
        return

    try:
        tariff_id = int(query.data.split("_")[-1])
        tariff = await db_helpers.get_tariff_by_id(tariff_id)
        if not tariff: raise ValueError("Тариф не найден")
    except (ValueError, IndexError):
        logger.error(f"Неверный формат callback_data для YooMoney: {query.data}")
        await query.answer("Ошибка в параметрах кнопки.", show_alert=True)
        return

    # Создаем уникальный ID для платежа
    payment_id = f"YOOMONEY_{int(time.time())}_{query.from_user.id}_{tariff_id}"
    
    # Формируем данные для платежа
    payment_data = {
        'amount': tariff['price'],
        'currency': tariff['currency'],
        'payment_id': payment_id,
        'description': f"Подписка : {tariff['name']} на {tariff['days']} дней",
        'user_id': query.from_user.id,
        'tariff_id': tariff_id,
        'days': tariff['days'],
        'limit_ip': tariff.get('limit_ip', 0)
    }

    from src.pay import create_yoomoney_quickpay
    payment_url = await create_yoomoney_quickpay(
        account=account,
        telegram_id=query.from_user.id,
        payment_id=payment_id,
        amount=float(tariff['price']),
        currency=tariff['currency'],
        target_text=f"Подписка {tariff['name']}",
        metadata=payment_data,
    )
    if not payment_url:
        await query.answer("Не удалось создать счет. Попробуйте позже.", show_alert=True)
        await query.answer()
        return

    # Формируем переменные для отображения, как в YooKassa и Platega
    days = tariff['days']
    price = float(tariff['price'])
    currency = tariff['currency']
    price_str = int(price) if price == int(price) else f"{price:.2f}"
    limit_ip = int(tariff.get('limit_ip', 0) or 0)
    limit_ip_display = "∞" if limit_ip == 0 else str(limit_ip)
    description_text = ""
    if tariff.get('description') and tariff['description'].strip():
        description_text = f"\n{tariff['description'].strip()}\n"

    await query.message.edit_text(
        txt_payment_renewal(days, limit_ip_display, price_str, currency, description_text),
        reply_markup=keyboards.get_yoomoney_payment_keyboard(payment_url, payment_id),
        parse_mode="HTML",
        disable_web_page_preview=True
    )

    # Для YooMoney НЕ запускаем автопроверку - платежи обрабатываются через webhook
    logger.info("YooMoney: автопроверка не запускается, платеж будет обработан через webhook")

    await query.answer()

@dp.callback_query(F.data.startswith("check_yoomoney_payment_"))
async def cq_check_yoomoney_payment(query: CallbackQuery):
    """Проверяет статус платежа в YooMoney по запросу пользователя."""
    logger.info(f"YooMoney: Начало проверки платежа для пользователя {query.from_user.id}")
    
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
        
    payment_id = query.data.replace("check_yoomoney_payment_", "")
    logger.info(f"YooMoney: Проверяем платеж {payment_id}")
    await query.answer("Проверяем статус платежа", show_alert=False)

    token = app_conf.get('yoomoney_token')
    logger.info(f"YooMoney: Токен получен: {'Да' if token else 'Нет'}")
    if not token:
        logger.error("YooMoney: API-ключ не найден в настройках!")
        await query.answer("Ошибка: сервис оплаты временно недоступен.", show_alert=True)
        return

    try:
        # Получаем информацию о платеже из БД
        logger.info(f"YooMoney: Получаем информацию о платеже {payment_id} из БД")
        db_payment = await db_helpers.get_payment(payment_id)
        if not db_payment:
            logger.error(f"YooMoney: Платеж {payment_id} не найден в БД")
            await query.answer("Платеж не найден.", show_alert=True)
            return

        logger.info(f"YooMoney: Платеж найден в БД, статус: {db_payment[4]}")
        if db_payment[4] == 'succeeded':
            logger.info(f"YooMoney: Платеж {payment_id} уже был зачислен")
            await query.answer("✓ Этот платёж уже зачислен.", show_alert=True)
            return

        # Поскольку API YooMoney не работает, используем упрощенную проверку
        # Пользователь должен подтвердить, что оплата прошла
        logger.info(f"YooMoney: API недоступен, используем упрощенную проверку для платежа {payment_id}")
        
        # Показываем пользователю инструкции
        text = txt_yoomoney_check(payment_id, db_payment)
        
        # Создаем клавиатуру с кнопкой проверки статуса
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(
            text="↻ Проверить статус",
            callback_data=f"confirm_yoomoney_payment_{payment_id}"
        ))
        builder.row(btn('btn_back_to_main', callback_data='back_to_main'))
        
        await query.message.edit_text(text, reply_markup=builder.as_markup())
        await query.answer()

    except Exception as e:
        logger.error(f"Критическая ошибка проверки платежа YooMoney {payment_id}: {e}")
        await query.answer("Ошибка проверки платежа. Попробуйте позже.", show_alert=True)


@dp.callback_query(F.data.startswith("confirm_yoomoney_payment_"))
async def cq_confirm_yoomoney_payment(query: CallbackQuery):
    """Проверяет статус платежа YooMoney (только для платежей, оплаченных через webhook)"""
    logger.info(f"YooMoney: Проверка статуса платежа для пользователя {query.from_user.id}")
    
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
    
    payment_id = query.data.replace("confirm_yoomoney_payment_", "")
    logger.info(f"YooMoney: Проверяем статус платежа {payment_id}")
    
    try:
        # Получаем информацию о платеже из БД
        db_payment = await db_helpers.get_payment(payment_id)
        if not db_payment:
            logger.error(f"YooMoney: Платеж {payment_id} не найден в БД")
            await query.answer("Платеж не найден.", show_alert=True)
            return
        
        if db_payment[4] == 'succeeded':
            logger.info(f"YooMoney: Платеж {payment_id} уже был зачислен")
            await query.answer("✓ Этот платёж уже зачислен.", show_alert=True)
            return
        
        # Проверяем, был ли платеж действительно оплачен через webhook
        if db_payment[4] == 'pending':
            await query.answer("⚠ Платёж ещё не получен. Завершите оплату и подождите несколько минут.", show_alert=True)
            return
        
        # Если платеж был обработан через webhook, показываем успех
        await query.message.edit_text(
            "✓ Оплата через YooMoney прошла. Подписка активна.",
            reply_markup=keyboards.get_success_with_referral_keyboard()
        )
        logger.info(f"YooMoney: Платеж {payment_id} уже обработан через webhook.")
        
    except Exception as e:
        logger.error(f"Ошибка проверки платежа YooMoney {payment_id}: {e}")
        await query.answer("Ошибка проверки платежа. Попробуйте позже.", show_alert=True)


# YooMoney-хелперы перенесены в src.pay.yoomoney
from src.pay import (
    verify_yoomoney_payment_via_api as _verify_yoomoney_via_api,
    verify_yoomoney_signature as _verify_yoomoney_signature,
)


# Webhook обработчик для YooMoney
async def yoomoney_webhook_handler(request):
    """Обрабатывает webhook уведомления от YooMoney"""
    try:
        # Читаем СЫРОЕ тело ДО парсинга, чтобы можно было проверить подпись от raw-body
        # (если такой формат когда-нибудь будет задокументирован). После `request.post()`
        # сырое тело уже недоступно.
        raw_body_bytes = await request.read()
        try:
            raw_body_str = raw_body_bytes.decode("utf-8", errors="replace")
        except Exception:
            raw_body_str = ""

        from urllib.parse import parse_qsl
        from multidict import MultiDict
        form_data = MultiDict(parse_qsl(raw_body_str, keep_blank_values=True))

        payment_id = form_data.get('label')
        operation_id = form_data.get('operation_id')
        amount = form_data.get('amount')
        currency = form_data.get('currency')
        notification_type = form_data.get('notification_type')

        logger.info(
            f"YooMoney webhook: {notification_type} | label={payment_id} | "
            f"op={operation_id} | amount={amount} {currency}"
        )

        signature_valid = _verify_yoomoney_signature(
            form_data,
            secret=app_conf.get('yoomoney_notification_secret', '') or '',
        )

        if not signature_valid:
            # Подпись не сошлась (или это новый SHA-256 формат). Подтверждаем оплату
            # через YooMoney API (`operation-history` под OAuth-токеном) — это
            # самый надёжный источник истины.
            expected_amount_for_api = None
            try:
                if payment_id:
                    db_payment_for_api = await db_helpers.get_payment(payment_id)
                    if db_payment_for_api:
                        expected_amount_for_api = db_payment_for_api[2]
            except Exception as e:
                logger.error(f"YooMoney webhook: не удалось получить сумму из БД: {e}")

            api_ok = None
            if payment_id and expected_amount_for_api is not None:
                api_ok = await _verify_yoomoney_via_api(
                    token=app_conf.get('yoomoney_token'),
                    label=payment_id,
                    operation_id=operation_id,
                    expected_amount=expected_amount_for_api,
                )

            if api_ok is True:
                logger.success(f"YooMoney webhook: платёж {payment_id} подтверждён через API")
                signature_valid = True
            else:
                logger.error(
                    f"YooMoney webhook: 403 (подпись неверна, API-fallback не подтвердил, api_ok={api_ok}). "
                    f"Raw body: {raw_body_str}"
                )
                return web.Response(status=403, text="Invalid signature")

        if operation_id == 'test-notification':
            logger.info("YooMoney webhook: тестовое уведомление, игнорируем")
            return web.Response(status=200, text="OK")

        if not payment_id:
            logger.warning("YooMoney webhook: отсутствует label, игнорируем")
            return web.Response(status=200, text="OK")

        if notification_type in ['p2p-incoming', 'card-incoming']:
            await process_yoomoney_webhook_payment(payment_id, operation_id, amount, currency)
        else:
            logger.info(f"YooMoney webhook: неизвестный тип уведомления: {notification_type}")

        return web.Response(status=200, text="OK")

    except Exception as e:
        logger.error(f"YooMoney webhook: ошибка обработки: {e}", exc_info=True)
        return web.Response(status=500, text="Internal error")


async def process_yoomoney_webhook_payment(payment_id, operation_id, amount, currency):
    """Обрабатывает успешный платеж от YooMoney webhook"""
    try:
        db_payment = await db_helpers.get_payment(payment_id)
        if not db_payment:
            logger.error(f"YooMoney webhook: платеж {payment_id} не найден в БД")
            return

        if db_payment[4] == 'succeeded':
            logger.info(f"YooMoney webhook: платеж {payment_id} уже обработан")
            return

        # Атомарно помечаем платеж как обрабатываемый, чтобы предотвратить race condition
        if not await db_helpers.try_mark_payment_as_processing(payment_id):
            logger.info(f"YooMoney webhook: платеж {payment_id} уже обрабатывается")
            return

        # Проверяем сумму платежа (учитываем комиссию YooMoney ~3%)
        expected_amount = db_payment[2]
        received_amount = float(amount)
        expected_min = expected_amount * 0.97

        if received_amount < expected_min:
            logger.error(
                f"YooMoney webhook: неверная сумма для {payment_id} "
                f"(ожидалось ≥{expected_min}, получено {received_amount}); откатываем в pending"
            )
            await db_helpers.update_payment_status(payment_id, 'pending')
            return

        payment_metadata = json.loads(db_payment[6])
        
        # Проверяем тип платежа
        payment_type = payment_metadata.get('payment_type')
        
        if payment_type == 'traffic_renewal':
            try:
                await db_helpers.update_payment_status(payment_id, 'succeeded')
                await process_successful_payment(
                    int(payment_metadata['telegram_user_id']),
                    payment_id,
                    payment_metadata
                )
            except Exception as e:
                logger.error(
                    f"YooMoney webhook: ошибка обработки {payment_type} {payment_id}: {e}",
                    exc_info=True,
                )
                await db_helpers.update_payment_status(payment_id, 'failed')
                raise
        else:
            user_id = int(payment_metadata.get('telegram_user_id') or payment_metadata.get('user_id'))
            days = int(payment_metadata.get('subscription_days') or payment_metadata.get('days', 30))

            # Получаем traffic_gb из тарифа, если указан tariff_id
            traffic_gb_to_add = 0
            if 'tariff_id' in payment_metadata:
                tariff_id = payment_metadata.get('tariff_id')
                if tariff_id:
                    try:
                        tariff = await db_helpers.get_tariff_by_id(tariff_id)
                        if tariff:
                            traffic_gb_to_add = tariff.get('traffic_gb', 0) or 0
                    except Exception as e:
                        logger.warning(f"YooMoney webhook: не удалось получить traffic_gb из тарифа {tariff_id}: {e}")
            
            try:
                subscription_data = await grant_subscription(
                    user_id, 
                    days, 
                    is_trial=False, 
                    limit_ip=int(payment_metadata.get('limit_ip', 0)),
                    traffic_gb_to_add=traffic_gb_to_add
                )
                if subscription_data:
                    await db_helpers.update_payment_status(payment_id, 'succeeded')

                    # Уведомление пользователю
                    try:
                        expiry_date = subscription_data.get('expiry_date')
                        if expiry_date and isinstance(expiry_date, datetime):
                            expiry_date_str = format_msk_date(expiry_date)
                        else:
                            logger.warning(
                                f"YooMoney webhook: expiry_date отсутствует/некорректна для {payment_id}, "
                                f"используется fallback дата"
                            )
                            expiry_date_str = format_msk_date(datetime.now(timezone.utc) + timedelta(days=days))
                        
                        remnawave_traffic_info = subscription_data.get('remnawave_traffic_info')
                        traffic_info_text = ""
                        if remnawave_traffic_info:
                            added_gb = remnawave_traffic_info.get('added_gb')
                            if added_gb:
                                traffic_info_text = f"\n\nТрафик: добавлено {added_gb} GB"
                        
                        tpl = (app_conf.get('text_payment_success') or '').replace('{sub_link}', '')
                        success_message = tpl.format(days=days, expiry_date=expiry_date_str) + traffic_info_text
                        
                        await bot.send_message(
                            user_id,
                            success_message,
                            reply_markup=keyboards.get_back_to_main_keyboard()
                        )
                    except Exception as e:
                        logger.error(f"YooMoney webhook: ошибка отправки уведомления пользователю {user_id}: {e}", exc_info=True)
                        try:
                            await bot.send_message(
                                user_id,
                                f"✓ Платёж обработан. Подписка продлена на {days} дней.",
                                reply_markup=keyboards.get_back_to_main_keyboard()
                            )
                        except Exception as e2:
                            logger.error(f"YooMoney webhook: не удалось отправить даже базовое сообщение пользователю {user_id}: {e2}")
                else:
                    logger.error(
                        f"YooMoney webhook: grant_subscription вернул None для {payment_id} "
                        f"(оплачен, подписка не выдана) → failed"
                    )
                    await db_helpers.update_payment_status(payment_id, 'failed')
                    await _notify_payment_grant_failed(
                        user_id,
                        registration_type=payment_metadata.get('registration_type'),
                    )
            except Exception as e:
                current = await db_helpers.get_payment(payment_id)
                current_status = current[4] if current and len(current) > 4 else None
                if current_status != 'succeeded':
                    logger.error(f"YooMoney webhook: ошибка обработки платежа {payment_id}: {e}", exc_info=True)
                    await db_helpers.update_payment_status(payment_id, 'failed')
                    await _notify_payment_grant_failed(
                        user_id,
                        registration_type=payment_metadata.get('registration_type'),
                    )
                else:
                    logger.warning(f"YooMoney webhook: ошибка после succeeded для платежа {payment_id} (статус не откатываем): {e}")
                raise
        
        # Партнёрка + реферальный бонус — единый хелпер.
        try:
            payer_uid = int(payment_metadata['user_id'])
            amount_rub = float(payment_metadata.get('amount') or db_payment[2] or 0)
            currency = (payment_metadata.get('currency') or db_payment[3] or 'RUB').upper()
            await _apply_partner_and_referral(
                payer_user_id=payer_uid,
                payment_id=payment_id,
                amount_rub=amount_rub,
                currency=currency,
                log_prefix="YooMoney",
            )
        except Exception as e:
            logger.error(f"Партнёрка/реферал (YooMoney) ошибка: {e}")
        
        logger.success(
            f"YooMoney webhook: платеж {payment_id} обработан для пользователя {payment_metadata['user_id']}"
        )

    except Exception as e:
        logger.error(f"YooMoney webhook: ошибка обработки платежа {payment_id}: {e}", exc_info=True)


async def cryptobot_webhook_handler(request: web.Request):
    """Crypto Pay API: событие ``invoice_paid`` с заголовком ``crypto-pay-api-signature``."""
    raw = await request.read()
    sig = (
        request.headers.get("Crypto-Pay-API-Signature")
        or request.headers.get("crypto-pay-api-signature")
        or ""
    )
    token = (app_conf.get("cryptobot_token") or "").strip()
    if not token:
        logger.warning("CryptoBot webhook: токен не задан")
        return web.Response(status=503, text="CryptoBot token not configured")

    from src.pay.cryptobot import verify_cryptobot_webhook_signature

    if not verify_cryptobot_webhook_signature(token, raw, sig):
        logger.warning("CryptoBot webhook: неверная подпись")
        return web.Response(status=401, text="invalid signature")

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return web.Response(status=400, text="bad json")

    if data.get("update_type") != "invoice_paid":
        return web.Response(text="OK")

    inv = data.get("payload") or data.get("invoice_payload") or {}
    if not isinstance(inv, dict):
        return web.Response(text="OK")

    invoice_id_raw = inv.get("invoice_id")
    if invoice_id_raw is None:
        return web.Response(text="OK")
    try:
        invoice_id = int(invoice_id_raw)
    except (TypeError, ValueError):
        return web.Response(text="OK")

    merchant_payload = inv.get("payload") or ""

    from src.pay.cryptobot import resolve_cryptobot_payment_row

    payment_id, row, payload_eff = await resolve_cryptobot_payment_row(
        invoice_id, str(merchant_payload or "")
    )
    if not payment_id or not row:
        logger.warning(f"CryptoBot webhook: invoice_id={invoice_id} нет в нашей БД")
        return web.Response(text="OK")

    if row[4] == "succeeded":
        return web.Response(text="OK")

    try:
        await handle_cryptobot_invoice_paid(payment_id, payload_eff)
    except Exception as e:
        logger.error(f"CryptoBot webhook: ошибка {payment_id}: {e}", exc_info=True)
        current = await db_helpers.get_payment(payment_id)
        current_status = current[4] if current and len(current) > 4 else None
        if current_status != "succeeded":
            await db_helpers.update_payment_status(payment_id, "failed")
            try:
                tg_uid = int(row[1] or 0)
            except (TypeError, ValueError):
                tg_uid = 0
            await _notify_payment_grant_failed(tg_uid)
        return web.Response(status=500, text="handler error")

    return web.Response(text="OK")


# CORS middleware для API запросов из Mini App
@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == 'OPTIONS':
        response = web.Response()
    else:
        response = await handler(request)
    
    # Добавляем CORS заголовки
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Max-Age'] = '3600'
    
    return response

# Создаем web приложение для webhook
web_app = web.Application(
    middlewares=[cors_middleware],
    client_max_size=64 * 1024 * 1024,  # remnawave.db (node-traffic exporter), до 64 МБ
)
web_app.router.add_post('/yoomoney/', yoomoney_webhook_handler)
web_app.router.add_post('/cryptobot/', cryptobot_webhook_handler)

# Platega callback
async def platega_callback_handler(request: web.Request):
    try:
        merchant_id = request.headers.get('X-MerchantId')
        api_secret = request.headers.get('X-Secret')
        if not merchant_id or not api_secret:
            return web.Response(status=401, text='Unauthorized')

        req_merchant = str(merchant_id)
        req_secret   = str(api_secret)

        bot_merchant = str(app_conf.get('platega_merchant_id', ''))
        bot_secret   = str(app_conf.get('platega_api_secret', ''))

        web_merchant = str(os.getenv('WEBSITE_PLATEGA_MERCHANT_ID', '')).strip()
        web_secret   = str(os.getenv('WEBSITE_PLATEGA_API_SECRET', '')).strip()

        is_bot_valid = bool(bot_merchant and req_merchant == bot_merchant and req_secret == bot_secret)
        is_web_valid = bool(web_merchant and req_merchant == web_merchant and req_secret == web_secret)

        if not (is_bot_valid or is_web_valid):
            return web.Response(status=401, text='Unauthorized')
        data = await request.json()
        tx_id = data.get('id')
        status = (data.get('status') or '').upper()
        # Наш payment_id имеет вид PLATEGA_<uuid> где uuid == tx_id
        payment_id = f"PLATEGA_{tx_id}" if tx_id else None
        if not payment_id:
            return web.Response(status=400, text='Bad Request')
        db_payment = await db_helpers.get_payment(payment_id)
        if not db_payment:
            return web.Response(status=200, text='OK')
        if db_payment[4] == 'succeeded':
            return web.Response(status=200, text='OK')
        # success
        if status == 'CONFIRMED':
            # Атомарно помечаем платеж как обрабатываемый, чтобы предотвратить race condition
            if not await db_helpers.try_mark_payment_as_processing(payment_id):
                logger.info(f"Platega webhook: платеж {payment_id} уже обрабатывается или обработан другим webhook'ом")
                return web.Response(status=200, text='OK')
            
            try:
                meta = json.loads(db_payment[6]) if db_payment[6] else {}
                user_id = int(meta.get('telegram_user_id') or db_payment[1])
                
                # Проверяем тип платежа
                payment_type = meta.get('payment_type')
                
                if payment_type == 'traffic_renewal':
                    # Обработка продления трафика
                    logger.info(f"Platega webhook: обработка продления трафика для платежа {payment_id}")
                    # НЕ обновляем статус здесь - process_successful_payment сам обновит статус после успешной обработки
                    await process_successful_payment(user_id, payment_id, meta)
                else:
                    # Обработка продления подписки (существующая логика)
                    days = int(meta.get('subscription_days') or app_conf.get('subscription_days', 30))
                    limit_ip = int(meta.get('limit_ip') or 0)
                    
                    # Получаем traffic_gb из тарифа, если указан tariff_id
                    traffic_gb_to_add = 0
                    if 'tariff_id' in meta:
                        tariff_id = meta.get('tariff_id')
                        if tariff_id:
                            try:
                                tariff = await db_helpers.get_tariff_by_id(tariff_id)
                                if tariff:
                                    traffic_gb_to_add = tariff.get('traffic_gb', 0) or 0
                                    if traffic_gb_to_add > 0:
                                        logger.info(f"Platega webhook: при продлении подписки будет добавлено {traffic_gb_to_add} GB из тарифа {tariff_id}")
                            except Exception as e:
                                logger.warning(f"Platega webhook: не удалось получить traffic_gb из тарифа {tariff_id}: {e}")
                    
                    subscription_data = await grant_subscription(user_id, days, is_trial=False, limit_ip=limit_ip, traffic_gb_to_add=traffic_gb_to_add)
                    if subscription_data:
                        await db_helpers.update_payment_status(payment_id, 'succeeded')
                        
                        # Останавливаем polling, если он запущен
                        if payment_id in active_payment_checkers:
                            try:
                                task = active_payment_checkers[payment_id]
                                task.cancel()
                                del active_payment_checkers[payment_id]
                                logger.info(f"Platega webhook: остановлен polling для платежа {payment_id}")
                            except Exception as e:
                                logger.debug(f"Platega webhook: ошибка при остановке polling: {e}")
                        
                        # Партнёрка + реферальный бонус — единый хелпер.
                        try:
                            amount_rub = float((db_payment or [None, None, 0])[2] or 0)
                            currency = str((db_payment or [None, None, None, ''])[3] or '').upper()
                            await _apply_partner_and_referral(
                                payer_user_id=user_id,
                                payment_id=payment_id,
                                amount_rub=amount_rub,
                                currency=currency or 'RUB',
                                log_prefix="Platega, webhook",
                            )
                        except Exception as e:
                            logger.error(f"Партнёрка/реферал (Platega, webhook) ошибка: {e}")
                        
                        # Уведомление пользователю
                        try:
                            if subscription_data:
                                expiry_date = subscription_data.get('expiry_date')
                                if expiry_date and isinstance(expiry_date, datetime):
                                    expiry_date_str = format_msk_date(expiry_date)
                                else:
                                    if expiry_date:
                                        logger.warning(f"Platega webhook: expiry_date не является datetime объектом: {type(expiry_date)}")
                                    else:
                                        logger.warning(f"Platega webhook: expiry_date отсутствует в subscription_data для платежа {payment_id}")
                                    expiry_date_str = format_msk_date(datetime.now(timezone.utc) + timedelta(days=days))
                                    logger.warning(f"Platega webhook: используется fallback дата для платежа {payment_id}")
                                
                                remnawave_traffic_info = subscription_data.get('remnawave_traffic_info')
                                traffic_info_text = ""
                                if remnawave_traffic_info:
                                    added_gb = remnawave_traffic_info.get('added_gb')
                                    if added_gb:
                                        traffic_info_text = f"\n\nТрафик: добавлено {added_gb} GB"
                                
                                tpl = (app_conf.get('text_payment_success') or '').replace('{sub_link}', '')
                                success_message = tpl.format(days=days, expiry_date=expiry_date_str) + traffic_info_text
                                
                                await bot.send_message(
                                    user_id,
                                    success_message,
                                    reply_markup=keyboards.get_back_to_main_keyboard()
                                )
                                logger.info(f"Platega webhook: сообщение об успешной оплате отправлено пользователю {user_id}")
                            else:
                                logger.warning(f"Platega webhook: subscription_data отсутствует для платежа {payment_id}, отправляем базовое сообщение")
                                await bot.send_message(
                                    user_id,
                                    f"✓ Платёж обработан. Подписка продлена на {days} дней.",
                                    reply_markup=keyboards.get_back_to_main_keyboard()
                                )
                        except Exception as e:
                            logger.error(f"Platega webhook: ошибка отправки уведомления пользователю {user_id}: {e}", exc_info=True)
                            try:
                                await bot.send_message(
                                    user_id,
                                    f"✓ Платёж обработан. Подписка продлена на {days} дней.",
                                    reply_markup=keyboards.get_back_to_main_keyboard()
                                )
                            except Exception as e2:
                                logger.error(f"Platega webhook: не удалось отправить даже базовое сообщение пользователю {user_id}: {e2}")
                    else:
                        logger.error(
                            f"Platega webhook: grant_subscription вернул None для {payment_id} "
                            f"(оплачен, подписка не выдана) → failed"
                        )
                        await db_helpers.update_payment_status(payment_id, 'failed')
                        await _notify_payment_grant_failed(
                            user_id,
                            registration_type=meta.get('registration_type'),
                        )
            except Exception as e:
                current = await db_helpers.get_payment(payment_id)
                current_status = current[4] if current and len(current) > 4 else None
                if current_status != 'succeeded':
                    logger.error(f"Platega webhook: ошибка обработки платежа {payment_id}: {e}", exc_info=True)
                    await db_helpers.update_payment_status(payment_id, 'failed')
                    await _notify_payment_grant_failed(
                        user_id,
                        registration_type=meta.get('registration_type'),
                    )
                else:
                    logger.warning(f"Platega webhook: ошибка после succeeded для платежа {payment_id} (статус не откатываем): {e}")
                raise
        elif status in ('CANCELED', 'FAILED', 'EXPIRED'):
            await db_helpers.update_payment_status(payment_id, 'canceled')
            # Останавливаем polling для отменённых платежей
            if payment_id in active_payment_checkers:
                try:
                    task = active_payment_checkers[payment_id]
                    task.cancel()
                    del active_payment_checkers[payment_id]
                    logger.info(f"Platega webhook: остановлен polling для отменённого платежа {payment_id}")
                except Exception as e:
                    logger.debug(f"Platega webhook: ошибка при остановке polling: {e}")
        return web.Response(status=200, text='OK')
    except Exception as e:
        logger.error(f"Platega callback error: {e}")
        return web.Response(status=500, text='Internal error')

async def yookassa_webhook_handler(request: web.Request):
    """
    Обработчик webhook уведомлений от YooKassa.
    YooKassa отправляет уведомления в формате JSON с объектом события.
    """
    try:
        data = await request.json()
        logger.info(f"YooKassa webhook: получено уведомление: {json.dumps(data, ensure_ascii=False)}")
        
        # YooKassa отправляет объект события с типом и объектом платежа
        event_type = data.get('event')
        payment_object = data.get('object', {})
        
        if not event_type or not payment_object:
            logger.warning(f"YooKassa webhook: некорректный формат уведомления")
            return web.Response(status=400, text='Bad Request')
        
        payment_id = payment_object.get('id')
        if not payment_id:
            logger.warning(f"YooKassa webhook: нет payment_id в уведомлении")
            return web.Response(status=400, text='Bad Request')
        
        # Проверяем, существует ли платеж в БД
        db_payment = await db_helpers.get_payment(payment_id)
        if not db_payment:
            logger.warning(f"YooKassa webhook: платеж {payment_id} не найден в БД")
            return web.Response(status=200, text='OK')  # Возвращаем 200, чтобы YooKassa не повторял запрос
        
        # Если платеж уже обработан, просто подтверждаем получение
        if db_payment[4] == 'succeeded':
            logger.info(f"YooKassa webhook: платеж {payment_id} уже обработан")
            return web.Response(status=200, text='OK')
        
        # Обрабатываем события
        if event_type == 'payment.succeeded':
            payment_status = payment_object.get('status', '').lower()
            if payment_status == 'succeeded':
                # Атомарно помечаем платеж как обрабатываемый, чтобы предотвратить race condition
                if not await db_helpers.try_mark_payment_as_processing(payment_id):
                    logger.info(f"YooKassa webhook: платеж {payment_id} уже обрабатывается или обработан другим webhook'ом")
                    return web.Response(status=200, text='OK')
                
                try:
                    meta = json.loads(db_payment[6]) if db_payment[6] else {}
                    user_id = int(meta.get('telegram_user_id') or db_payment[1])
                    
                    # Проверяем тип платежа
                    payment_type = meta.get('payment_type')
                    
                    if payment_type == 'traffic_renewal':
                        # Обработка продления трафика
                        logger.info(f"YooKassa webhook: обработка продления трафика для платежа {payment_id}")
                        # НЕ обновляем статус здесь - process_successful_payment сам обновит статус после успешной обработки
                        await process_successful_payment(user_id, payment_id, meta)
                    else:
                        # Обработка продления подписки (существующая логика)
                        days = int(meta.get('subscription_days') or app_conf.get('subscription_days', 30))
                        limit_ip = int(meta.get('limit_ip') or 0)
                        
                        # Получаем traffic_gb из тарифа, если указан tariff_id
                        traffic_gb_to_add = 0
                        if 'tariff_id' in meta:
                            tariff_id = meta.get('tariff_id')
                            if tariff_id:
                                try:
                                    tariff = await db_helpers.get_tariff_by_id(tariff_id)
                                    if tariff:
                                        traffic_gb_to_add = tariff.get('traffic_gb', 0) or 0
                                        if traffic_gb_to_add > 0:
                                            logger.info(f"YooKassa webhook: при продлении подписки будет добавлено {traffic_gb_to_add} GB из тарифа {tariff_id}")
                                except Exception as e:
                                    logger.warning(f"YooKassa webhook: не удалось получить traffic_gb из тарифа {tariff_id}: {e}")
                        
                        subscription_data = await grant_subscription(user_id, days, is_trial=False, limit_ip=limit_ip, traffic_gb_to_add=traffic_gb_to_add)
                        if subscription_data:
                            await db_helpers.update_payment_status(payment_id, 'succeeded')
                            
                            # Останавливаем polling, если он запущен (на случай, если webhook пришел раньше)
                            if payment_id in active_payment_checkers:
                                try:
                                    task = active_payment_checkers[payment_id]
                                    task.cancel()
                                    del active_payment_checkers[payment_id]
                                    logger.info(f"YooKassa webhook: остановлен polling для платежа {payment_id}")
                                except Exception as e:
                                    logger.debug(f"YooKassa webhook: ошибка при остановке polling: {e}")
                            
                            # Партнёрка + реферальный бонус — единый хелпер.
                            try:
                                amount_rub = float((db_payment or [None, None, 0])[2] or 0)
                                currency = str((db_payment or [None, None, None, ''])[3] or '').upper()
                                await _apply_partner_and_referral(
                                    payer_user_id=user_id,
                                    payment_id=payment_id,
                                    amount_rub=amount_rub,
                                    currency=currency or 'RUB',
                                    log_prefix="YooKassa, webhook",
                                )
                            except Exception as e:
                                logger.error(f"Партнёрка/реферал (YooKassa, webhook) ошибка: {e}")
                            
                            # Уведомление пользователю
                            try:
                                if subscription_data:
                                    expiry_date = subscription_data.get('expiry_date')
                                    # Формируем сообщение об успехе
                                    if expiry_date and isinstance(expiry_date, datetime):
                                        expiry_date_str = format_msk_date(expiry_date)
                                    else:
                                        if expiry_date:
                                            logger.warning(f"YooKassa webhook: expiry_date не является datetime объектом: {type(expiry_date)}")
                                        else:
                                            logger.warning(f"YooKassa webhook: expiry_date отсутствует в subscription_data для платежа {payment_id}")
                                        expiry_date_str = format_msk_date(datetime.now(timezone.utc) + timedelta(days=days))
                                        logger.warning(f"YooKassa webhook: используется fallback дата для платежа {payment_id}")
                                    
                                    # Добавляем информацию о трафике Remnawave, если есть
                                    remnawave_traffic_info = subscription_data.get('remnawave_traffic_info')
                                    traffic_info_text = ""
                                    if remnawave_traffic_info:
                                        added_gb = remnawave_traffic_info.get('added_gb')
                                        if added_gb:
                                            traffic_info_text = f"\n\nТрафик: добавлено {added_gb} GB"
                                    
                                    tpl = (app_conf.get('text_payment_success') or '').replace('{sub_link}', '')
                                    success_message = tpl.format(days=days, expiry_date=expiry_date_str) + traffic_info_text
                                    
                                    try:
                                        await bot.send_message(
                                            user_id,
                                            success_message,
                                            reply_markup=keyboards.get_back_to_main_keyboard()
                                        )
                                    except Exception:
                                        pass
                                    logger.info(f"YooKassa webhook: уведомление отправлено пользователю {user_id}")
                                else:
                                    logger.warning(f"YooKassa webhook: subscription_data отсутствует для платежа {payment_id}, отправляем базовое сообщение")
                                    if meta.get('registration_type') != 'site':
                                        await bot.send_message(
                                            user_id,
                                            f"✓ Платёж обработан. Подписка продлена на {days} дней.",
                                            reply_markup=keyboards.get_back_to_main_keyboard()
                                        )
                            except Exception as e:
                                logger.error(f"YooKassa webhook: ошибка отправки уведомления пользователю {user_id}: {e}", exc_info=True)
                                try:
                                    if meta.get('registration_type') != 'site':
                                        await bot.send_message(
                                            user_id,
                                            f"✓ Платёж обработан. Подписка продлена на {days} дней.",
                                            reply_markup=keyboards.get_back_to_main_keyboard()
                                        )
                                except Exception as e2:
                                    logger.error(f"YooKassa webhook: не удалось отправить базовое сообщение: {e2}")
                        else:
                            logger.error(
                                f"YooKassa webhook: grant_subscription вернул None для {payment_id} "
                                f"(оплачен, подписка не выдана) → failed"
                            )
                            await db_helpers.update_payment_status(payment_id, 'failed')
                            await _notify_payment_grant_failed(
                                user_id,
                                registration_type=meta.get('registration_type'),
                            )
                except Exception as e:
                    current = await db_helpers.get_payment(payment_id)
                    current_status = current[4] if current and len(current) > 4 else None
                    if current_status != 'succeeded':
                        logger.error(
                            f"YooKassa webhook: ошибка обработки платежа {payment_id}: {e}",
                            exc_info=True,
                        )
                        await db_helpers.update_payment_status(payment_id, 'failed')
                        await _notify_payment_grant_failed(
                            user_id,
                            registration_type=meta.get('registration_type'),
                        )
                    else:
                        logger.warning(
                            f"YooKassa webhook: ошибка после succeeded для {payment_id} "
                            f"(статус не меняем): {e}"
                        )
        
        elif event_type == 'payment.canceled':
            payment_status = payment_object.get('status', '').lower()
            if payment_status in ('canceled', 'failed'):
                await db_helpers.update_payment_status(payment_id, 'canceled')
                # Останавливаем polling для отменённых платежей
                if payment_id in active_payment_checkers:
                    try:
                        task = active_payment_checkers[payment_id]
                        task.cancel()
                        del active_payment_checkers[payment_id]
                        logger.info(f"YooKassa webhook: остановлен polling для отменённого платежа {payment_id}")
                    except Exception as e:
                        logger.debug(f"YooKassa webhook: ошибка при остановке polling: {e}")
        
        return web.Response(status=200, text='OK')
    except Exception as e:
        logger.error(f"YooKassa webhook error: {e}", exc_info=True)
        return web.Response(status=500, text='Internal error')

# Путь к агрегированной БД трафика Remnawave (рядом с router_bot.db, заливается вебхуком)


async def _handle_remnawave_node_usage_upload(request: web.Request, body: bytes):
    """Приём агрегированного SQLite ``remnawave.db`` от скрипта на сервере Remnawave.

    Поток: скрипт (в docker-сети Remnawave) читает Postgres, агрегирует трафик по
    ``user_id`` (сумма по всем нодам: за сутки и за всё время), упаковывает в SQLite,
    gzip + HMAC-подпись тем же ``remnawave_webhook_secret`` и POST-ит сюда.

    Здесь: проверяем подпись → (опц.) распаковываем gzip → проверяем магию SQLite →
    атомарно сохраняем в ``remnawave.db`` (рядом с ``router_bot.db``). Читатели открывают файл read-only.
    """
    import hmac as _hmac
    import gzip as _gzip
    import tempfile as _tempfile

    await app_conf.load_settings()
    if app_conf.get('remnawave_webhook_enabled', '0') != '1':
        logger.debug("Remnawave node-usage: вебхуки отключены в настройках")
        return web.Response(status=200, text='OK')

    secret = app_conf.get('remnawave_webhook_secret', '')
    if not secret:
        logger.warning("Remnawave node-usage: секрет не настроен")
        return web.Response(status=500, text='Webhook secret not configured')

    # Подпись считается по сырому телу запроса (как пришло, т.е. по gzip-байтам)
    received_sig = (request.headers.get('X-Node-Usage-Signature', '') or '').strip().lower()
    expected_sig = _hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest().lower()
    if not received_sig or not _hmac.compare_digest(received_sig, expected_sig):
        logger.warning("Remnawave node-usage: неверная подпись")
        return web.Response(status=401, text='Invalid signature')

    # Распаковка gzip (по заголовку или по магии 1f 8b)
    data = body
    is_gzip = request.headers.get('X-Node-Usage-Gzip') == '1' or (len(body) >= 2 and body[:2] == b'\x1f\x8b')
    if is_gzip:
        try:
            data = _gzip.decompress(body)
        except Exception as e:
            logger.error(f"Remnawave node-usage: ошибка распаковки gzip: {e}")
            return web.Response(status=400, text='Bad gzip')

    # Проверка, что это действительно файл SQLite
    if not data.startswith(b'SQLite format 3\x00'):
        logger.warning("Remnawave node-usage: тело не является SQLite-файлом")
        return web.Response(status=400, text='Bad payload')

    # Атомарная запись в remnawave.db (рядом с router_bot.db)
    target = migrate_remnawave_db_if_needed()
    try:
        fd, tmp_path = _tempfile.mkstemp(dir=os.path.dirname(target), suffix='.tmp')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        logger.error(f"Remnawave node-usage: не удалось сохранить файл: {e}", exc_info=True)
        return web.Response(status=500, text='Write failed')

    logger.info(f"Remnawave node-usage: сохранён remnawave.db ({len(data)} байт) → {target}")
    db_helpers.schedule_sync_remnawave_traffic_to_users()
    return web.Response(status=200, text='OK')


# Обработчик вебхуков от Remnawave
async def remnawave_webhook_handler(request: web.Request):
    """Обработчик вебхуков от Remnawave.

    Две ветки на одном пути:
      • X-Node-Usage: 1 → приём агрегированного SQLite-файла remnawave.db
        (трафик по user_id, см. _handle_remnawave_node_usage_upload).
      • иначе → официальный вебхук Remnawave (``user.limited``, ``user.first_connected``).
    """
    try:
        body = await request.read()

        # Ветка приёма remnawave.db (бинарный SQLite/gzip) — ДО decode,
        # потому что бинарь не декодируется в utf-8.
        if request.headers.get('X-Node-Usage') == '1':
            return await _handle_remnawave_node_usage_upload(request, body)

        body_str = body.decode('utf-8')
        
        # Ранний выход: извлекаем event из JSON без полной валидации
        _HANDLED_EVENTS = frozenset({"user.limited", "user.first_connected"})
        try:
            raw = json.loads(body_str)
            event = raw.get('event', '')
            if event not in _HANDLED_EVENTS:
                logger.debug(f"Remnawave webhook: событие {event} — игнорируем")
                return web.Response(status=200, text='OK')
        except (json.JSONDecodeError, TypeError):
            pass  # Невалидный JSON — пойдём в полную обработку, там будет 401
        
        # Только для обрабатываемых событий: загружаем настройки и валидируем
        await app_conf.load_settings()
        webhook_enabled = app_conf.get('remnawave_webhook_enabled', '0') == '1'
        if not webhook_enabled:
            logger.debug("Remnawave webhook: вебхуки отключены в настройках")
            return web.Response(status=200, text='OK')
        
        webhook_secret = app_conf.get('remnawave_webhook_secret', '')
        if not webhook_secret:
            logger.warning("Remnawave webhook: секретный ключ не настроен")
            return web.Response(status=500, text='Webhook secret not configured')
        
        headers = dict(request.headers)
        from remnawave.controllers.webhooks import WebhookUtility
        from remnawave.models.webhook import UserDto
        
        webhook_utility = WebhookUtility()
        payload = webhook_utility.parse_webhook(
            body=body_str,
            headers=headers,
            webhook_secret=webhook_secret,
            validate=True
        )
        
        if not payload:
            logger.warning("Remnawave webhook: невалидная подпись вебхука")
            return web.Response(status=401, text='Invalid signature')
        
        event = payload.event
        user_data: UserDto = payload.data
        telegram_id = user_data.telegram_id
        if not telegram_id:
            logger.warning(f"Remnawave webhook: у пользователя {user_data.uuid} нет telegram_id")
            return web.Response(status=200, text='OK')
        
        if event == "user.first_connected":
            logger.info(f"Remnawave webhook: обработка user.first_connected для {telegram_id}")
            await _apply_referral_join_bonus(
                telegram_id,
                log_prefix="Remnawave, user.first_connected",
            )
            return web.Response(status=200, text='OK')
        
        logger.info(f"Remnawave webhook: обработка user.limited для {telegram_id}")
        await handle_traffic_exhausted(telegram_id, user_data)
        
        return web.Response(status=200, text='OK')
        
    except Exception as e:
        logger.error(f"Remnawave webhook error: {e}", exc_info=True)
        return web.Response(status=500, text='Internal error')


async def handle_traffic_exhausted(telegram_id: int, user_data):
    """Обработка события окончания трафика"""
    try:
        
        user_uuid = str(user_data.uuid)
        
        logger.info(f"Remnawave webhook: обработка окончания трафика для пользователя {telegram_id} (UUID: {user_uuid})")
        
        exhausted_squad_uuid = app_conf.get('remnawave_traffic_exhausted_squad_uuid', '').strip()
        
        # Если squad UUID не указан, только отправляем уведомление без изменения трафика и squad
        if not exhausted_squad_uuid:
            logger.info(f"Remnawave webhook: squad UUID не указан, отправляем только уведомление пользователю {telegram_id} без изменения трафика")
        else:
            # Если squad UUID указан, выполняем полную обработку: сброс трафика, безлимит, смена squad
            logger.info(f"Remnawave webhook: squad UUID указан ({exhausted_squad_uuid}), выполняем полную обработку для пользователя {telegram_id}")
            
            # Сбрасываем трафик на 0
            reset_result = await remnawave_manager_instance.reset_user_traffic(user_uuid, apply_default_squad=False)
            if not reset_result:
                logger.error(f"Remnawave webhook: не удалось сбросить трафик для пользователя {telegram_id}")
            
            # Получаем обновленного пользователя после сброса трафика
            try:
                import re
                from uuid import UUID
                from remnawave.models import UpdateUserRequestDto
                
                # Получаем текущего пользователя после сброса трафика
                current_user = await remnawave_manager_instance._sdk.users.get_user_by_uuid(user_uuid)
                if not current_user:
                    logger.error(f"Remnawave webhook: не удалось получить пользователя {user_uuid} после сброса трафика")
                    return
                
                # Подготавливаем данные для обновления
                update_data = {
                    "uuid": current_user.uuid,
                    "expire_at": current_user.expire_at,
                    "traffic_limit_strategy": current_user.traffic_limit_strategy,
                    "description": current_user.description,
                    "email": current_user.email,
                    "telegram_id": current_user.telegram_id,
                    "hwid_device_limit": current_user.hwid_device_limit,
                }
                
                # Устанавливаем трафик в безлимит (0 = безлимит)
                update_data["traffic_limit_bytes"] = 0
                
                # Убираем статус LIMITED, если он есть
                if current_user.status in ["EXPIRED", "LIMITED"]:
                    update_data["status"] = None
                else:
                    update_data["status"] = current_user.status
                
                # Обрабатываем tag
                if current_user.tag and re.match(r"^[A-Z0-9_]+$", current_user.tag):
                    update_data["tag"] = current_user.tag
                else:
                    update_data["tag"] = None
                
                # Применяем squad для окончания трафика
                squad_uuids_list = []
                try:
                    squad_uuids_str = [s.strip() for s in exhausted_squad_uuid.split(',')]
                    for squad_uuid_str in squad_uuids_str:
                        if squad_uuid_str:
                            squad_uuid_obj = UUID(squad_uuid_str)
                            squad_uuids_list.append(squad_uuid_obj)
                    
                    if squad_uuids_list:
                        update_data["active_internal_squads"] = squad_uuids_list
                        logger.info(f"Remnawave webhook: будет применен squad {exhausted_squad_uuid} для пользователя {telegram_id}")
                except ValueError as e:
                    logger.warning(f"Remnawave webhook: неверный формат UUID для exhausted squad: {exhausted_squad_uuid}, ошибка: {e}")
                
                # Обновляем пользователя
                update_request = UpdateUserRequestDto(**update_data)
                updated_user = await remnawave_manager_instance._sdk.users.update_user(update_request)
                logger.info(f"Remnawave webhook: обновлен пользователь {telegram_id} - трафик установлен в безлимит, статус сброшен, squad изменен")
                
            except Exception as e:
                logger.error(f"Remnawave webhook: ошибка обновления пользователя при окончании трафика: {e}", exc_info=True)
        
        # Форматируем информацию о трафике
        # Получаем использованный трафик из user_traffic (правильный способ для новой структуры Remnawave)
        used_traffic_bytes = 0
        try:
            # Пробуем получить через свойство used_traffic_bytes (backward compatibility property)
            if hasattr(user_data, 'used_traffic_bytes'):
                used_bytes_value = user_data.used_traffic_bytes
                used_traffic_bytes = float(used_bytes_value) if used_bytes_value else 0
            # Или через user_traffic объект напрямую
            elif hasattr(user_data, 'user_traffic'):
                user_traffic = getattr(user_data, 'user_traffic', None)
                if user_traffic:
                    used_bytes_value = getattr(user_traffic, 'used_traffic_bytes', 0)
                    used_traffic_bytes = float(used_bytes_value) if used_bytes_value else 0
            # Или напрямую (старый способ)
            else:
                used_bytes_value = getattr(user_data, 'traffic_used_bytes', 0)
                used_traffic_bytes = float(used_bytes_value) if used_bytes_value else 0
        except Exception as e:
            logger.warning(f"Remnawave webhook: ошибка получения used_traffic_bytes: {e}, используем 0")
            used_traffic_bytes = 0
        
        used_gb = used_traffic_bytes / (1024 ** 3) if used_traffic_bytes > 0 else 0
        limit_gb = user_data.traffic_limit_bytes / (1024 ** 3) if user_data.traffic_limit_bytes > 0 else 0
        
        message_template = app_conf.get(
            'text_remnawave_traffic_exhausted',
            REST_TEXT_DEFAULTS['text_remnawave_traffic_exhausted'],
        )
        
        # Формируем сообщение с подстановкой переменных
        try:
            message = message_template.format(used_gb=used_gb, limit_gb=limit_gb)
        except (KeyError, ValueError) as e:
            logger.warning(f"Remnawave webhook: ошибка форматирования сообщения: {e}, используем шаблон по умолчанию")
            message = REST_TEXT_DEFAULTS['text_remnawave_traffic_exhausted'].format(
                used_gb=used_gb, limit_gb=limit_gb
            )
        
        # Создаем клавиатуру
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [btn('btn_back_to_main', callback_data='back_to_main')]
        ])
        
        # Отправляем сообщение пользователю
        await bot.send_message(
            telegram_id,
            message,
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info(f"Remnawave webhook: отправлено уведомление об окончании трафика пользователю {telegram_id}")
        
    except Exception as e:
        logger.error(f"Remnawave webhook: ошибка обработки окончания трафика для пользователя {telegram_id}: {e}", exc_info=True)


async def wata_webhook_handler(request: web.Request):
    """Wata H2H: webhook оплаты/отказа. Сырое тело JSON + заголовок X-Signature (RSA-SHA512)."""
    try:
        raw = await request.read()
    except Exception as e:
        logger.error(f"WATA webhook: не удалось прочитать тело: {e}")
        return web.Response(status=400, text="bad request")

    sig = (
        request.headers.get("X-Signature")
        or request.headers.get("x-signature")
        or ""
    ).strip()

    skip_verify = os.getenv("WATA_WEBHOOK_SKIP_VERIFY", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    try:
        from src.pay.wata import (
            decode_wata_webhook_json,
            fetch_wata_public_key_pem,
            verify_wata_webhook_signature,
        )

        if skip_verify:
            logger.warning(
                "WATA webhook: WATA_WEBHOOK_SKIP_VERIFY — подпись не проверяется"
            )
        else:
            pem = await fetch_wata_public_key_pem()
            if not pem or not verify_wata_webhook_signature(raw, sig, pem):
                logger.warning(
                    "WATA webhook: неверная подпись или не удалось получить public-key"
                )
                return web.Response(status=401, text="invalid signature")

        data, dec_err = decode_wata_webhook_json(raw)
        if not data:
            logger.warning(f"WATA webhook: некорректный JSON: {dec_err}")
            return web.Response(status=400, text="bad json")

        kind = (data.get("kind") or "").strip()
        tx_status = (data.get("transactionStatus") or "").strip()
        order_id = (data.get("orderId") or "").strip()

        logger.info(
            "WATA webhook: "
            f"kind={kind!r} status={tx_status!r} orderId={order_id!r} "
            f"transactionId={data.get('transactionId')!r}"
        )

        if kind == "Refund":
            return web.Response(status=200, text="OK")

        if kind != "Payment":
            return web.Response(status=200, text="OK")

        if tx_status in ("Created", "Pending"):
            return web.Response(status=200, text="OK")

        if not order_id:
            logger.warning("WATA webhook: пустой orderId — пропуск")
            return web.Response(status=200, text="OK")

        payment_id = order_id
        db_payment = await db_helpers.get_payment(payment_id)
        if not db_payment:
            return web.Response(status=200, text="OK")

        if db_payment[4] == "succeeded":
            return web.Response(status=200, text="OK")

        if tx_status == "Declined":
            await db_helpers.update_payment_status(payment_id, "canceled")
            if payment_id in active_payment_checkers:
                try:
                    task = active_payment_checkers[payment_id]
                    task.cancel()
                    del active_payment_checkers[payment_id]
                except Exception:
                    pass
            return web.Response(status=200, text="OK")

        if tx_status != "Paid":
            return web.Response(status=200, text="OK")

        if not await db_helpers.try_mark_payment_as_processing(payment_id):
            logger.info(
                f"WATA webhook: платеж {payment_id} уже обрабатывается или обработан"
            )
            return web.Response(status=200, text="OK")

        try:
            meta = json.loads(db_payment[6]) if db_payment[6] else {}
            user_id = int(meta.get("telegram_user_id") or db_payment[1])

            payment_type = meta.get("payment_type")

            if payment_type == "traffic_renewal":
                logger.info(
                    f"WATA webhook: продление трафика payment_id={payment_id}"
                )
                await process_successful_payment(user_id, payment_id, meta)
            else:
                days = int(
                    meta.get("subscription_days")
                    or app_conf.get("subscription_days", 30)
                )
                limit_ip = int(meta.get("limit_ip") or 0)

                traffic_gb_to_add = 0
                if "tariff_id" in meta:
                    tariff_id = meta.get("tariff_id")
                    if tariff_id:
                        try:
                            tariff = await db_helpers.get_tariff_by_id(tariff_id)
                            if tariff:
                                traffic_gb_to_add = (
                                    tariff.get("traffic_gb", 0) or 0
                                )
                                if traffic_gb_to_add > 0:
                                    logger.info(
                                        f"WATA webhook: тариф {tariff_id} "
                                        f"+{traffic_gb_to_add} GB"
                                    )
                        except Exception as e:
                            logger.warning(
                                f"WATA webhook: tariff {tariff_id}: {e}"
                            )

                subscription_data = await grant_subscription(
                    user_id,
                    days,
                    is_trial=False,
                    limit_ip=limit_ip,
                    traffic_gb_to_add=traffic_gb_to_add,
                )
                if subscription_data:
                    await db_helpers.update_payment_status(
                        payment_id, "succeeded"
                    )

                    if payment_id in active_payment_checkers:
                        try:
                            task = active_payment_checkers[payment_id]
                            task.cancel()
                            del active_payment_checkers[payment_id]
                            logger.info(
                                f"WATA webhook: остановлен polling для {payment_id}"
                            )
                        except Exception as e:
                            logger.debug(
                                f"WATA webhook: остановка polling: {e}"
                            )

                    try:
                        amount_rub = float((db_payment or [None, None, 0])[2] or 0)
                        currency = str(
                            (db_payment or [None, None, None, ""])[3] or ""
                        ).upper()
                        await _apply_partner_and_referral(
                            payer_user_id=user_id,
                            payment_id=payment_id,
                            amount_rub=amount_rub,
                            currency=currency or "RUB",
                            log_prefix="Wata, webhook",
                        )
                    except Exception as e:
                        logger.error(
                            f"Партнёрка/реферал (Wata, webhook) ошибка: {e}"
                        )

                    try:
                        if subscription_data:
                            expiry_date = subscription_data.get("expiry_date")
                            if expiry_date and isinstance(
                                expiry_date, datetime
                            ):
                                expiry_date_str = format_msk_date(expiry_date)
                            else:
                                if expiry_date:
                                    logger.warning(
                                        "WATA webhook: expiry_date не datetime"
                                    )
                                else:
                                    logger.warning(
                                        "WATA webhook: нет expiry_date в subscription_data"
                                    )
                                expiry_date_str = format_msk_date(
                                    datetime.now(timezone.utc)
                                    + timedelta(days=days)
                                )

                            remnawave_traffic_info = subscription_data.get(
                                "remnawave_traffic_info"
                            )
                            traffic_info_text = ""
                            if remnawave_traffic_info:
                                added_gb = remnawave_traffic_info.get(
                                    "added_gb"
                                )
                                if added_gb:
                                    traffic_info_text = (
                                        f"\n\nТрафик: добавлено {added_gb} GB"
                                    )

                            tpl = (
                                app_conf.get("text_payment_success") or ""
                            ).replace("{sub_link}", "")
                            success_message = (
                                tpl.format(
                                    days=days, expiry_date=expiry_date_str
                                )
                                + traffic_info_text
                            )

                            await bot.send_message(
                                user_id,
                                success_message,
                                reply_markup=keyboards.get_back_to_main_keyboard(),
                            )
                            logger.info(
                                f"WATA webhook: уведомление отправлено user={user_id}"
                            )
                        else:
                            await bot.send_message(
                                user_id,
                                f"✓ Платёж обработан. Подписка продлена на {days} дней.",
                                reply_markup=keyboards.get_back_to_main_keyboard(),
                            )
                    except Exception as e:
                        logger.error(
                            f"WATA webhook: ошибка уведомления user={user_id}: {e}",
                            exc_info=True,
                        )
                        try:
                            await bot.send_message(
                                user_id,
                                f"✓ Платёж обработан. Подписка продлена на {days} дней.",
                                reply_markup=keyboards.get_back_to_main_keyboard(),
                            )
                        except Exception as e2:
                            logger.error(
                                f"WATA webhook: fallback сообщение не отправлено: {e2}"
                            )
                else:
                    logger.error(
                        f"WATA webhook: grant_subscription вернул None для {payment_id} "
                        f"(оплачен, подписка не выдана) → failed"
                    )
                    await db_helpers.update_payment_status(
                        payment_id, "failed"
                    )
                    await _notify_payment_grant_failed(
                        user_id,
                        registration_type=meta.get('registration_type'),
                    )
        except Exception as e:
            current = await db_helpers.get_payment(payment_id)
            current_status = (
                current[4] if current and len(current) > 4 else None
            )
            if current_status != "succeeded":
                logger.error(
                    f"WATA webhook: ошибка обработки {payment_id}: {e}",
                    exc_info=True,
                )
                await db_helpers.update_payment_status(
                    payment_id, "failed"
                )
                await _notify_payment_grant_failed(
                    user_id,
                    registration_type=meta.get('registration_type'),
                )
            else:
                logger.warning(
                    f"WATA webhook: ошибка после succeeded {payment_id}: {e}"
                )
            raise

        return web.Response(status=200, text="OK")
    except Exception as e:
        logger.error(f"WATA webhook: {e}", exc_info=True)
        return web.Response(status=500, text="Internal error")


web_app.router.add_post('/platega/callback', platega_callback_handler)
web_app.router.add_post('/wata/webhook', wata_webhook_handler)
web_app.router.add_get('/yoomoney/test', lambda r: web.Response(text="YooMoney webhook server is running!"))
web_app.router.add_post('/yookassa/webhook', yookassa_webhook_handler)
web_app.router.add_post('/remnawave/webhook', remnawave_webhook_handler)


# API endpoint для перезагрузки настроек (только локально)
async def api_reload_settings(request: web.Request):
    """Перезагрузка настроек бота через API (только для localhost)"""
    try:
        # Проверяем, что запрос приходит только с localhost
        # Учитываем как прямой доступ, так и доступ через nginx proxy
        remote_host = request.remote
        x_real_ip = request.headers.get('X-Real-IP', '')
        x_forwarded_for = request.headers.get('X-Forwarded-For', '')
        
        # Разрешенные IP адреса для localhost
        allowed_hosts = ('127.0.0.1', '::1', 'localhost')
        
        # Проверяем remote_host (прямой доступ)
        # Проверяем X-Real-IP (если запрос через nginx)
        # Проверяем первый IP в X-Forwarded-For (если запрос через цепочку прокси)
        is_localhost = (
            remote_host in allowed_hosts or
            x_real_ip in allowed_hosts or
            (x_forwarded_for and x_forwarded_for.split(',')[0].strip() in allowed_hosts)
        )
        
        if not is_localhost:
            logger.warning(f"API reload-settings: отклонен запрос - remote={remote_host}, X-Real-IP={x_real_ip}, X-Forwarded-For={x_forwarded_for} (разрешен только localhost)")
            return web.json_response({'success': False, 'error': 'Access denied. Only localhost allowed.'}, status=403)
        
        logger.info(f"API reload-settings: получен запрос на перезагрузку настроек от localhost (remote={remote_host}, X-Real-IP={x_real_ip})")
        await app_conf.load_settings()
        await apply_bot_session_from_settings(dp)
        logger.success("API reload-settings: настройки успешно перезагружены")
        return web.json_response({'success': True, 'message': 'Settings reloaded successfully'})
    except Exception as e:
        logger.error(f"API reload-settings: ошибка при перезагрузке настроек: {e}")
        return web.json_response({'success': False, 'error': str(e)}, status=500)

web_app.router.add_post('/api/reload-settings', api_reload_settings)


# API endpoint для выдачи пробного периода от website (только локально)
async def api_grant_trial(request: web.Request):
    """Выдаёт пробный период пользователю. Вызывается с website по loopback."""
    try:
        remote_host = request.remote
        x_real_ip = request.headers.get('X-Real-IP', '')
        x_forwarded_for = request.headers.get('X-Forwarded-For', '')
        allowed_hosts = ('127.0.0.1', '::1', 'localhost')
        is_localhost = (
            remote_host in allowed_hosts or
            x_real_ip in allowed_hosts or
            (x_forwarded_for and x_forwarded_for.split(',')[0].strip() in allowed_hosts)
        )
        if not is_localhost:
            logger.warning(f"API grant-trial: отклонен запрос от {remote_host}")
            return web.json_response({'ok': False, 'error': 'Access denied'}, status=403)

        data = await request.json()
        user_id = data.get('user_id')
        if not user_id or not isinstance(user_id, int):
            return web.json_response({'ok': False, 'error': 'Неверный user_id'}, status=400)

        # Читаем настройки триала из app_conf (те же что использует бот)
        trial_days = int(app_conf.get('trial_days', 3) or 3)
        trial_limit_ip = int(app_conf.get('trial_limit_ip', 1) or 1)

        if trial_days <= 0:
            return web.json_response({'ok': False, 'error': 'Триал отключён'}, status=400)

        # Проверяем что пользователь существует и триал ещё не выдавался
        user_row = await db_helpers.get_user(user_id)
        if not user_row:
            return web.json_response({'ok': False, 'error': 'Пользователь не найден'}, status=404)
        if user_row.get('is_trial_used'):
            logger.info(f"[TRIAL-API] user={user_id} — триал уже использован, пропускаем")
            return web.json_response({'ok': False, 'error': 'Триал уже использован'}, status=409)
        if await db_helpers.get_active_subscription(user_id):
            logger.info(f"[TRIAL-API] user={user_id} — активная подписка уже есть")
            return web.json_response({'ok': False, 'error': 'Подписка уже активна'}, status=409)

        result = await grant_subscription(
            user_id=user_id,
            days_to_add=trial_days,
            is_trial=True,
            limit_ip=trial_limit_ip,
        )
        if result:
            logger.success(f"[TRIAL-API] Триал {trial_days}д выдан user={user_id} limit_ip={trial_limit_ip}")
            return web.json_response({'ok': True, 'trial_days': trial_days})
        else:
            logger.error(f"[TRIAL-API] grant_subscription вернул None для user={user_id}")
            return web.json_response({'ok': False, 'error': 'Ошибка выдачи триала'}, status=500)

    except Exception as e:
        logger.error(f"[TRIAL-API] Ошибка: {e}", exc_info=True)
        return web.json_response({'ok': False, 'error': str(e)}, status=500)

web_app.router.add_post('/api/grant-trial', api_grant_trial)


def _api_is_localhost(request: web.Request) -> bool:
    """True если запрос пришёл с loopback (как у /api/grant-trial)."""
    remote_host = request.remote
    x_real_ip = request.headers.get('X-Real-IP', '')
    x_forwarded_for = request.headers.get('X-Forwarded-For', '')
    allowed_hosts = ('127.0.0.1', '::1', 'localhost')
    return (
        remote_host in allowed_hosts or
        x_real_ip in allowed_hosts or
        (x_forwarded_for and x_forwarded_for.split(',')[0].strip() in allowed_hosts)
    )



# REST перезагрузки настроек удален по запросу


@dp.callback_query(F.data.startswith("renew_tariff_cryptobot_"))
async def cq_renew_tariff_cryptobot(query: CallbackQuery):
    """Создаёт CryptoBot-счёт; после оплаты приходит webhook ``invoice_paid``."""
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
        
    token = app_conf.get('cryptobot_token')
    
    logger.info(f"Используется CryptoBot ключ: '{token[:5]}...{token[-5:]}'" if token else "CryptoBot ключ НЕ НАЙДЕН")

    if not token:
        logger.error("CryptoBot: API-ключ не найден в настройках!")
        await query.answer("Оплата через CryptoBot временно недоступна.", show_alert=True)
        return

    try:
        tariff_id = int(query.data.split("_")[-1])
        tariff = await db_helpers.get_tariff_by_id(tariff_id)
        if not tariff: raise ValueError("Тариф не найден")
    except (ValueError, IndexError):
        logger.error(f"Неверный формат callback_data для CryptoBot: {query.data}")
        await query.answer("Ошибка в параметрах кнопки.", show_alert=True)
        return

    payload = f"{query.from_user.id}|{tariff_id}|{tariff['days']}|{tariff.get('limit_ip', 0)}"
    # CryptoBot — только RUB. Цена тарифа считается в рублях, CryptoBot сам
    # конвертит в крипту по своему курсу (currency_type='fiat', fiat='RUB').
    from src.pay import cryptobot_fiat_invoice_body
    invoice_amount_part = cryptobot_fiat_invoice_body(tariff['price'])

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                'https://pay.crypt.bot/api/createInvoice',
                headers={'Crypto-Pay-API-Token': token},
                json={
                    **invoice_amount_part,
                    'payload': payload,
                    'allow_comments': False,
                }
            )
            response.raise_for_status() 
            data = response.json()

        if data.get('ok'):
            invoice = data['result']
            invoice_id = invoice['invoice_id']
            pay_url = invoice['pay_url']
            
            payment_metadata = {
                "telegram_user_id": query.from_user.id,
                "subscription_days": tariff['days'],
                "price": tariff['price'],
                "limit_ip": tariff.get('limit_ip', 0),
                "tariff_id": tariff_id,
                "payment_method": "CryptoBot",
                "payload": payload
            }
            
            await db_helpers.add_payment(
                payment_id=f"CRYPTO_{invoice_id}",
                telegram_id=query.from_user.id,
                amount=tariff['price'],
                currency='RUB',
                metadata_json=json.dumps(payment_metadata, ensure_ascii=False)
            )
            
            # Формируем переменные для отображения, как в YooKassa, Platega и YooMoney
            days = tariff['days']
            price = float(tariff['price'])
            currency = 'RUB'
            price_str = int(price) if price == int(price) else f"{price:.2f}"
            limit_ip = int(tariff.get('limit_ip', 0) or 0)
            limit_ip_display = "∞" if limit_ip == 0 else str(limit_ip)
            description_text = ""
            if tariff.get('description') and tariff['description'].strip():
                description_text = f"\n{tariff['description'].strip()}\n"
            
            # Обновляем сообщение, показывая кнопку оплаты (в том же формате, что и другие методы)
            await query.message.edit_text(
                txt_payment_renewal(days, limit_ip_display, price_str, currency, description_text),
                reply_markup=keyboards.get_cryptobot_payment_keyboard(pay_url, invoice_id),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            logger.info(
                f"CryptoBot: создан счёт CRYPTO_{invoice_id}, ожидаем webhook invoice_paid"
            )
        else:
            raise Exception(f"CryptoBot API error: {data.get('error', 'Unknown error')}")

    except Exception as e:
        logger.error(f"Критическая ошибка создания инвойса CryptoBot для user {query.from_user.id}: {e}", exc_info=True)
        await query.answer("Не удалось создать счет. Попробуйте позже.", show_alert=True)
    
    await query.answer()


@dp.callback_query(F.data.startswith("check_crypto_payment_"))
async def cq_check_crypto_payment(query: CallbackQuery):
    """Ручная проверка счёта CryptoBot (fallback; основной путь — webhook invoice_paid)."""
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return

    invoice_id = int(query.data.split("_")[-1])
    await query.answer("Проверяем статус платежа", show_alert=False)

    token = app_conf.get('cryptobot_token')
    if not token:
        logger.error("CryptoBot: API-ключ не найден в настройках!")
        await query.message.edit_text("Ошибка: сервис оплаты временно недоступен.")
        return

    try:
        from src.pay.cryptobot import resolve_cryptobot_payment_row

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                "https://pay.crypt.bot/api/getInvoices",
                headers={"Crypto-Pay-API-Token": token},
                params={"invoice_ids": str(invoice_id)},
            )
            response.raise_for_status()
            data = response.json()

        if not data.get("ok"):
            raise Exception(f"CryptoBot API error: {data.get('error')}")

        items = (data.get("result") or {}).get("items") or []
        if not items:
            await query.answer("Счёт не найден в CryptoBot.", show_alert=True)
            return

        invoice = items[0]
        status = invoice.get("status")

        if status == "paid":
            payload_str = invoice.get("payload") or ""
            payment_id, row, payload_eff = await resolve_cryptobot_payment_row(
                invoice_id, str(payload_str or "")
            )
            if not payment_id or not row:
                logger.error(f"CryptoBot: оплаченный invoice_id={invoice_id} не сопоставлен с БД")
                await query.message.edit_text(
                    "✕ Платёж не найден. Напишите в поддержку.",
                    reply_markup=keyboards.get_back_to_main_keyboard(),
                )
                return
            if row[4] == "succeeded":
                await query.answer("✓ Этот платёж уже зачислен.", show_alert=True)
                return

            await handle_cryptobot_invoice_paid(payment_id, payload_eff)
            await query.answer("✓ Оплата зачислена.", show_alert=True)
            return

        if status == "active":
            await query.answer(
                "⏳ Платеж еще не получен. Пожалуйста, завершите оплату и попробуйте снова.",
                show_alert=True,
            )
            return

        await query.answer(
            f"Статус счета: '{status}'. Если вы считаете, что это ошибка, обратитесь в поддержку.",
            show_alert=True,
        )

    except Exception as e:
        logger.error(
            f"Ошибка проверки инвойса CryptoBot {invoice_id} для user {query.from_user.id}: {e}",
            exc_info=True,
        )
        await query.answer("Не удалось проверить статус счета. Попробуйте позже.", show_alert=True)



# --- Обработка успешной оплаты TG Star ---
@dp.message(F.successful_payment)
async def process_tgstar_payment(message: Message):
    # Проверяем блокировку
    if await check_user_blocked(message.from_user.id):
        await send_blocked_message(message.from_user.id)
        return
        
    payload = message.successful_payment.invoice_payload
    if not payload.startswith("tgstar_"):
        return

    # Проверяем, это продление трафика или продление подписки
    # ('traffic_reset' через отдельную кнопку удалён.)
    if "traffic_renewal" in payload:
        # Обработка продления трафика
        # Проверяем, есть ли тариф в payload
        tariff_id = None
        tariff_gb = None
        tariff_price = None
        
        if "traffic_renewal_tariff_" in payload:
            # Новый формат с тарифом: tgstar_traffic_renewal_tariff_{tariff_id}_{user_id}
            # split("_") → ["tgstar","traffic","renewal","tariff",tariff_id,user_id]
            #   индексы:        0        1         2        3         4         5
            try:
                parts = payload.split("_")
                tariff_id = int(parts[4])
                user_id = int(parts[5])
                telegram_user_id = user_id
                
                # Получаем тариф из базы данных
                tariff = await db_helpers.get_traffic_topup_tariff_by_id(tariff_id)
                if tariff:
                    tariff_dict = dict(tariff)
                    tariff_gb = tariff_dict.get('traffic_gb', 0)
                    tariff_price = tariff_dict.get('price', 0)
            except (ValueError, IndexError, Exception) as e:
                logger.error(f"TG Star: Ошибка при парсинге payload с тарифом: {e}")
                telegram_user_id = None
        else:
            # Старый формат: tgstar_traffic_renewal_{user_id}
            try:
                parts = payload.split("_")
                user_id = int(parts[-1])
                telegram_user_id = user_id
            except (ValueError, IndexError):
                logger.error(f"TG Star: Неверный user_id в payload: {payload}")
                await message.answer(
                    "✕ Не удалось определить пользователя. Напишите в поддержку.",
                    reply_markup=keyboards.get_back_to_main_keyboard()
                )
                return
        
        if telegram_user_id is None:
            logger.error(f"TG Star: Не удалось определить user_id из payload: {payload}")
            await message.answer(
                "✕ Не удалось определить пользователя. Напишите в поддержку.",
                reply_markup=keyboards.get_back_to_main_keyboard()
            )
            return
        
        # Получаем количество трафика для добавления
        if tariff_id and tariff_gb:
            # Используем данные из тарифа
            traffic_to_add_gb = tariff_gb
            price = tariff_price
        else:
            # Старая логика - используем настройки
            default_traffic_limit_gb = get_default_limit_gb()
            traffic_to_add_gb = default_traffic_limit_gb
            price = float('100')
        
        payment_id = f"TGSTAR_TRAFFIC_RENEWAL_{message.successful_payment.telegram_payment_charge_id}"
        paid_stars = int(getattr(message.successful_payment, 'total_amount', 0) or 0)
        payment_metadata = {
            "payment_type": "traffic_renewal",
            "telegram_user_id": telegram_user_id,
            "price": price,
            "traffic_to_add_gb": traffic_to_add_gb,
            "payment_method": "TG Star",
            "paid_stars": paid_stars,
        }
        if tariff_id:
            payment_metadata["tariff_id"] = tariff_id
        
        try:
            await db_helpers.add_payment(
                payment_id=payment_id,
                telegram_id=telegram_user_id,
                amount=price,
                currency='RUB',
                metadata_json=json.dumps(payment_metadata)
            )
            # Не выставляем 'succeeded' заранее: иначе process_successful_payment
            # уйдёт в retry-ветку и запишет в лог обманчивое «уже был обработан».
            # Реальное применение трафика и нормальный путь — в основном
            # обработчике (line ~1003), статус ставится там после успеха.
            logger.info(f"TG Star: Платеж продления трафика {payment_id} сохранен в БД для пользователя {telegram_user_id}")
        except Exception as e:
            logger.error(f"TG Star: Ошибка сохранения платежа в БД: {e}")
        
        # Обрабатываем продление трафика
        await process_successful_payment(telegram_user_id, payment_id, payment_metadata)
        return
        
    # Обработка продления подписки (существующая логика)
    _, tariff_id, user_id = payload.split("_")
    tariffs = await db_helpers.get_active_tariffs()
    tariff = next((t for t in tariffs if str(t['id']) == tariff_id), None)
    
    if not tariff:
        await message.answer(
            "Ошибка: тариф не найден",
            reply_markup=keyboards.get_back_to_main_keyboard()
        )
        return
        
    days = int(tariff['days'])
    limit_ip = int(tariff.get('limit_ip', 0))
    traffic_gb_to_add = tariff.get('traffic_gb', 0) or 0
    
    # Убедимся, что user_id - это целое число для функции grant_subscription
    try:
        telegram_user_id = int(user_id)
    except ValueError:
        logger.error(f"TG Star: Неверный user_id в payload: {user_id}")
        await message.answer(
            "✕ Не удалось определить пользователя. Напишите в поддержку.",
            reply_markup=keyboards.get_back_to_main_keyboard()
        )
        return

    # Сохраняем платеж в БД. TG Stars — только RUB: цена тарифа в ₽,
    # реально списанные звёзды кладём в metadata для отчётности.
    payment_id = f"TGSTAR_{message.successful_payment.telegram_payment_charge_id}"
    paid_stars = int(getattr(message.successful_payment, 'total_amount', 0) or 0)
    payment_metadata = {
        "telegram_user_id": telegram_user_id,
        "subscription_days": days,
        "limit_ip": limit_ip,
        "payment_method": "tgstar",
        "tariff_id": tariff_id,
        "paid_stars": paid_stars,
    }

    try:
        await db_helpers.add_payment(
            payment_id=payment_id,
            telegram_id=telegram_user_id,
            amount=float(tariff['price']),
            currency='RUB',
            metadata_json=json.dumps(payment_metadata)
        )
        logger.info(f"TG Star: Платеж {payment_id} сохранен в БД для пользователя {telegram_user_id}")
    except Exception as e:
        logger.error(f"TG Star: Ошибка сохранения платежа в БД: {e}")

    result = await grant_subscription(telegram_user_id, days, is_trial=False, limit_ip=limit_ip, traffic_gb_to_add=traffic_gb_to_add)
    
    if result:
        await db_helpers.update_payment_status(payment_id, "succeeded")
        # Партнёрка + реферальный бонус — единый хелпер.
        try:
            await _apply_partner_and_referral(
                payer_user_id=telegram_user_id,
                payment_id=payment_id,
                amount_rub=float(tariff['price']),
                currency='RUB',
                log_prefix="TG Star",
            )
        except Exception as e:
            logger.error(f"Партнёрка/реферал (TG Star) ошибка: {e}")

        
        # Используем настройку text_payment_success, как в других методах оплаты
        try:
            expiry_date = result.get('expiry_date')
            if expiry_date and isinstance(expiry_date, datetime):
                expiry_date_str = format_msk_date(expiry_date)
            else:
                if expiry_date:
                    logger.warning(f"TG Star: expiry_date не является datetime объектом: {type(expiry_date)}")
                else:
                    logger.warning(f"TG Star: expiry_date отсутствует в result для платежа {payment_id}")
                expiry_date_str = format_msk_date(datetime.now(timezone.utc) + timedelta(days=days))
                logger.warning(f"TG Star: используется fallback дата для платежа {payment_id}")
            
            remnawave_traffic_info = result.get('remnawave_traffic_info')
            traffic_info_text = ""
            if remnawave_traffic_info:
                added_gb = remnawave_traffic_info.get('added_gb')
                if added_gb:
                    traffic_info_text = f"\n\nТрафик: добавлено {added_gb} GB"
            
            tpl = (app_conf.get('text_payment_success') or '').replace('{sub_link}', '')
            success_message = tpl.format(days=days, expiry_date=expiry_date_str) + traffic_info_text
            
            await message.answer(
                success_message,
                reply_markup=keyboards.get_success_with_referral_keyboard()
            )
            logger.info(f"TG Star: подписка активирована для user_id={telegram_user_id} на {days} дней.")
        except Exception as e:
            logger.error(f"TG Star: ошибка отправки уведомления пользователю {telegram_user_id}: {e}", exc_info=True)
            try:
                await message.answer(
                    f"✓ Платёж обработан. Подписка продлена на {days} дней.",
                    reply_markup=keyboards.get_back_to_main_keyboard()
                )
            except Exception as e2:
                logger.error(f"TG Star: не удалось отправить даже базовое сообщение пользователю {telegram_user_id}: {e2}")
    else:
        await db_helpers.update_payment_status(payment_id, "failed")
        await _notify_payment_grant_failed(telegram_user_id)
        logger.error(f"TG Star: ошибка активации подписки для user_id={telegram_user_id} на {days} дней!")

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    logger.info(f"TG Star: pre_checkout_query подтвержден для user_id={pre_checkout_query.from_user.id}")


@dp.callback_query(F.data == "website_access")
async def cq_website_access(query: CallbackQuery, state: FSMContext):
    """Доступ к личному кабинету на сайте."""
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return

    site_url = os.getenv('SITE_URL', '').strip() or (app_conf.get('website_url') or '').strip()
    if not site_url:
        await query.answer("Сайт не настроен", show_alert=True)
        return

    user = await db_helpers.get_user(query.from_user.id)
    email = (user.get('email') or '').strip() if user else ''

    # 1. Если email не привязан — просим привязку; «Мои устройства» всё равно показываем при HWID
    if not email or email.startswith('tg:'):
        kb_rows = [
            [btn('btn_website_link_email', callback_data='website_link_email')],
        ]
        kb_rows.append([btn('btn_back_to_main', callback_data='back_to_main')])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        text = setting_text(
            app_conf.get('text_website_cabinet_no_email'),
            DEFAULT_TEXT_WEBSITE_CABINET_NO_EMAIL,
        )
        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        await query.answer()
        return

    # 2. Если email есть — генерируем magic link (одноразовый токен на 10 минут)
    magic_token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    try:
        await db_helpers.save_web_auth_token(email, magic_token, expires_at)
        magic_url = f"{site_url}/auth/magic/{magic_token}"
    except Exception as e:
        logger.warning(f"[MAGIC] Ошибка генерации magic link: {e}")
        magic_url = site_url

    # Проверяем активность подписки
    sub_end = user.get('subscription_end_date') if user else None
    if sub_end:
        try:
            if isinstance(sub_end, str):
                sub_end = datetime.fromisoformat(sub_end)
            tz = sub_end.tzinfo
            sub_active = sub_end > datetime.now(tz) if tz else sub_end > datetime.now()
        except Exception:
            sub_active = False
    else:
        sub_active = False

    kb_rows = [
        [btn('btn_website_open', url=magic_url)],
    ]

    if not sub_active:
        # Подписка истекла или отсутствует
        kb_rows.append([btn('btn_renew_sub', callback_data='shop_renew')])
        kb_rows.append([btn('btn_back_to_main', callback_data='back_to_main')])
        await query.message.edit_text(
            setting_text(
                app_conf.get('text_website_cabinet_expired'),
                DEFAULT_TEXT_WEBSITE_CABINET_EXPIRED,
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            disable_web_page_preview=True,
        )
        await query.answer()
        return


    # Кнопку "Привязать email" мы отсюда убрали, так как сюда доходят только те, кто уже привязал.
    kb_rows.append([btn('btn_back_to_main', callback_data='back_to_main')])

    await query.message.edit_text(
        setting_text(
            app_conf.get('text_website_cabinet_active'),
            DEFAULT_TEXT_WEBSITE_CABINET_ACTIVE,
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        disable_web_page_preview=True,
    )
    await query.answer()




@dp.callback_query(F.data == "website_link_email")
async def cq_website_link_email(query: CallbackQuery, state: FSMContext):
    """Предлагаем привязать реальный email."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✕ Отмена", callback_data="website_access")]
    ])
    await query.message.edit_text(
        "<b>Привязать email</b>\n"
        "○ Ожидаем адрес\n\n"
        "Введите email — пришлём код подтверждения.",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.set_state(WebsiteEmailLink.waiting_email)
    await query.answer()


@dp.message(WebsiteEmailLink.waiting_email)
async def wsl_email_input(message: Message, state: FSMContext):
    """Получаем email от пользователя."""
    import re as _re, secrets as _sec
    email = message.text.strip().lower()
    if not _re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        await message.answer("✕ Неверный формат email. Попробуйте ещё раз:")
        return

    # Проверяем что email не занят другим пользователем
    existing = await db_helpers.get_web_user_by_email(email)
    if existing and existing.get('telegram_id') != message.from_user.id:
        await message.answer(
            "✕ Этот email уже привязан к другому аккаунту.\n"
            "Введите другой email:"
        )
        return

    from email_domain_policy import (
        SETTING_KEY,
        config_from_setting_value,
        is_email_domain_allowed,
        EMAIL_DOMAIN_REJECT_MESSAGE,
    )
    wc_cfg = config_from_setting_value(
        await db_helpers.get_setting_by_key(SETTING_KEY, ''),
    )
    if not is_email_domain_allowed(
        email, wc_cfg, user_already_exists=await db_helpers.email_registered_in_db(email),
    ):
        await message.answer(f"✕ {EMAIL_DOMAIN_REJECT_MESSAGE}\n\nВведите другой email:")
        return

    code = f"{_sec.randbelow(1000000):06d}"
    await state.update_data(email=email, code=code)
    await state.set_state(WebsiteEmailLink.waiting_code)

    # Отправляем код через email_sender
    try:
        import os as _os
        from email_sender import send_email, code_email_html
        project_name = app_conf.get('project_name', 'Сервис')
        await send_email(
            to=email,
            subject=f"Код подтверждения — {project_name}",
            html=code_email_html(code, project_name),
            smtp_from=_os.getenv('SMTP_FROM', ''),
        )
        sent_ok = True
    except Exception as e:
        logger.error(f"[WEBSITE] Ошибка отправки кода на {email}: {e}")
        sent_ok = False

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✕ Отмена", callback_data="back_to_main")]
    ])
    if sent_ok:
        await message.answer(
            "<b>Подтверждение email</b>\n"
            f"✓ Код отправлен на <code>{email}</code>\n\n"
            "Введите 6-значный код:",
            parse_mode="HTML",
            reply_markup=kb
        )
    else:
        await message.answer(
            f"<b>Подтверждение email</b>\n⚠ Письмо на <code>{email}</code> не отправлено\n\n"
            "Проверьте адрес и попробуйте ещё раз или введите другой email:",
            parse_mode="HTML",
            reply_markup=kb
        )
        await state.set_state(WebsiteEmailLink.waiting_email)


@dp.message(WebsiteEmailLink.waiting_code)
async def wsl_code_input(message: Message, state: FSMContext):
    """Проверяем код и привязываем email."""
    import os as _os
    data = await state.get_data()
    expected = data.get('code', '')
    email = data.get('email', '')

    if message.text.strip() != expected:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="back_to_main")]
        ])
        await message.answer("✕ Неверный код. Попробуйте ещё раз:", reply_markup=kb)
        return

    from email_domain_policy import (
        SETTING_KEY,
        config_from_setting_value,
        is_email_domain_allowed,
        EMAIL_DOMAIN_REJECT_MESSAGE,
    )
    wc_cfg = config_from_setting_value(
        await db_helpers.get_setting_by_key(SETTING_KEY, ''),
    )
    if not is_email_domain_allowed(
        email, wc_cfg, user_already_exists=await db_helpers.email_registered_in_db(email),
    ):
        await state.clear()
        await message.answer(f"✕ {EMAIL_DOMAIN_REJECT_MESSAGE}")
        return

    # Привязываем email
    await db_helpers.update_user_email(message.from_user.id, email)
    await state.clear()

    site_url = _os.getenv('SITE_URL', '').strip() or (app_conf.get('website_url') or '').strip()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть кабинет", url=site_url)],
        [InlineKeyboardButton(text=app_conf.get('back_to_main', '‹ В меню'), callback_data="back_to_main")],
    ])
    await message.answer(
        "<b>Email привязан</b>\n"
        f"✓ <code>{email}</code>\n\n"
        "Подписка уже отображается в кабинете.",
        parse_mode="HTML",
        reply_markup=kb
    )


@dp.callback_query(F.data == "referral_program")
async def cq_referral_program(query: CallbackQuery):
    # Проверяем блокировку
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return
        
    user_id = query.from_user.id
    user = await db_helpers.get_user(user_id)
    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={user_id}"
    
    # Получаем статистику рефералов
    referrals_today = await db_helpers.get_referrals_count_today(user_id)
    max_per_day = int(app_conf.get('referral_limit_per_day', 3))
    remaining_invites = max(0, max_per_day - referrals_today)
    
    # Получаем текст из настроек
    text_template = app_conf.get(
        'text_referral_program', REST_TEXT_DEFAULTS['text_referral_program']
    )
    
    # Формируем ссылку на сайт для реферальной программы
    import os as _os
    _site_url = _os.getenv('SITE_URL', '').strip() or (app_conf.get('website_url') or '').strip()
    ref_link_url = f"{_site_url}/?ref={user_id}" if _site_url else ''

    # Заполняем шаблон
    text = _filter_empty_menu_fields(text_template.format(
        ref_link=ref_link,
        ref_link_url=ref_link_url,
        used_invites=referrals_today,
        remaining_invites=remaining_invites,
        max_per_day=max_per_day,
        join_days=app_conf.get('ref_bonus_on_join_days', 3),
        payment_days=app_conf.get('ref_bonus_on_payment_days', 7)
    ))
    # Клавиатура: Поделиться, Мои рефералы, Назад
    from urllib.parse import quote
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    # Формируем текст приглашения без ссылки, а ссылку передаем отдельно через url — так не будет дубля
    share_text_template = app_conf.get(
        'text_referral_share', REST_TEXT_DEFAULTS['text_referral_share']
    )
    try:
        share_text = share_text_template.format(ref_link=ref_link)
    except Exception:
        share_text = app_conf.get(
            'text_referral_share', REST_TEXT_DEFAULTS['text_referral_share']
        )
    # Убираем ссылку из текста, если она туда попала
    share_text = (share_text or '').replace(ref_link, '').strip()
    if not share_text:
        share_text = REST_TEXT_DEFAULTS['text_referral_share']
    share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={quote(share_text)}"
    # Кнопки: Поделиться, (опционально) Партнёрская MiniApp, Мои рефералы, Назад
    buttons_rows = []
    buttons_rows.append([btn('btn_referral_share', url=share_url)])
    try:
        # Добавляем кнопку MiniApp партнёрки внутрь реферального раздела
        connect_url = app_conf.get('connect_page_url', '').strip() or ''
        last_sub = await db_helpers.get_last_subscription(user_id)
        sub_uuid = last_sub['xui_client_uuid'] if last_sub and last_sub.get('xui_client_uuid') else None
        
        # Проверяем индивидуальную настройку пользователя
        # Используем уже полученного пользователя из строки 7084, чтобы не делать лишний запрос
        user_show_partner = None
        if user:
            # aiosqlite.Row поддерживает доступ по ключу через []
            # Пробуем получить значение напрямую (может быть None если поле NULL)
            try:
                user_show_partner = user['show_partner_program_button']
                # Если значение пустая строка, считаем как None
                if user_show_partner == '':
                    user_show_partner = None
            except (KeyError, IndexError, AttributeError, TypeError):
                # Если поле отсутствует или ошибка доступа, используем None
                user_show_partner = None
        
        # Определяем, показывать ли кнопку:
        # 1. Если у пользователя индивидуальная настройка '1' - показываем
        # 2. Если у пользователя индивидуальная настройка '0' - скрываем
        # 3. Если NULL/не задана - используем глобальную настройку
        should_show = False
        if user_show_partner == '1' or user_show_partner == 1:
            should_show = True
        elif user_show_partner == '0' or user_show_partner == 0:
            should_show = False
        else:
            # Используем глобальную настройку
            global_setting = app_conf.get('show_partner_program_button', '1')
            should_show = (str(global_setting).lower() in ('1','true','yes'))
        
        # Логирование для отладки
        from loguru import logger
        logger.debug(f"[REFERRAL] Партнёрская программа: user_id={user_id}, user_show_partner={user_show_partner}, global_setting={app_conf.get('show_partner_program_button', '1')}, should_show={should_show}, connect_url={bool(connect_url)}, sub_uuid={bool(sub_uuid)}")
        
        if should_show:
            buttons_rows.append([btn('btn_partner_program', callback_data='partner_program')])
    except Exception as e:
        from loguru import logger
        logger.error(f"[REFERRAL] Ошибка при добавлении кнопки партнёрской программы: {e}", exc_info=True)
    buttons_rows.append([btn('btn_my_referrals', callback_data='my_referrals')])
    buttons_rows.append([btn('btn_back_to_main', callback_data='back_to_main')])
    reply_markup = InlineKeyboardMarkup(inline_keyboard=buttons_rows)
    await query.message.edit_text(text, reply_markup=reply_markup)
    await query.answer()

# ─────────────────────────────────────────────────────────────────────────────
# ПАРТНЁРСКАЯ ПРОГРАММА (bot-native, без MiniApp)
# ─────────────────────────────────────────────────────────────────────────────

import string as _string


def _gen_partner_code(telegram_id: int) -> str:
    postfix = ''.join(random.choice(_string.ascii_lowercase) for _ in range(5))
    return f"{telegram_id}{postfix}"


async def _ensure_partner_code(user_id: int) -> str | None:
    """Возвращает partner_ref_code пользователя, генерирует и сохраняет если ещё нет."""
    try:
        async with db_helpers.get_db_connection_safe() as db:
            row = await (await db.execute(
                "SELECT partner_ref_code FROM users WHERE telegram_id = ?", (user_id,)
            )).fetchone()
            if row and row[0]:
                return row[0]
            # Генерируем уникальный код
            for _ in range(10):
                code = _gen_partner_code(user_id)
                existing = await (await db.execute(
                    "SELECT 1 FROM users WHERE partner_ref_code = ?", (code,)
                )).fetchone()
                if not existing:
                    await db.execute(
                        "UPDATE users SET partner_ref_code = ? WHERE telegram_id = ?", (code, user_id)
                    )
                    await db.commit()
                    return code
    except Exception as e:
        logger.error(f"[PARTNER] Ошибка генерации кода для {user_id}: {e}")
    return None


async def _get_partner_stats(user_id: int) -> dict:
    """Возвращает статистику партнёра одним запросом."""
    try:
        async with db_helpers.get_db_connection_safe() as db:
            # Баланс и код
            row = await (await db.execute(
                "SELECT partner_balance_rub, partner_ref_code FROM users WHERE telegram_id = ?",
                (user_id,)
            )).fetchone()
            balance = float(row[0] or 0) if row else 0.0
            # Количество рефералов
            ref_row = await (await db.execute(
                "SELECT COUNT(*) FROM users WHERE invited_by = ? AND invited_by_method = 'partner'",
                (user_id,)
            )).fetchone()
            ref_count = ref_row[0] if ref_row else 0
            pay_row = await (await db.execute(
                "SELECT COUNT(*) FROM partner_accruals WHERE partner_id = ?",
                (user_id,)
            )).fetchone()
            pay_count = pay_row[0] if pay_row else 0
        return {'balance': balance, 'ref_count': ref_count, 'pay_count': pay_count}
    except Exception as e:
        logger.error(f"[PARTNER] Ошибка получения статистики для {user_id}: {e}")
        return {'balance': 0.0, 'ref_count': 0, 'pay_count': 0}


def _get_partner_min_withdraw_rub() -> int:
    try:
        v = int(float(app_conf.get('partner_min_withdraw_rub', '500') or 500))
        return max(1, v)
    except (ValueError, TypeError):
        return 500


def _partner_main_kb(balance: float, min_withdraw: int) -> InlineKeyboardMarkup:
    rows = [
        [btn('btn_partner_accruals', callback_data='partner_accruals')],
    ]
    if balance >= min_withdraw:
        rows.append([btn('btn_partner_withdraw', callback_data='partner_withdraw')])
    rows.append([btn('btn_back', callback_data='referral_program')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "partner_program")
async def cq_partner_program(query: CallbackQuery):
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return

    user_id = query.from_user.id
    await query.answer()

    code = await _ensure_partner_code(user_id)
    stats = await _get_partner_stats(user_id)
    percent = await db_helpers.get_partner_percent(user_id)
    bot_me = await bot.get_me()
    min_withdraw = _get_partner_min_withdraw_rub()

    if code:
        partner_link = f"https://t.me/{bot_me.username}?start=par_{code}"
        link_line = f"\nСсылка в Telegram: <code>{partner_link}</code>\n"
    else:
        link_line = "\n⚠ Не удалось создать ссылку. Попробуйте позже.\n"

    _site_url = os.getenv('SITE_URL', '').strip() or (app_conf.get('website_url') or '').strip()
    if _site_url:
        base = _site_url.rstrip('/')
        site_href = f"{base}/?ref=par_{code}" if code else base
        link_line += f"Ссылка на сайт: <code>{html.escape(site_href)}</code>\n"

    balance_str = f"{stats['balance']:.2f}".rstrip('0').rstrip('.')
    pay_count = int(stats.get('pay_count', 0) or 0)

    text = txt_partner_program(
        balance_str, percent, stats['ref_count'], pay_count, link_line, min_withdraw,
        template=app_conf.get('text_partner_program'),
    )

    await query.message.edit_text(
        text,
        reply_markup=_partner_main_kb(stats['balance'], min_withdraw),
        disable_web_page_preview=True,
    )


@dp.callback_query(F.data == "partner_accruals")
async def cq_partner_accruals(query: CallbackQuery):
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return

    user_id = query.from_user.id
    await query.answer()

    accruals = await db_helpers.get_partner_accruals(user_id, limit=15)

    if not accruals:
        text = (
            "<b>История начислений</b>\n○ Начислений пока нет\n\n"
            "Первое появится после оплаты приглашённого."
        )
    else:
        lines = ["<b>История начислений</b>\n✓ Последние начисления\n"]
        for i, a in enumerate(accruals, 1):
            try:
                dt = datetime.fromisoformat(a['created_at'].replace('Z', '+00:00'))
                dt_msk = format_msk_date(dt, '%d.%m.%Y %H:%M')
            except Exception:
                dt_msk = a.get('created_at', '—')[:16]
            bonus = float(a.get('bonus') or 0)
            payer = a.get('payer_id', '—')
            lines.append(f"{i}. {dt_msk} — <b>+{bonus:.2f} ₽</b> (от <code>{payer}</code>)")
        text = '\n'.join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn('btn_back', callback_data='partner_program')]
    ])
    await query.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data == "partner_withdraw")
async def cq_partner_withdraw(query: CallbackQuery):
    if await check_user_blocked(query.from_user.id):
        await send_blocked_message(query.from_user.id, query)
        return

    user_id = query.from_user.id
    min_w = _get_partner_min_withdraw_rub()
    stats = await _get_partner_stats(user_id)
    if stats['balance'] < min_w:
        await query.answer(f"Минимум для вывода: {min_w} ₽", show_alert=True)
        return
    await query.answer()

    balance_str = f"{stats['balance']:.2f}".rstrip('0').rstrip('.')
    support_url = app_conf.get('support_url', '')

    text = txt_withdraw_request(
        balance_str, user_id,
        template=app_conf.get('text_partner_withdraw'),
    )

    rows = []
    if support_url:
        rows.append([InlineKeyboardButton(text='Написать в поддержку', url=support_url)])
    rows.append([btn('btn_back', callback_data='partner_program')])

    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        disable_web_page_preview=True,
    )


# --- Событие запуска бота ---
async def on_startup(dispatcher: Dispatcher):
    """
    Выполняется при запуске бота:
    - Инициализация базы данных
    - Загрузка настроек из базы
    - Регистрация URL CryptoBot (setWebhook), если задан connect_page_url
    - Проверка подключения к X-UI серверам
    - Возобновление проверки ожидающих платежей
    """
    global bot
    await db_helpers.init_db()
    await app_conf.load_settings()
    migrated_rw_db = migrate_remnawave_db_if_needed()
    if migrated_rw_db and os.path.isfile(migrated_rw_db):
        logger.info(f"Remnawave traffic DB: {migrated_rw_db}")
    await apply_bot_session_from_settings(dispatcher)
    YKConfig.account_id = app_conf.get('yookassa_shop_id', '')
    YKConfig.secret_key = app_conf.get('yookassa_secret_key', '')
    bot_info = await bot.get_me()
    logger.success(f"Бот @{bot_info.username} запущен!")

    try:
        _tok = (app_conf.get("cryptobot_token") or "").strip()
        _base = (app_conf.get("connect_page_url") or "").strip().rstrip("/")
        if _tok and _base:
            from src.pay.cryptobot import cryptobot_set_webhook_url

            _wurl = f"{_base}/cryptobot/"
            _ok, _msg = await cryptobot_set_webhook_url(_tok, _wurl)
            if _ok:
                logger.success(f"CryptoBot: webhook зарегистрирован ({_wurl})")
            else:
                logger.warning(f"CryptoBot: setWebhook не удалось — {_msg} (задайте URL вручную в @CryptoBot)")
    except Exception as _e:
        logger.warning(f"CryptoBot: регистрация webhook пропущена: {_e}")

    # Подготовка: прогреть кэш видео-инструкции (получить file_id в settings)
    try:
        video_path = os.path.join(os.path.dirname(__file__), 'ins.mp4')
        file_exists = os.path.isfile(video_path)
        if not file_exists:
            # Файл отсутствует — очищаем сохранённые file_id/hash, чтобы точно не слать видео
            try:
                await set_setting_value('ins_video_file_id', '')
                await set_setting_value('ins_video_hash', '')
            except Exception:
                pass
        else:
            cached_file_id = app_conf.get('ins_video_file_id', '')
            current_hash = _file_sha256(video_path)
            saved_hash = app_conf.get('ins_video_hash', '')
            # Если файл обновился — инвалидируем старый file_id
            if saved_hash and current_hash != saved_hash:
                cached_file_id = ''
            if not cached_file_id:
                # Получаем список администраторов из admin_ids
                admin_ids = []
                admins_str = app_conf.get('admin_ids', '')
                if admins_str:
                    try:
                        admin_ids = [int(x.strip()) for x in admins_str.split(',') if x.strip().isdigit()]
                    except Exception:
                        admin_ids = []
                
                # Если admin_ids пуст, используем fallback из backup_settings
                if not admin_ids:
                    try:
                        async with db_helpers.get_db_connection_safe() as db:
                            async with db.execute("SELECT admin_telegram_id FROM backup_settings LIMIT 1") as cur:
                                row = await cur.fetchone()
                                if row and row[0] and str(row[0]).isdigit():
                                    admin_ids = [int(row[0])]
                    except Exception:
                        pass
                
                # Отправляем первому администратору из списка
                if admin_ids:
                    try:
                        admin_id = admin_ids[0]  # Берем первого администратора
                        msg = await bot.send_video(admin_id, video=FSInputFile(video_path), caption='Кешируем инструкцию для ускорения старта новых пользователей. Можно удалить это сообщение.')
                        if msg and getattr(msg, 'video', None) and msg.video.file_id:
                            await set_setting_value('ins_video_file_id', msg.video.file_id)
                            if current_hash:
                                await set_setting_value('ins_video_hash', current_hash)
                    except Exception as e:
                        logger.warning(f"Не удалось прогреть кэш видео через отправку админу: {e}")
    except Exception as e:
        logger.warning(f"Видео-кеш старт: {e}")
    
    # Инициализация Remnawave SDK (если включен)
    remnawave_enabled = app_conf.get('remnawave_enabled', False)
    remnawave_base_url = app_conf.get('remnawave_base_url', '')
    remnawave_api_token = app_conf.get('remnawave_api_token', '')
    
    if remnawave_enabled and remnawave_base_url and remnawave_api_token:
        try:
            await remnawave_manager_instance._ensure_initialized()
            logger.success("Remnawave SDK успешно инициализирован!")
        except Exception as e:
            logger.error(f"Ошибка инициализации Remnawave SDK: {e}")
            logger.warning("Бот продолжит работу без Remnawave")
    elif remnawave_enabled:
        logger.warning("Remnawave включен в настройках, но не указаны base_url или api_token")
    
    pending_payments = await db_helpers.get_pending_payments()
    logger.info(f"Найдено {len(pending_payments)} pending платежей для проверки при старте")
    for p in pending_payments:
        pid, uid, _, _, _, created_at_str, meta_str = p
        if str(pid).startswith("CRYPTO_"):
            logger.debug(f"[STARTUP] Пропуск pending CryptoBot {pid} — зачисление через webhook")
            continue
        meta = json.loads(meta_str) if meta_str else {}
        created_at = datetime.fromisoformat(created_at_str).replace(tzinfo=timezone.utc)
        age_minutes = (datetime.now(timezone.utc) - created_at).total_seconds() / 60
        if age_minutes < 15:
            logger.info(f"Возобновление автопроверки для платежа {pid}, возраст: {age_minutes:.1f} мин, metadata: {meta}")
            try:
                async def task_wrapper():
                    try:
                        await auto_check_payment_status(pid, uid, meta)
                    except Exception as e:
                        logger.error(f"Критическая ошибка в автопроверке при старте для платежа {pid}: {e}", exc_info=True)
                
                task = asyncio.create_task(task_wrapper())
                active_payment_checkers[pid] = task
                logger.info(f"Автопроверка возобновлена для платежа {pid}, задача создана: {task}")
            except Exception as e:
                logger.error(f"Ошибка возобновления автопроверки для платежа {pid}: {e}", exc_info=True)
        else:
            logger.debug(f"Пропуск платежа {pid} - слишком старый ({age_minutes:.1f} мин)")

# --- Событие остановки бота ---
async def on_shutdown(dispatcher: Dispatcher):
    """
    Выполняется при остановке бота:
    - Отмена всех фоновых задач
    - Закрытие соединений с Remnawave
    - Завершение работы
    """
    logger.info("Бот останавливается...")
    for task in active_payment_checkers.values():
        task.cancel()
    
    # Закрываем соединение с Remnawave
    try:
        await remnawave_manager_instance.close()
        logger.info("Соединение с Remnawave закрыто")
    except Exception as e:
        logger.warning(f"Ошибка при закрытии соединения с Remnawave: {e}")
    
    await asyncio.sleep(1)
    logger.info("Бот остановлен.")

# --- Главная точка входа ---
async def main():
    """
    Главная функция запуска бота:
    - Регистрирует события запуска и остановки
    - Регистрирует админские обработчики
    - Загружает настройки из базы
    - Запускает фоновую задачу напоминаний
    - Запускает webhook сервер для YooMoney
    - Запускает polling aiogram
    """
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    admin.register_admin_handlers(dp)
    # Регистрируем обработчики подписок из src
    register_subscription_handlers(dp, check_user_blocked, send_blocked_message, show_main_menu)
    # Регистрируем обработчики расширения лимита устройств
    # Каталог роутеров и оформление заказа (товары и заказы — в основном приложении)
    register_router_catalog_handlers(dp, check_user_blocked, send_blocked_message)
    try:
        # До polling нужны актуальный токен/прокси из БД: иначе start_polling(bot) держит
        # старый bootstrap-Bot из верха файла, а on_startup уже создаёт другой — long polling идёт мимо прокси.
        await db_helpers.init_db()
        await app_conf.load_settings()
        await apply_bot_session_from_settings(dp)
        start_notification_tasks(bot)
        # Очередь основного приложения: напоминания об окончании подписки,
        # подтверждения оплаты и алерты оператору. Отправляем мы — токен
        # есть только у нас, и клиент разговаривает именно с этим ботом.
        start_outbox(bot)
        # Тарифы — один список на систему: правятся здесь, считает по ним каталог.
        start_tariff_sync()
        start_stale_payments_task(active_payment_checkers)
        start_expired_traffic_reset_task()

        # Запускаем webhook-сервер (127.0.0.1:8081): YooMoney, Platega, CryptoBot, …
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 8081)  # Слушаем на всех интерфейсах
        await site.start()
        logger.info("Webhook сервер запущен на http://127.0.0.1:8081")
        connect_url = app_conf.get('connect_page_url', '').strip() or ''
        if connect_url:
            connect_url = connect_url.rstrip('/')
            logger.info(f"  - YooMoney: {connect_url}/yoomoney/")
            logger.info(f"  - Platega: {connect_url}/platega/callback")
            logger.info(f"  - YooKassa: {connect_url}/yookassa/webhook")
            logger.info(f"  - CryptoBot: {connect_url}/cryptobot/")
            logger.info(f"  - Remnawave: {connect_url}/remnawave/webhook")
            logger.info(f"  - Wata: {connect_url}/wata/webhook")
        else:
            logger.info("  - YooMoney: http://127.0.0.1:8081/yoomoney/")
            logger.info("  - Platega: http://127.0.0.1:8081/platega/callback")
            logger.info("  - YooKassa: http://127.0.0.1:8081/yookassa/webhook")
            logger.info("  - CryptoBot: http://127.0.0.1:8081/cryptobot/")
            logger.info("  - Remnawave: http://127.0.0.1:8081/remnawave/webhook")
            logger.info("  - Wata: http://127.0.0.1:8081/wata/webhook")
        
        await dp.start_polling(bot)  # Запускаем polling aiogram
    finally:
        if bot and bot.session:
            await bot.session.close()

# Обработчик admin_renew_subscription_free перенесен в src/subscription_handlers.py

# --- Запуск скрипта ---
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    asyncio.run(main())
