"""Работа с датами: в БД и коде — UTC, пользователю показываем Europe/Moscow."""

from __future__ import annotations

import calendar
import datetime as dt
from zoneinfo import ZoneInfo

from core.config import settings

DISPLAY_TZ = ZoneInfo(settings.app.timezone)

MONTHS_RU_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def ensure_utc(value: dt.datetime) -> dt.datetime:
    """Наивную дату считаем UTC (так приходит из БД при отключённой tz-осведомлённости)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def to_display(value: dt.datetime) -> dt.datetime:
    return ensure_utc(value).astimezone(DISPLAY_TZ)


def add_months(value: dt.datetime, months: int) -> dt.datetime:
    """Календарное сложение месяцев.

    31 января + 1 месяц = 28/29 февраля — то, чего ожидает клиент,
    и то, что не даёт подписке «уехать» на несколько дней за год.
    """
    if months == 0:
        return value
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def add_period(start: dt.datetime, *, months: int = 0, days: int = 0) -> dt.datetime:
    return add_months(start, months) + dt.timedelta(days=days)


def days_left(expires_at: dt.datetime, *, now: dt.datetime | None = None) -> int:
    """Полных суток до окончания. Отрицательное значение — подписка просрочена."""
    delta = ensure_utc(expires_at) - ensure_utc(now or utcnow())
    return delta // dt.timedelta(days=1)


def format_date_ru(value: dt.datetime) -> str:
    local = to_display(value)
    return f"{local.day} {MONTHS_RU_GENITIVE[local.month - 1]} {local.year}"


def format_datetime_ru(value: dt.datetime) -> str:
    local = to_display(value)
    return f"{format_date_ru(value)}, {local:%H:%M}"


def plural_ru(number: int, one: str, few: str, many: str) -> str:
    """`3` -> «3 дня». Используется в текстах бота и уведомлениях."""
    number = abs(number)
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many


def days_phrase(number: int) -> str:
    return f"{number} {plural_ru(number, 'день', 'дня', 'дней')}"


def ago_phrase(value: dt.datetime, *, now: dt.datetime | None = None) -> str:
    """«7 минут назад».

    Когда речь о свежести показаний роутера, точная дата ничего не говорит:
    важно, минуты прошли или третьи сутки.
    """
    seconds = int((ensure_utc(now or utcnow()) - ensure_utc(value)).total_seconds())
    if seconds < 60:
        return "только что"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} {plural_ru(minutes, 'минуту', 'минуты', 'минут')} назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} {plural_ru(hours, 'час', 'часа', 'часов')} назад"
    days = hours // 24
    if days < 30:
        return f"{days_phrase(days)} назад"
    return format_date_ru(value)


def uptime_phrase(seconds: int) -> str:
    """Непрерывная работа устройства. Точность до минут нужна только в первый час."""
    if seconds <= 0:
        return "—"
    days = seconds // 86400
    if days:
        return days_phrase(days)
    hours = seconds // 3600
    if hours:
        return f"{hours} {plural_ru(hours, 'час', 'часа', 'часов')}"
    minutes = max(seconds // 60, 1)
    return f"{minutes} {plural_ru(minutes, 'минута', 'минуты', 'минут')}"
