"""Агрегаты для дашборда админки.

Все запросы — одним проходом по индексам, без выгрузки строк в питон:
дашборд открывают часто, и он не должен нагружать базу.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.dates import utcnow
from core.enums import (
    OrderStatus,
    PaymentStatus,
    SubscriptionStatus,
    TicketStatus,
)
from core.models import Device, Order, Payment, Subscription, Ticket, User

PAID_STATUSES = (
    OrderStatus.PAID,
    OrderStatus.PACKING,
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
    OrderStatus.DONE,
)


@dataclass(slots=True)
class PeriodStats:
    orders: int = 0
    paid_orders: int = 0
    revenue: Decimal = Decimal("0.00")
    new_users: int = 0

    @property
    def conversion(self) -> float:
        """Доля заказов, дошедших до оплаты."""
        return round(self.paid_orders / self.orders * 100, 1) if self.orders else 0.0

    @property
    def average_check(self) -> Decimal:
        if not self.paid_orders:
            return Decimal("0.00")
        return (self.revenue / self.paid_orders).quantize(Decimal("0.01"))


@dataclass(slots=True)
class Dashboard:
    day: PeriodStats = field(default_factory=PeriodStats)
    week: PeriodStats = field(default_factory=PeriodStats)
    month: PeriodStats = field(default_factory=PeriodStats)

    users_total: int = 0
    users_blocked_bot: int = 0

    devices_total: int = 0
    devices_online: int = 0
    devices_active: int = 0

    subscriptions_active: int = 0
    subscriptions_grace: int = 0
    subscriptions_pending: int = 0
    subscriptions_expired: int = 0
    expiring_7d: int = 0

    mrr: Decimal = Decimal("0.00")
    orders_awaiting: int = 0
    orders_to_ship: int = 0
    tickets_open: int = 0


async def _period(session: AsyncSession, since: dt.datetime) -> PeriodStats:
    stats = PeriodStats()
    stats.orders = (
        await session.scalar(select(func.count()).select_from(Order).where(Order.created_at >= since)) or 0
    )
    stats.paid_orders = (
        await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.created_at >= since, Order.status.in_(PAID_STATUSES))
        )
        or 0
    )
    stats.revenue = Decimal(
        str(
            await session.scalar(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.paid_at >= since, Payment.status == PaymentStatus.SUCCEEDED
                )
            )
            or 0
        )
    )
    stats.new_users = (
        await session.scalar(select(func.count()).select_from(User).where(User.created_at >= since)) or 0
    )
    return stats


async def collect(session: AsyncSession) -> Dashboard:
    now = utcnow()
    data = Dashboard()

    data.day = await _period(session, now - dt.timedelta(days=1))
    data.week = await _period(session, now - dt.timedelta(days=7))
    data.month = await _period(session, now - dt.timedelta(days=30))

    data.users_total = await session.scalar(select(func.count()).select_from(User)) or 0
    data.users_blocked_bot = (
        await session.scalar(select(func.count()).select_from(User).where(User.bot_blocked.is_(True))) or 0
    )

    online_since = now - dt.timedelta(minutes=settings.subscription.heartbeat_offline_min)
    data.devices_total = await session.scalar(select(func.count()).select_from(Device)) or 0
    data.devices_online = (
        await session.scalar(
            select(func.count()).select_from(Device).where(Device.last_heartbeat_at >= online_since)
        )
        or 0
    )
    data.devices_active = (
        await session.scalar(select(func.count()).select_from(Device).where(Device.activated_at.is_not(None)))
        or 0
    )

    counts = dict(
        (await session.execute(select(Subscription.status, func.count()).group_by(Subscription.status))).all()
    )
    data.subscriptions_active = counts.get(SubscriptionStatus.ACTIVE, 0)
    data.subscriptions_grace = counts.get(SubscriptionStatus.GRACE, 0)
    data.subscriptions_pending = counts.get(SubscriptionStatus.PENDING, 0)
    data.subscriptions_expired = counts.get(SubscriptionStatus.EXPIRED, 0)

    data.expiring_7d = (
        await session.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at > now,
                Subscription.expires_at <= now + dt.timedelta(days=7),
            )
        )
        or 0
    )

    # MRR: сумма активных подписок, приведённая к месяцу по цене тарифа.
    from core.models import Plan

    rows = await session.execute(
        select(Plan.price, Plan.months, func.count(Subscription.id))
        .join(Subscription, Subscription.plan_id == Plan.id)
        .where(Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE]))
        .group_by(Plan.price, Plan.months)
    )
    mrr = Decimal("0.00")
    for price, months, count in rows:
        if months:
            mrr += (Decimal(str(price)) / months) * count
    data.mrr = mrr.quantize(Decimal("0.01"))

    data.orders_awaiting = (
        await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.status.in_([OrderStatus.NEW, OrderStatus.AWAITING_PAYMENT]))
        )
        or 0
    )
    data.orders_to_ship = (
        await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.status.in_([OrderStatus.PAID, OrderStatus.PACKING]))
        )
        or 0
    )
    data.tickets_open = (
        await session.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS]))
        )
        or 0
    )
    return data


async def revenue_series(session: AsyncSession, *, days: int = 14) -> list[tuple[dt.date, Decimal]]:
    """Выручка по дням — для простого графика на дашборде."""
    since = utcnow() - dt.timedelta(days=days)
    rows = await session.execute(
        select(
            func.date_trunc("day", Payment.paid_at).label("day"),
            func.coalesce(func.sum(Payment.amount), 0),
        )
        .where(Payment.paid_at >= since, Payment.status == PaymentStatus.SUCCEEDED)
        .group_by("day")
        .order_by("day")
    )
    return [(row[0].date(), Decimal(str(row[1]))) for row in rows]
