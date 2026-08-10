"""
Сервис расчёта и применения апгрейда лимита устройств.

Принципы:
- Меняем ТОЛЬКО колонку users.limit_ip в БД.
- Сроки подписки не уменьшаются никогда.
- Только увеличение лимита (downgrade — отдельная операция, через админку).
- На X-UI/Remnawave новый лимит уезжает при следующем продлении/обновлении подписки
  (через стандартный путь grant_subscription).

Модель ценообразования B (за слот в день):
    price = (new_limit - old_limit) * days_left * price_per_slot_per_day

Все настройки хранятся в таблице settings:
- device_upgrade_enabled            — фича включена (0/1)
- device_upgrade_max_limit          — потолок лимита через self-service
- device_upgrade_min_days_left      — минимум дней подписки, чтобы апгрейдить
- device_upgrade_min_price          — минимальный чек (округление вверх)
- device_upgrade_price_per_slot_per_day — цена слота в день, ₽
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import pytz

import db_helpers
from app_config import app_conf

logger = logging.getLogger(__name__)


# ---- Утилиты ----

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _moscow_tz():
    return pytz.timezone('Europe/Moscow')


def _parse_dt(value) -> Optional[datetime]:
    """Парсит дату из БД. Возвращает aware-datetime в UTC или None."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except Exception:
            try:
                dt = datetime.strptime(str(value), '%Y-%m-%d %H:%M:%S')
            except Exception:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _setting_int(key: str, default: int) -> int:
    try:
        v = app_conf.get(key, default)
        if v is None or str(v).strip() == '':
            return default
        return int(v)
    except (ValueError, TypeError):
        return default


def _setting_float(key: str, default: float) -> float:
    try:
        v = app_conf.get(key, default)
        if v is None or str(v).strip() == '':
            return default
        return float(v)
    except (ValueError, TypeError):
        return default


def _setting_bool(key: str, default: bool) -> bool:
    v = app_conf.get(key, '1' if default else '0')
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


# ---- Основная бизнес-логика ----

def _config_snapshot() -> dict:
    """Собирает текущие настройки апгрейда в один dict."""
    return {
        'enabled': _setting_bool('device_upgrade_enabled', False),
        'max_limit': _setting_int('device_upgrade_max_limit', 20),
        'min_days_left': _setting_int('device_upgrade_min_days_left', 3),
        'min_price': _setting_float('device_upgrade_min_price', 30.0),
        'price_per_slot_per_day': _setting_float('device_upgrade_price_per_slot_per_day', 5.0),
    }


def _calc_price(old_limit: int, new_limit: int, days_left: int, price_per_slot_per_day: float, min_price: float) -> float:
    """Цена апгрейда по модели B + округление вверх до min_price."""
    raw = (new_limit - old_limit) * days_left * price_per_slot_per_day
    if raw <= 0:
        return 0.0
    raw = math.ceil(raw)
    if raw < min_price:
        raw = math.ceil(min_price)
    return float(raw)


async def _get_user(telegram_id: int) -> Optional[dict]:
    async with db_helpers.get_db_connection_safe() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT telegram_id, limit_ip, subscription_end_date, xui_client_uuid "
            "FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def calculate_upgrade_price(telegram_id: int, new_limit: int) -> dict:
    """
    Возвращает структуру:
        {
            'allowed': bool,
            'reason': str | None,        # причина отказа (для UI)
            'old_limit': int,
            'new_limit': int,
            'days_left': int,
            'subscription_end_date': str | None,
            'price': float,              # ₽
            'currency': 'RUB',
            'config': {...}              # snapshot настроек на момент расчёта
        }
    """
    cfg = _config_snapshot()
    result = {
        'allowed': False,
        'reason': None,
        'old_limit': 0,
        'new_limit': int(new_limit) if new_limit else 0,
        'days_left': 0,
        'subscription_end_date': None,
        'price': 0.0,
        'currency': 'RUB',
        'config': cfg,
    }

    if not cfg['enabled']:
        result['reason'] = 'feature_disabled'
        return result

    user = await _get_user(telegram_id)
    if not user:
        result['reason'] = 'user_not_found'
        return result

    old_limit = int(user.get('limit_ip') or 0)
    result['old_limit'] = old_limit

    end_dt = _parse_dt(user.get('subscription_end_date'))
    if not end_dt:
        result['reason'] = 'no_active_subscription'
        return result

    days_left = max(0, (end_dt - _now_utc()).days)
    result['days_left'] = days_left
    result['subscription_end_date'] = end_dt.astimezone(_moscow_tz()).strftime('%d.%m.%Y')

    if days_left < cfg['min_days_left']:
        result['reason'] = 'too_few_days_left'
        return result

    new_limit = int(new_limit or 0)
    if new_limit <= 0:
        result['reason'] = 'invalid_new_limit'
        return result

    if new_limit > cfg['max_limit']:
        result['reason'] = 'over_max_limit'
        return result

    # Безлимитный (0) считаем «выше любого числа»
    if old_limit == 0:
        result['reason'] = 'already_unlimited'
        return result

    if new_limit <= old_limit:
        result['reason'] = 'not_an_upgrade'
        return result

    price = _calc_price(
        old_limit=old_limit,
        new_limit=new_limit,
        days_left=days_left,
        price_per_slot_per_day=cfg['price_per_slot_per_day'],
        min_price=cfg['min_price'],
    )
    result['price'] = price
    result['allowed'] = True
    return result


