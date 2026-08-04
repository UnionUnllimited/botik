"""Дашборд: продажи, подписки, устройства, очереди."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.admin.auth import Principal, require_section
from api.admin.templating import render
from api.deps import get_session
from core.config import settings
from core.enums import OrderStatus, SubscriptionStatus
from core.models import Order, Subscription
from core.services import stats

router = APIRouter()

CHART_WIDTH = 720
CHART_HEIGHT = 150
CHART_GAP = 6
SERIES_DAYS = 14


def _chart(series: list[tuple[dt.date, Decimal]]) -> tuple[list[dict[str, Any]], Decimal]:
    """Готовит столбцы для inline-SVG.

    Считаем координаты здесь, а не в шаблоне: арифметика в Jinja читается
    заметно хуже, а результат один и тот же.
    """
    peak = max((amount for _, amount in series), default=Decimal("0"))
    scale = float(peak) or 1.0
    slot = CHART_WIDTH / (len(series) or 1)
    width = max(slot - CHART_GAP, 3.0)

    bars: list[dict[str, Any]] = []
    for index, (day, amount) in enumerate(series):
        height = float(amount) / scale * (CHART_HEIGHT - 12)
        bars.append(
            {
                "x": round(index * slot + (slot - width) / 2, 2),
                "y": round(CHART_HEIGHT - max(height, 2.0), 2),
                "width": round(width, 2),
                "height": round(max(height, 2.0), 2),
                "day": day,
                "amount": amount,
                "empty": amount == 0,
            }
        )
    return bars, peak


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(
    request: Request,
    principal: Principal = Depends(require_section("dashboard")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    data = await stats.collect(session)
    series = await stats.revenue_series(session, days=SERIES_DAYS)
    bars, peak = _chart(series)

    recent_orders = list(
        await session.scalars(
            select(Order).order_by(Order.id.desc()).limit(8).options(selectinload(Order.user))
        )
    )
    expiring = list(
        await session.scalars(
            select(Subscription)
            .where(Subscription.status == SubscriptionStatus.ACTIVE)
            .order_by(Subscription.expires_at.asc().nulls_last())
            .limit(8)
            .options(selectinload(Subscription.user), selectinload(Subscription.plan))
        )
    )

    # Состояние интеграций читаем из конфигурации, а не опросом по сети:
    # дашборд открывают часто, и он не должен ждать чужие таймауты.
    integrations = [
        ("Оплата PLATEGA", settings.platega.enabled, "PLATEGA_MERCHANT_ID и PLATEGA_SECRET"),
        ("Туннели к роутерам", settings.frp.is_configured, ", ".join(settings.frp.missing_keys)),
        ("Панель Remnawave", settings.remnawave.is_configured, ", ".join(settings.remnawave.missing_keys)),
    ]

    return render(
        request,
        "dashboard.html",
        principal,
        data=data,
        bars=bars,
        peak=peak,
        series_days=SERIES_DAYS,
        chart_width=CHART_WIDTH,
        chart_height=CHART_HEIGHT,
        recent_orders=recent_orders,
        expiring=expiring,
        integrations=integrations,
        awaiting_statuses=[OrderStatus.NEW, OrderStatus.AWAITING_PAYMENT],
    )
