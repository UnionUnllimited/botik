"""Расчёт дат подписки: активация, продление, grace, бонусные дни."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from core.enums import SubscriptionEventType, SubscriptionStatus
from core.models import Plan, Subscription
from core.services import subscriptions as service

NOW = dt.datetime(2026, 8, 3, 12, tzinfo=dt.UTC)


@pytest.fixture
def plan_month() -> Plan:
    return Plan(id=1, slug="m1", title="1 месяц", months=1, price=Decimal("399.00"))


@pytest.fixture
def plan_year() -> Plan:
    return Plan(id=4, slug="m12", title="12 месяцев", months=12, extra_days=30, price=Decimal("3490.00"))


def make_subscription(**kwargs) -> Subscription:
    defaults = {"user_id": 1, "status": SubscriptionStatus.PENDING}
    defaults.update(kwargs)
    subscription = Subscription(**defaults)
    subscription.events = []
    return subscription


class TestActivation:
    def test_starts_counting_from_activation_not_payment(self, plan_month):
        """Дни доставки не сгорают: отсчёт идёт с момента активации."""
        subscription = make_subscription()
        service.activate(subscription, plan=plan_month, device_id=7, now=NOW)

        assert subscription.status is SubscriptionStatus.ACTIVE
        assert subscription.started_at == NOW
        assert subscription.expires_at == dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)
        assert subscription.device_id == 7

    def test_grace_added_after_expiry(self, plan_month):
        subscription = make_subscription()
        service.activate(subscription, plan=plan_month, now=NOW)
        assert subscription.grace_until == subscription.expires_at + dt.timedelta(days=3)

    def test_year_plan_with_bonus_days(self, plan_year):
        subscription = make_subscription()
        service.activate(subscription, plan=plan_year, now=NOW)
        assert subscription.expires_at == dt.datetime(2027, 9, 2, 12, tzinfo=dt.UTC)

    def test_activation_is_recorded(self, plan_month):
        subscription = make_subscription()
        service.activate(subscription, plan=plan_month, now=NOW)
        assert subscription.events[-1].event is SubscriptionEventType.ACTIVATED


class TestExtension:
    def test_active_subscription_extends_from_expiry(self, plan_month):
        """Продление активной прибавляется к текущей дате, а не к сегодня."""
        subscription = make_subscription(
            status=SubscriptionStatus.ACTIVE,
            started_at=NOW - dt.timedelta(days=20),
            expires_at=dt.datetime(2026, 8, 20, 12, tzinfo=dt.UTC),
        )
        service.extend(subscription, plan=plan_month, now=NOW)
        assert subscription.expires_at == dt.datetime(2026, 9, 20, 12, tzinfo=dt.UTC)

    def test_expired_subscription_extends_from_today(self, plan_month):
        subscription = make_subscription(
            status=SubscriptionStatus.EXPIRED,
            expires_at=dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC),
        )
        service.extend(subscription, plan=plan_month, now=NOW)
        assert subscription.expires_at == dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)
        assert subscription.status is SubscriptionStatus.ACTIVE

    def test_grace_subscription_extends_from_original_expiry(self, plan_month):
        """В grace-периоде оплаченные дни не теряются."""
        subscription = make_subscription(
            status=SubscriptionStatus.GRACE,
            expires_at=NOW - dt.timedelta(days=1),
            grace_until=NOW + dt.timedelta(days=2),
        )
        service.extend(subscription, plan=plan_month, now=NOW)
        assert subscription.expires_at == dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)
        assert subscription.status is SubscriptionStatus.ACTIVE

    def test_pending_subscription_does_not_start_counting(self, plan_month):
        subscription = make_subscription(status=SubscriptionStatus.PENDING)
        service.extend(subscription, plan=plan_month, now=NOW)
        assert subscription.expires_at is None
        assert subscription.status is SubscriptionStatus.PENDING

    def test_extension_resets_reminder_marker(self, plan_month):
        subscription = make_subscription(
            status=SubscriptionStatus.ACTIVE,
            expires_at=NOW + dt.timedelta(days=1),
            last_reminder_day=1,
        )
        service.extend(subscription, plan=plan_month, now=NOW)
        assert subscription.last_reminder_day is None

    def test_extension_is_recorded_with_delta(self, plan_month):
        subscription = make_subscription(
            status=SubscriptionStatus.ACTIVE, expires_at=NOW + dt.timedelta(days=10)
        )
        service.extend(subscription, plan=plan_month, now=NOW)
        event = subscription.events[-1]
        assert event.event is SubscriptionEventType.RENEWED
        assert event.days_delta == 31  # с 13 августа по 13 сентября


class TestStatusRefresh:
    def test_active_while_not_expired(self):
        subscription = make_subscription(
            status=SubscriptionStatus.ACTIVE, expires_at=NOW + dt.timedelta(days=1)
        )
        assert service.refresh_status(subscription, now=NOW) is SubscriptionStatus.ACTIVE

    def test_grace_after_expiry(self):
        subscription = make_subscription(
            status=SubscriptionStatus.ACTIVE,
            expires_at=NOW - dt.timedelta(hours=1),
            grace_until=NOW + dt.timedelta(days=2),
        )
        assert service.refresh_status(subscription, now=NOW) is SubscriptionStatus.GRACE

    def test_expired_after_grace(self):
        subscription = make_subscription(
            status=SubscriptionStatus.GRACE,
            expires_at=NOW - dt.timedelta(days=5),
            grace_until=NOW - dt.timedelta(days=2),
        )
        assert service.refresh_status(subscription, now=NOW) is SubscriptionStatus.EXPIRED

    def test_pending_is_not_touched(self):
        subscription = make_subscription(status=SubscriptionStatus.PENDING)
        assert service.refresh_status(subscription, now=NOW) is SubscriptionStatus.PENDING


class TestBonusDays:
    def test_days_added_to_active(self):
        subscription = make_subscription(
            status=SubscriptionStatus.ACTIVE, expires_at=NOW + dt.timedelta(days=10)
        )
        service.add_days(subscription, 14, now=NOW)
        assert subscription.expires_at == NOW + dt.timedelta(days=24)

    def test_days_revive_expired_subscription(self):
        subscription = make_subscription(
            status=SubscriptionStatus.EXPIRED,
            expires_at=NOW - dt.timedelta(days=5),
            grace_until=NOW - dt.timedelta(days=2),
        )
        service.add_days(subscription, 30, now=NOW)
        assert subscription.status is SubscriptionStatus.ACTIVE
        assert subscription.expires_at == NOW + dt.timedelta(days=30)

    def test_days_for_expired_count_from_today_not_from_past(self):
        """Иначе бонус за аварию частично сгорел бы в прошлом."""
        subscription = make_subscription(
            status=SubscriptionStatus.EXPIRED, expires_at=NOW - dt.timedelta(days=100)
        )
        service.add_days(subscription, 3, now=NOW)
        assert subscription.expires_at == NOW + dt.timedelta(days=3)


class TestServing:
    def test_active_serves_nodes(self):
        subscription = make_subscription(
            status=SubscriptionStatus.ACTIVE,
            expires_at=NOW + dt.timedelta(days=1),
            grace_until=NOW + dt.timedelta(days=4),
        )
        assert subscription.is_serving(now=NOW) is True

    def test_grace_still_serves(self):
        subscription = make_subscription(
            status=SubscriptionStatus.GRACE,
            expires_at=NOW - dt.timedelta(days=1),
            grace_until=NOW + dt.timedelta(days=2),
        )
        assert subscription.is_serving(now=NOW) is True

    def test_expired_does_not_serve(self):
        subscription = make_subscription(
            status=SubscriptionStatus.EXPIRED,
            expires_at=NOW - dt.timedelta(days=10),
            grace_until=NOW - dt.timedelta(days=7),
        )
        assert subscription.is_serving(now=NOW) is False