async def get_available_options(telegram_id: int) -> dict:
    """
    Возвращает список доступных вариантов апгрейда для UI:
        {
            'allowed': bool,
            'reason': str | None,
            'old_limit': int,
            'days_left': int,
            'subscription_end_date': str | None,
            'options': [ {new_limit, price}, ... ]
        }
    Список лимитов формируется как: old+1, old+2, old+3, +5, +10, ..., до max_limit.
    """
    base = await calculate_upgrade_price(telegram_id, new_limit=0)  # просто чтобы получить контекст
    out = {
        'allowed': base['reason'] in (None, 'invalid_new_limit', 'not_an_upgrade'),
        'reason': base['reason'] if base['reason'] not in (None, 'invalid_new_limit', 'not_an_upgrade') else None,
        'old_limit': base['old_limit'],
        'days_left': base['days_left'],
        'subscription_end_date': base['subscription_end_date'],
        'options': [],
        'config': base['config'],
    }

    if out['reason']:
        return out

    cfg = base['config']
    old = base['old_limit']
    if old <= 0:
        out['allowed'] = False
        out['reason'] = 'already_unlimited'
        return out

    # Показываем все целые от old+1 до max_limit включительно (например, old=3, max=30 → 4..30).
    candidates = list(range(old + 1, cfg['max_limit'] + 1))
    for lim in candidates:
        priced = await calculate_upgrade_price(telegram_id, lim)
        if priced['allowed']:
            out['options'].append({'new_limit': lim, 'price': priced['price']})

    if not out['options']:
        out['allowed'] = False
        out['reason'] = 'no_options'
    return out


async def get_monthly_price_for_limit(
    new_limit: int,
    enabled_methods: Optional[set] = None,
) -> Optional[dict]:
    """Возвращает «месячный» тариф для указанного лимита устройств.

    Учитываются только RUB-тарифы: CryptoBot (USDT) и TG Stars (XTR) исключаются,
    чтобы не показать клиенту «100 ₽», когда в БД лежит «100 звёзд».

    Логика поиска (первый успешный шаг):
        1. Минимальный по цене активный тариф с limit_ip == new_limit и days в
           диапазоне 24..35 (≈ «месяц»).
        2. Fallback: минимальный по цене активный тариф с limit_ip == new_limit
           любой длительности. Длительность в ответе всегда реальная (поле days),
           поэтому клиент не введён в заблуждение — увидит честное «/ N дней».

    Args:
        new_limit: лимит устройств, для которого ищем тариф.
        enabled_methods: множество включённых RUB-методов оплаты (например
            {'yookassa', 'platega', 'wata'}). Если задано — учитываем только
            тарифы, чей payment_method универсальный ('', 'all', 'both') ИЛИ
            входит в это множество. Если None/пусто — фильтр по методу не
            применяется.

    Returns:
        {'name': str, 'days': int, 'price': float, 'traffic_gb': float} или None,
        если ничего не подошло. ``traffic_gb`` — из поля тарифа; 0 если не задано.
    """
    try:
        new_limit = int(new_limit)
    except (TypeError, ValueError):
        return None
    if new_limit <= 0:
        return None

    method_filter = ""
    method_params: list = []
    if enabled_methods:
        methods_lower = [str(m).strip().lower() for m in enabled_methods if str(m).strip()]
        if methods_lower:
            placeholders = ",".join("?" * len(methods_lower))
            method_filter = (
                "AND (LOWER(COALESCE(payment_method, '')) IN ('', 'all', 'both') "
                f"     OR LOWER(payment_method) IN ({placeholders})) "
            )
            method_params = methods_lower

    rub_filter = (
        "AND UPPER(COALESCE(currency, 'RUB')) = 'RUB' "
        "AND COALESCE(payment_method, '') NOT IN ('cryptobot', 'tgstar') "
    )

    # (sql, params) по приоритету: сначала «месячный» диапазон, потом любой тариф.
    queries = (
        (
            "SELECT name, days, price, COALESCE(traffic_gb, 0) AS traffic_gb FROM tariffs "
            "WHERE is_active = 1 AND limit_ip = ? AND days BETWEEN 24 AND 35 "
            f"{rub_filter}{method_filter}"
            "ORDER BY price ASC LIMIT 1",
            [new_limit, *method_params],
        ),
        (
            "SELECT name, days, price, COALESCE(traffic_gb, 0) AS traffic_gb FROM tariffs "
            "WHERE is_active = 1 AND limit_ip = ? "
            f"{rub_filter}{method_filter}"
            "ORDER BY price ASC LIMIT 1",
            [new_limit, *method_params],
        ),
    )

    try:
        async with db_helpers.get_db_connection_safe() as db:
            db.row_factory = aiosqlite.Row
            for sql, params in queries:
                async with db.execute(sql, tuple(params)) as cursor:
                    row = await cursor.fetchone()
                if row:
                    return {
                        'name': row['name'] or '',
                        'days': int(row['days'] or 0),
                        'price': float(row['price'] or 0),
                        'traffic_gb': float(row['traffic_gb'] or 0),
                    }
    except Exception as e:
        logger.warning(
            f"[DEVICE_UPGRADE] Не удалось получить месячный тариф для limit={new_limit}: {e}"
        )
    return None


