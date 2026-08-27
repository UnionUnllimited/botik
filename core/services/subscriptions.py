"""Подписки: создание после оплаты, продление, активация, истечение.

Ключевое правило: оплаченная подписка создаётся в статусе `pending` и не
тратит дни, пока клиент не активировал роутер. Продление активной подписки
прибавляется к текущей дате окончания, истёкшей — считается от сегодня.
"""

from __future__ import annotations

import datetime as dt

import structlog
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.config import settings
from core.dates import add_period, ensure_utc, utcnow
from core.enums import SubscriptionEventType, SubscriptionStatus
from core.models import Plan, Subscription, SubscriptionEvent

log = structlog.get_logger("services.subscriptions")

LIVE_STATUSES = (SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE)


def _record(
    subscription: Subscription,
    event: SubscriptionEventType,
    *,
    old_expires_at: dt.datetime | None,
    days_delta: int = 0,
    payment_id: int | None = None,
    admin_id: int | None = None,
    comment: str | None = None,
) -> SubscriptionEvent:
    entry = SubscriptionEvent(
        subscription=subscription,
        event=event,
        days_delta=days_delta,
        old_expires_at=old_expires_at,
        new_expires_at=subscription.expires_at,
        payment_id=payment_id,
        admin_id=admin_id,
        comment=comment,
    )
    # Связь уже установлена конструктором, и этого достаточно, чтобы событие
    # сохранилось каскадом. Дописываем в коллекцию только когда она уже в памяти:
    # у подписки, поднятой из базы без selectinload, обращение к `events`
    # запускает ленивую загрузку, а в async-сессии она падает MissingGreenlet.
    if "events" not in sa_inspect(subscription).unloaded:
        subscription.events.append(entry)
    return entry


async def get_active(session: AsyncSession, user_id: int) -> Subscription | None:
    """Действующая подписка пользователя (включая grace-период)."""
    return await session.scalar(
        select(Subscription)
        .where(Subscription.user_id == user_id, Subscription.status.in_(LIVE_STATUSES))
        .order_by(Subscription.expires_at.desc().nulls_last())
        .limit(1)
        .options(selectinload(Subscription.plan), selectinload(Subscription.device))
    )


async def get_pending(session: AsyncSession, user_id: int) -> Subscription | None:
    """Оплаченная, но ещё не активированная подписка."""
    return await session.scalar(
        select(Subscription)
        .where(Subscription.user_id == user_id, Subscription.status == SubscriptionStatus.PENDING)
        .order_by(Subscription.id.asc())
        .limit(1)
        .options(selectinload(Subscription.plan))
    )


async def get_for_device(session: AsyncSession, device_id: int) -> Subscription | None:
    """Подписка **этого** роутера. Одна подписка — один роутер.

    Именно так надо спрашивать везде, где речь про устройство: `get_current`
    отвечает про клиента, и у владельца двух роутеров отдаёт одну и ту же
    подписку обоим. Второй роутер от этого выглядит оплаченным, хотя доступа
    на нём нет.
    """
    return await session.scalar(
        select(Subscription)
        .where(Subscription.device_id == device_id, Subscription.status.in_(LIVE_STATUSES))
        .order_by(Subscription.expires_at.desc().nulls_last())
        .limit(1)
        .options(selectinload(Subscription.plan), selectinload(Subscription.device))
    )


