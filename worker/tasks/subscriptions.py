"""Жизненный цикл подписок: статусы, напоминания, сгорание неактивированных."""

from __future__ import annotations

import datetime as dt

import structlog
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from core import texts as ru
from core.config import settings
from core.dates import days_left, days_phrase, utcnow
from core.db import session_scope
from core.enums import SubscriptionStatus
from core.models import Subscription, User
from core.notifications import send_message
from core.services import subscriptions as subscription_service

log = structlog.get_logger("worker.subscriptions")


async def refresh_statuses() -> int:
    """Переводит подписки active → grace → expired по датам."""
    now = utcnow()
    changed = 0
    async with session_scope() as session:
        rows = await session.scalars(
            select(Subscription).where(
                Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE]),
                Subscription.expires_at.is_not(None),
                Subscription.expires_at < now,
            )
        )
        for subscription in rows:
            before = subscription.status
            after = subscription_service.refresh_status(subscription, now=now)
            if after is not before:
                changed += 1
    if changed:
        log.info("subscriptions.statuses_refreshed", count=changed)
    return changed


async def send_reminders() -> int:
    """Напоминания за 7/3/1/0 дней до конца и на 1-й и 3-й день после."""
    now = utcnow()
    sent = 0
    before_days = list(settings.subscription.reminder_days_before)
    after_days = list(settings.subscription.reminder_days_after)

    async with session_scope() as session:
        rows = await session.scalars(
            select(Subscription)
            .where(
                Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE]),
                Subscription.expires_at.is_not(None),
            )
            .options(selectinload(Subscription.plan))
        )
        for subscription in rows:
            remaining = days_left(subscription.expires_at, now=now)
            marker: int | None = None
            text: str | None = None

            if remaining in before_days:
                marker = remaining
                text = (
                    ru.REMINDER_LAST_DAY
                    if remaining == 0
                    else ru.REMINDER_BEFORE.format(days=days_phrase(remaining))
                )
            elif -remaining in after_days and remaining < 0:
                marker = remaining
                grace_left = max(
                    days_left(subscription.grace_until, now=now) if subscription.grace_until else 0,
                    0,
                )
                text = ru.REMINDER_AFTER.format(days=days_phrase(-remaining), grace=days_phrase(grace_left))

            if text is None or marker is None:
                continue
            if subscription.last_reminder_day == marker:
                continue

            user = await session.get(User, subscription.user_id)
            if user is None or user.bot_blocked or user.is_blocked:
                subscription.last_reminder_day = marker
                continue

            # Кнопки нет намеренно: кабинет на сайте удалён вместе с сайтом,
            # а продление живёт в самом боте — клиент уже в нужном чате.
            delivered = await send_message(
                user.tg_id,
                text,
                session=session,
                kind="reminder",
            )
            subscription.last_reminder_day = marker
            sent += int(delivered)

    if sent:
        log.info("subscriptions.reminders_sent", count=sent)
    return sent


async def expire_unactivated() -> int:
    """Оплаченная, но так и не активированная подписка сгорает по дедлайну."""
    now = utcnow()
    count = 0
    async with session_scope() as session:
        rows = await session.scalars(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.PENDING,
                Subscription.pending_expires_at.is_not(None),
                Subscription.pending_expires_at < now,
            )
        )
        for subscription in rows:
            subscription.status = SubscriptionStatus.EXPIRED
            subscription.cancelled_at = now
            count += 1
    if count:
        log.info("subscriptions.unactivated_expired", count=count)
    return count


def next_reminder_run(now: dt.datetime | None = None) -> dt.datetime:
    """Ближайшие 10:00 по Москве — время, когда шлём напоминания."""
    moment = now or utcnow()
    target = moment.replace(hour=7, minute=0, second=0, microsecond=0)  # 10:00 MSK = 07:00 UTC
    if target <= moment:
        target += dt.timedelta(days=1)
    return target
