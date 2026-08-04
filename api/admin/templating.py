"""Шаблоны админки: окружение Jinja2 и фильтры отображения."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from api.admin.auth import CSRF_FIELD, Principal
from core.config import settings
from core.dates import days_left, format_date_ru, format_datetime_ru, to_display
from core.enums import DeviceStatus, OrderStatus, PaymentStatus, SubscriptionStatus

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

STATUS_LABELS: dict[str, str] = {
    OrderStatus.NEW: "новый",
    OrderStatus.AWAITING_PAYMENT: "ждёт оплаты",
    OrderStatus.PAID: "оплачен",
    OrderStatus.PACKING: "сборка",
    OrderStatus.SHIPPED: "отправлен",
    OrderStatus.DELIVERED: "доставлен",
    OrderStatus.DONE: "закрыт",
    OrderStatus.CANCELLED: "отменён",
    OrderStatus.REFUNDED: "возврат",
    SubscriptionStatus.PENDING: "ждёт активации",
    SubscriptionStatus.ACTIVE: "активна",
    SubscriptionStatus.GRACE: "льготный период",
    SubscriptionStatus.EXPIRED: "истекла",
    SubscriptionStatus.CANCELLED: "отменена",
    PaymentStatus.PENDING: "ожидание",
    PaymentStatus.SUCCEEDED: "оплачен",
    PaymentStatus.CANCELED: "отменён",
    PaymentStatus.FAILED: "ошибка",
    PaymentStatus.REFUNDED: "возврат",
    DeviceStatus.NEW: "на складе",
    DeviceStatus.ASSIGNED: "отгружено",
    DeviceStatus.ACTIVE: "активно",
    DeviceStatus.REVOKED: "отвязано",
    DeviceStatus.BLOCKED: "заблокировано",
}

STATUS_TONES: dict[str, str] = {
    OrderStatus.NEW: "info",
    OrderStatus.AWAITING_PAYMENT: "warn",
    OrderStatus.PAID: "ok",
    OrderStatus.PACKING: "info",
    OrderStatus.SHIPPED: "info",
    OrderStatus.DELIVERED: "ok",
    OrderStatus.DONE: "muted",
    OrderStatus.CANCELLED: "muted",
    OrderStatus.REFUNDED: "bad",
    SubscriptionStatus.ACTIVE: "ok",
    SubscriptionStatus.GRACE: "warn",
    SubscriptionStatus.EXPIRED: "bad",
    SubscriptionStatus.PENDING: "info",
    SubscriptionStatus.CANCELLED: "muted",
    PaymentStatus.SUCCEEDED: "ok",
    PaymentStatus.PENDING: "warn",
    PaymentStatus.FAILED: "bad",
    PaymentStatus.CANCELED: "muted",
    PaymentStatus.REFUNDED: "bad",
    DeviceStatus.ACTIVE: "ok",
    DeviceStatus.BLOCKED: "bad",
    DeviceStatus.REVOKED: "muted",
    DeviceStatus.NEW: "info",
    DeviceStatus.ASSIGNED: "warn",
}


def money(value: Decimal | int | float | None) -> str:
    if value is None:
        return "—"
    amount = Decimal(str(value))
    if amount == amount.to_integral_value():
        return f"{amount:,.0f} ₽".replace(",", " ")
    return f"{amount:,.2f} ₽".replace(",", " ").replace(".", ",")


def date_ru(value: dt.datetime | None) -> str:
    return format_date_ru(value) if value else "—"


def datetime_ru(value: dt.datetime | None) -> str:
    return format_datetime_ru(value) if value else "—"


def short_datetime(value: dt.datetime | None) -> str:
    return f"{to_display(value):%d.%m %H:%M}" if value else "—"


def status_label(value: Any) -> str:
    return STATUS_LABELS.get(value, str(value) if value is not None else "—")


def status_tone(value: Any) -> str:
    return STATUS_TONES.get(value, "muted")


def days_until(value: dt.datetime | None) -> str:
    if value is None:
        return "—"
    remaining = days_left(value)
    if remaining < 0:
        return f"просрочено на {abs(remaining)}"
    return str(remaining)


def bytes_human(value: int | float | None) -> str:
    """Трафик в привычных единицах: точные байты в таблице всё равно не читают."""
    if not value:
        return "0"
    amount = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if amount < 1024 or unit == "ТБ":
            # Десятая доля важна только на мелких числах: «1,5 ГБ», но «128 ГБ».
            precision = 0 if unit == "Б" or amount >= 10 else 1
            return f"{amount:.{precision}f} {unit}".replace(".", ",")
        amount /= 1024
    return f"{amount:.0f} ТБ"  # недостижимо: цикл всегда возвращает на ТБ


def phone_pretty(value: str | None) -> str:
    if not value:
        return "—"
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 11:
        return f"+{digits[0]} {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
    return value


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters.update(
    {
        "money": money,
        "date_ru": date_ru,
        "datetime_ru": datetime_ru,
        "short_dt": short_datetime,
        "status_label": status_label,
        "status_tone": status_tone,
        "days_until": days_until,
        "phone": phone_pretty,
        "bytes_human": bytes_human,
    }
)
templates.env.globals.update(
    {
        "brand": settings.app.brand,
        "csrf_field": CSRF_FIELD,
    }
)


def render(
    request: Request,
    template: str,
    principal: Principal | None = None,
    **context: Any,
) -> HTMLResponse:
    payload: dict[str, Any] = {
        "request": request,
        "principal": principal,
        "csrf_token": principal.session.csrf if principal else "",
        "current_path": request.url.path,
    }
    payload.update(context)
    return templates.TemplateResponse(request, template, payload)