async def grant_manual(
    session: AsyncSession,
    *,
    user_id: int,
    device_id: int,
    days: int,
    now: dt.datetime | None = None,
) -> Subscription:
    """Подписка ручной активации: срок без тарифа, привязанная к роутеру.

    Ручная активация заводит доступ в панели, и до сих пор этим всё
    и заканчивалось: в наших таблицах не появлялось ничего, и парк писал
    «подписка: нет» у роутера, который в эту секунду работает. Оператор видел
    противоречие и не мог понять, прошла активация или нет.

    Тарифа здесь нет и быть не может — роутер служебный, подменный или
    проданный вне сайта, — поэтому `plan_id` пуст, а срок ставится днями.
    Повторная активация того же роутера продлевает ту же запись, а не заводит
    вторую: одна подписка на роутер, и это правило не должен нарушать даже
    двойной клик.
    """
    moment = now or utcnow()
    subscription = await get_for_device(session, device_id)
    old_expires_at = subscription.expires_at if subscription else None

    if subscription is None:
        subscription = Subscription(
            user_id=user_id,
            device_id=device_id,
            status=SubscriptionStatus.ACTIVE,
            started_at=moment,
            source="manual",
        )
        session.add(subscription)

    subscription.user_id = user_id
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.started_at = subscription.started_at or moment
    subscription.expires_at = moment + dt.timedelta(days=days)
    subscription.grace_until = subscription.expires_at + dt.timedelta(
        days=settings.subscription.grace_days
    )
    subscription.pending_expires_at = None
    subscription.last_reminder_day = None
    subscription.cancelled_at = None

    await session.flush()
    _record(
        subscription,
        SubscriptionEventType.ACTIVATED,
        old_expires_at=old_expires_at,
        days_delta=days,
        comment=f"Ручная активация на {days} дн.",
    )
    log.info(
        "subscription.granted_manually",
        subscription_id=subscription.id,
        device_id=device_id,
        days=days,
    )
    return subscription


async def get_current(session: AsyncSession, user_id: int) -> Subscription | None:
    """Что показывать клиенту: сначала действующая, иначе ожидающая активации."""
    return await get_active(session, user_id) or await get_pending(session, user_id)


async def create_pending(
    session: AsyncSession,
    *,
    user_id: int,
    plan: Plan,
    order_id: int | None = None,
    payment_id: int | None = None,
    source: str = "order",
    now: dt.datetime | None = None,
) -> Subscription:
    """Подписка оплачена — ждёт активации роутера, срок ещё не идёт."""
    moment = now or utcnow()
    subscription = Subscription(
        user_id=user_id,
        plan_id=plan.id,
        order_id=order_id,
        status=SubscriptionStatus.PENDING,
        pending_expires_at=moment + dt.timedelta(days=settings.subscription.activation_deadline_days),
        source=source,
    )
    session.add(subscription)
    _record(
        subscription,
        SubscriptionEventType.CREATED,
        old_expires_at=None,
        payment_id=payment_id,
        comment=f"Тариф {plan.title}",
    )
    log.info("subscription.created_pending", user_id=user_id, plan_id=plan.id, order_id=order_id)
    return subscription


def activate(
    subscription: Subscription,
    *,
    plan: Plan,
    device_id: int | None = None,
    now: dt.datetime | None = None,
) -> Subscription:
    """Старт отсчёта в момент активации устройства."""
    moment = now or utcnow()
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.started_at = moment
    subscription.expires_at = plan.apply_to(moment)
    subscription.grace_until = subscription.expires_at + dt.timedelta(days=settings.subscription.grace_days)
    subscription.pending_expires_at = None
    subscription.last_reminder_day = None
    if device_id is not None:
        subscription.device_id = device_id
    _record(
        subscription,
        SubscriptionEventType.ACTIVATED,
        old_expires_at=None,
        comment=f"Активация, тариф {plan.title}",
    )
    log.info(
        "subscription.activated",
        subscription_id=subscription.id,
        device_id=device_id,
        expires_at=subscription.expires_at.isoformat() if subscription.expires_at else None,
    )
    return subscription


