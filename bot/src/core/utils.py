"""
Общие вспомогательные функции.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytz

from app_config import app_conf

_MOSCOW = pytz.timezone('Europe/Moscow')

_MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


# ── Даты ─────────────────────────────────────────────────────────────────────

def format_msk_date(dt: Optional[datetime], fmt: str = '%d.%m.%Y %H:%M %Z') -> str:
    """
    Переводит datetime в московское время и возвращает отформатированную строку.
    Если dt без tzinfo — считается UTC.
    """
    if dt is None:
        return "Неизвестно"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_MOSCOW).strftime(fmt)


def format_msk_date_long(dt: Optional[datetime]) -> str:
    """
    Возвращает дату в формате «1 января 2025 г. 15:30 MSK».
    Используется в сообщениях о пробном периоде и успешной оплате.
    """
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(_MOSCOW)
    month_name = _MONTHS_RU.get(local.month, "")
    return f"{local.day} {month_name} {local.year} г. {local.strftime('%H:%M %Z')}"


def format_msk_date_day(dt: Optional[datetime]) -> str:
    """
    Возвращает только дату в формате «1 января 2025 г.» (без времени).
    Используется в главном меню подписки.
    """
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(_MOSCOW)
    month_name = _MONTHS_RU.get(local.month, "")
    return f"{local.day} {month_name} {local.year} г."


# ── Трафик ───────────────────────────────────────────────────────────────────

def _traffic_emoji(used_bytes: int, limit_bytes: int) -> str:
    if limit_bytes <= 0:
        return "📊"
    pct = used_bytes / limit_bytes * 100
    return "📉" if pct < 50 else "📊" if pct < 90 else "📈"


def format_traffic_inline(used_bytes: int, limit_bytes: int) -> str:
    """
    Однострочное представление трафика для главного меню.
    Пример: «1.50 / 10.00 GB 📊» или «1.50 GB (безлимит)»
    """
    used_gb = used_bytes / (1024 ** 3)
    if limit_bytes > 0:
        limit_gb = limit_bytes / (1024 ** 3)
        emoji = _traffic_emoji(used_bytes, limit_bytes)
        return f"{used_gb:.2f} / {limit_gb:.2f} GB {emoji}"
    return f"{used_gb:.2f} GB (безлимит)"


def format_traffic_section(used_bytes: int, limit_bytes: int) -> str:
    """
    Полный блок трафика для экранов выбора сброса/продления трафика.
    Пример:
        📊 <b>Текущий трафик:</b>
        Использовано: 1.50 / 10.00 GB 📊
        Осталось: 8.50 GB
    """
    used_gb = used_bytes / (1024 ** 3)
    if limit_bytes > 0:
        limit_gb = limit_bytes / (1024 ** 3)
        emoji = _traffic_emoji(used_bytes, limit_bytes)
        return (
            f"\n\n📊 <b>Текущий трафик:</b>\n"
            f"Использовано: {used_gb:.2f} / {limit_gb:.2f} GB {emoji}\n"
            f"Осталось: {limit_gb - used_gb:.2f} GB"
        )
    return (
        f"\n\n📊 <b>Текущий трафик:</b>\n"
        f"Использовано: {used_gb:.2f} GB (безлимит)"
    )


# ── Настройки ─────────────────────────────────────────────────────────────────

def parse_admin_ids(raw=None) -> list[int]:
    """
    Разбирает admin_ids из settings: JSON-массив, список, «123,456» или одно число.
    """
    import json

    if raw is None:
        raw = app_conf.get('admin_ids', '')

    if isinstance(raw, list):
        out: list[int] = []
        for item in raw:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
        return out

    s = str(raw or '').strip()
    if not s:
        return []

    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return parse_admin_ids(parsed)
        if isinstance(parsed, int):
            return [parsed]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    return [int(x.strip()) for x in s.split(',') if x.strip().isdigit()]


def is_bot_admin(user_id: int) -> bool:
    return user_id in parse_admin_ids()


def get_default_limit_gb() -> int:
    """
    Читает и парсит настройку remnawave_default_traffic_limit_gb из app_conf.
    Возвращает 0 если не задана или не число.
    """
    raw = app_conf.get('remnawave_default_traffic_limit_gb', 0)
    try:
        return int(raw) if raw else 0
    except (ValueError, TypeError):
        return 0
