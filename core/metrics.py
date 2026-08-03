"""Prometheus-метрики. Один модуль на все процессы: bot, api, worker."""

from __future__ import annotations

import datetime as dt
import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.enums import OrderStatus, PaymentStatus, SubscriptionStatus
from core.models import Device, Order, Payment, Subscription

METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST

# --- события ---------------------------------------------------------------
bot_updates_total = Counter("bot_updates_total", "Обработано апдейтов Telegram", ["type"])
bot_errors_total = Counter("bot_errors_total", "Ошибки обработки апдейтов", ["kind"])
api_requests_total = Counter("api_requests_total", "Запросы к API", ["method", "path", "status"])
api_request_seconds = Histogram(
    "api_request_seconds",
    "Время обработки запроса API",
    ["method", "path"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
device_heartbeats_total = Counter("device_heartbeats_total", "Принято heartbeat от устройств")
device_activations_total = Counter("device_activations_total", "Попытки активации устройств", ["result"])
subscription_fetch_total = Counter("subscription_fetch_total", "Обращения роутеров за подпиской", ["outcome"])
payments_total = Counter("payments_total", "Платежи", ["provider", "status"])
broadcast_messages_total = Counter("broadcast_messages_total", "Сообщения рассылок", ["status"])
worker_job_seconds = Histogram("worker_job_seconds", "Время выполнения фоновых задач", ["job"])
worker_job_errors_total = Counter("worker_job_errors_total", "Ошибки фоновых задач", ["job"])

# --- состояние системы -----------------------------------------------------
devices_online = Gauge("devices_online", "Устройств на связи")
devices_total = Gauge("devices_total", "Устройств всего", ["status"])
subscriptions_gauge = Gauge("subscriptions", "Подписки по статусам", ["status"])
subscriptions_expiring = Gauge("subscriptions_expiring_7d", "Подписки, истекающие за 7 дней")
orders_today = Gauge("orders_today", "Заказы за сутки", ["status"])
revenue_today = Gauge("revenue_today", "Выручка по успешным платежам за сутки")

_GAUGE_TTL_SEC = 30
_last_refresh = 0.0


async def refresh_business_gauges(session: AsyncSession, *, force: bool = False) -> None:
    """Обновляет бизнес-метрики из БД. Кэш на 30 секунд — скрейп не должен грузить базу."""
    global _last_refresh  # noqa: PLW0603
    now = time.monotonic()
    if not force and now - _last_refresh < _GAUGE_TTL_SEC:
        return
    _last_refresh = now

    utc_now = dt.datetime.now(dt.UTC)
    online_since = utc_now - dt.timedelta(minutes=settings.subscription.heartbeat_offline_min)
    day_ago = utc_now - dt.timedelta(days=1)

    online = await session.scalar(
        select(func.count()).select_from(Device).where(Device.last_heartbeat_at >= online_since)
    )
    devices_online.set(online or 0)

    rows = await session.execute(select(Device.status, func.count()).group_by(Device.status))
    devices_total.clear()  # сбрасываем лейблы, которых больше нет
    for status, count in rows:
        devices_total.labels(status=str(status)).set(count)

    rows = await session.execute(select(Subscription.status, func.count()).group_by(Subscription.status))
    subscriptions_gauge.clear()
    for status, count in rows:
        subscriptions_gauge.labels(status=str(status)).set(count)

    expiring = await session.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expires_at <= utc_now + dt.timedelta(days=7),
            Subscription.expires_at > utc_now,
        )
    )
    subscriptions_expiring.set(expiring or 0)

    rows = await session.execute(
        select(Order.status, func.count()).where(Order.created_at >= day_ago).group_by(Order.status)
    )
    orders_today.clear()
    for status, count in rows:
        orders_today.labels(status=str(status)).set(count)
    for status in OrderStatus:
        orders_today.labels(status=str(status))

    revenue = await session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.paid_at >= day_ago,
            Payment.status == PaymentStatus.SUCCEEDED,
        )
    )
    revenue_today.set(float(revenue or 0))


def render_metrics() -> bytes:
    return generate_latest()