def extend(
    subscription: Subscription,
    *,
    plan: Plan,
    payment_id: int | None = None,
    admin_id: int | None = None,
    now: dt.datetime | None = None,
) -> Subscription:
    """Продление: к текущей дате окончания, если она в будущем, иначе от сегодня."""
    moment = now or utcnow()
    old_expires_at = subscription.expires_at

    if subscription.status is SubscriptionStatus.PENDING:
        # Ещё не активирована — просто складываем оплаченные периоды.
        subscription.pending_expires_at = max(
            subscription.pending_expires_at or moment,
            moment + dt.timedelta(days=settings.subscription.activation_deadline_days),
        )
        _record(
            subscription,
            SubscriptionEventType.EXTENDED,
            old_expires_at=old_expires_at,
            payment_id=payment_id,
            admin_id=admin_id,
            comment=f"Оплачен ещё один период: {plan.title}",
        )
        return subscription

    base = ensure_utc(old_expires_at) if old_expires_at and ensure_utc(old_expires_at) > moment else moment
    subscription.expires_at = plan.apply_to(base)
    subscription.grace_until = subscription.expires_at + dt.timedelta(days=settings.subscription.grace_days)
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.started_at = subscription.started_at or moment
    subscription.last_reminder_day = None
    subscription.plan_id = plan.id

    delta_days = (subscription.expires_at - base).days
    _record(
        subscription,
        SubscriptionEventType.RENEWED,
        old_expires_at=old_expires_at,
        days_delta=delta_days,
        payment_id=payment_id,
        admin_id=admin_id,
        comment=f"Продление: {plan.title}",
    )
    log.info(
        "subscription.extended",
        subscription_id=subscription.id,
        days=delta_days,
        expires_at=subscription.expires_at.isoformat(),
    )
    return subscription


def add_days(
    subscription: Subscription,
    days: int,
    *,
    event: SubscriptionEventType = SubscriptionEventType.BONUS,
    admin_id: int | None = None,
    comment: str | None = None,
    now: dt.datetime | None = None,
) -> Subscription:
    """Бонусные дни: реферальные, компенсация за аварию, ручная правка админа."""
    moment = now or utcnow()
    old_expires_at = subscription.expires_at

    if subscription.expires_at is None:
        # Подписка ещё не активирована — дни лягут при активации через бонус пользователя.
        _record(subscription, event, old_expires_at=None, days_delta=days, admin_id=admin_id, comment=comment)
        return subscription

    base = ensure_utc(subscription.expires_at)
    subscription.expires_at = max(base, moment) + dt.timedelta(days=days)
    subscription.grace_until = subscription.expires_at + dt.timedelta(days=settings.subscription.grace_days)
    if days > 0 and subscription.status in (SubscriptionStatus.EXPIRED, SubscriptionStatus.GRACE):
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.last_reminder_day = None
    _record(
        subscription,
        event,
        old_expires_at=old_expires_at,
        days_delta=days,
        admin_id=admin_id,
        comment=comment,
    )
    return subscription


def refresh_status(subscription: Subscription, *, now: dt.datetime | None = None) -> SubscriptionStatus:
    """Пересчитывает статус по датам: active → grace → expired."""
    moment = now or utcnow()
    if subscription.status in (SubscriptionStatus.PENDING, SubscriptionStatus.CANCELLED):
        return subscription.status
    if subscription.expires_at is None:
        return subscription.status

    expires_at = ensure_utc(subscription.expires_at)
    grace_until = ensure_utc(subscription.grace_until) if subscription.grace_until else expires_at

    previous = subscription.status
    if moment <= expires_at:
        subscription.status = SubscriptionStatus.ACTIVE
    elif moment <= grace_until:
        subscription.status = SubscriptionStatus.GRACE
    else:
        subscription.status = SubscriptionStatus.EXPIRED

    if subscription.status is not previous:
        event = (
            SubscriptionEventType.GRACE_STARTED
            if subscription.status is SubscriptionStatus.GRACE
            else SubscriptionEventType.EXPIRED
        )
        if subscription.status is not SubscriptionStatus.ACTIVE:
            _record(subscription, event, old_expires_at=subscription.expires_at)
        log.info(
            "subscription.status_refreshed",
            subscription_id=subscription.id,
            old=str(previous),
            new=str(subscription.status),
        )
    return subscription.status


def period_end_for(plan: Plan, *, start: dt.datetime | None = None) -> dt.datetime:
    """Когда закончится подписка, если купить этот тариф сейчас."""
    return add_period(start or utcnow(), months=plan.months, days=plan.extra_days)
