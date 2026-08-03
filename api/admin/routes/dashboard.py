"""Дашборд: продажи, подписки, устройства, очереди."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.admin.auth import Principal, require_section
from api.admin.templating import render
from api.deps import get_session
from core.enums import OrderStatus, SubscriptionStatus
from core.models import Order, Subscription
from core.services import stats

router = APIRouter()


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(
    request: Request,
    principal: Principal = Depends(require_section("dashboard")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    data = await stats.collect(session)
    series = await stats.revenue_series(session, days=14)
    peak = max((amount for _, amount in series), default=0) or 1

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

    return render(
        request,
        "dashboard.html",
        principal,
        data=data,
        series=[(day, amount, int(amount / peak * 100)) for day, amount in series],
        recent_orders=recent_orders,
        expiring=expiring,
        awaiting_statuses=[OrderStatus.NEW, OrderStatus.AWAITING_PAYMENT],
    )