async def apply_upgrade(
    telegram_id: int,
    new_limit: int,
    *,
    payment_id: Optional[str] = None,
    source: str = 'user_purchase',
    reason: Optional[str] = None,
) -> dict:
    """
    Применяет апгрейд лимита: обновляет users.limit_ip и пишет запись в device_limit_changes.

    Идемпотентность: если payment_id уже использовался — повторно лимит не меняется.

    Args:
        telegram_id: ID пользователя
        new_limit: Новый лимит устройств
        payment_id: ID платежа (для аудита и защиты от дубликатов)
        source: 'user_purchase' | 'admin' | 'tariff_renewal' | 'trial' | 'refund' | ...
        reason: Произвольный комментарий (показывается в админке)

    Returns:
        {'ok': bool, 'message': str, 'old_limit': int, 'new_limit': int}
    """
    try:
        async with db_helpers.get_db_connection_safe() as db:
            db.row_factory = aiosqlite.Row

            if payment_id:
                async with db.execute(
                    "SELECT id FROM device_limit_changes WHERE payment_id = ?",
                    (payment_id,),
                ) as cur:
                    existing = await cur.fetchone()
                if existing:
                    logger.info(f"[DEVICE_UPGRADE] Платёж {payment_id} уже применён ранее — пропуск")
                    return {'ok': True, 'message': 'already_applied', 'old_limit': -1, 'new_limit': new_limit}

            async with db.execute(
                "SELECT limit_ip FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return {'ok': False, 'message': 'user_not_found', 'old_limit': 0, 'new_limit': new_limit}

            old_limit = int(row['limit_ip'] or 0)
            new_limit = int(new_limit)

            if new_limit < 0:
                return {'ok': False, 'message': 'invalid_new_limit', 'old_limit': old_limit, 'new_limit': new_limit}

            await db.execute(
                "UPDATE users SET limit_ip = ? WHERE telegram_id = ?",
                (new_limit, telegram_id),
            )
            await db.execute(
                "INSERT INTO device_limit_changes (telegram_id, old_limit, new_limit, source, payment_id, reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (telegram_id, old_limit, new_limit, source, payment_id, reason),
            )
            await db.commit()

        logger.info(
            f"[DEVICE_UPGRADE] user={telegram_id} {old_limit} -> {new_limit} "
            f"source={source} payment_id={payment_id}"
        )
        return {
            'ok': True,
            'message': 'applied',
            'old_limit': old_limit,
            'new_limit': new_limit,
        }
    except Exception as e:
        logger.error(
            f"[DEVICE_UPGRADE] Ошибка применения апгрейда user={telegram_id} new_limit={new_limit}: {e}",
            exc_info=True,
        )
        return {'ok': False, 'message': str(e), 'old_limit': 0, 'new_limit': new_limit}


def build_payment_metadata(telegram_id: int, new_limit: int, calc: dict) -> dict:
    """
    Готовит payload для metadata_json платежа.
    Сохраняем достаточно данных, чтобы при успешной оплате применить апгрейд
    без повторного похода в БД (но мы всё равно перепроверим текущий limit_ip).
    """
    return {
        # Используем 'payment_type' (как в проекте: traffic_reset / traffic_renewal),
        # чтобы process_successful_payment мог распознать апгрейд.
        'payment_type': 'device_limit_upgrade',
        'telegram_user_id': telegram_id,
        'new_limit': int(new_limit),
        'old_limit': int(calc.get('old_limit') or 0),
        'days_left_at_purchase': int(calc.get('days_left') or 0),
        'price': float(calc.get('price') or 0),
        'currency': calc.get('currency', 'RUB'),
        'price_per_slot_per_day': float(calc.get('config', {}).get('price_per_slot_per_day') or 0),
    }


def parse_payment_metadata(metadata_json: str | dict | None) -> Optional[dict]:
    """
    Извлекает данные апгрейда из metadata платежа.
    Возвращает None, если это не апгрейд лимита.
    """
    if not metadata_json:
        return None
    try:
        meta = metadata_json if isinstance(metadata_json, dict) else json.loads(metadata_json)
    except Exception:
        return None
    if not isinstance(meta, dict):
        return None
    if meta.get('payment_type') != 'device_limit_upgrade':
        return None
    try:
        return {
            'telegram_id': int(meta.get('telegram_user_id') or meta.get('telegram_id') or 0),
            'new_limit': int(meta.get('new_limit') or 0),
            'old_limit': int(meta.get('old_limit') or 0),
            'price': float(meta.get('price') or 0),
        }
    except (ValueError, TypeError):
        return None
