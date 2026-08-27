"""Расчёт дат подписки: активация, продление, grace, бонусные дни."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

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

    def test_access_ends_on_the_paid_date(self, plan_month):
        """Льготных дней нет — решение заказчика от 21 августа 2026.

        Так было и на деле: в панель кладётся дата окончания, и доступ
        обрывался ровно в срок. Обещание льготы висело только в тексте
        напоминания, и его убрали.
        """
        subscription = make_subscription()
        service.activate(subscription, plan=plan_month, now=NOW)
        assert subscription.grace_until == subscription.expires_at

    def test_year_plan_with_bonus_days(self, plan_year):
        subscription = make_subscription()
        service.activate(subscription, plan=plan_year, now=NOW)
        assert subscription.expires_at == dt.datetime(2027, 9, 2, 12, tzinfo=dt.UTC)

    def test_activation_is_recorded(self, plan_month):
        subscription = make_subscription()
        service.activate(subscription, plan=plan_month, now=NOW)
        assert subscription.events[-1].event is SubscriptionEventType.ACTIVATED

    def test_event_is_linked_to_subscription_itself(self, plan_month):
        """Событие держится связью, а не только присутствием в коллекции.

        На этом стоит защита от MissingGreenlet: подписку из базы поднимают
        без `events`, и обращение к коллекции запускало бы ленивую загрузку
        посреди async-сессии. Каскад сохранит событие и без неё.
        """
        subscription = make_subscription()
        service.activate(subscription, plan=plan_month, now=NOW)
        assert subscription.events[-1].subscription is subscription


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


class TestOneSubscriptionPerRouter:
    """Одна подписка — один роутер. Правило заказчика, повторённое трижды.

    Ломалось оно двумя способами сразу: парк спрашивал подписку **клиента**
    (и у владельца двух роутеров показывал её обоим), а ручная активация
    не заводила подписку вовсе — доступ в панели был, а в строке стояло «нет».
    """

    SOURCE = (
        Path(__file__).resolve().parents[1] / "api/routes/fleet_api.py"
    ).read_text(encoding="utf-8")

    ACTIVATION = (
        Path(__file__).resolve().parents[1] / "core/services/activation.py"
    ).read_text(encoding="utf-8")

    def test_fleet_asks_by_device_not_by_client(self):
        """Оба экрана роутера — список и карточка — берут подписку по устройству.

        `get_current` отвечает про клиента; он остался там, где речь и правда
        о клиенте (его экран, привязка), но роутеру он отдаёт чужой срок.
        """
        assert self.SOURCE.count("get_for_device(session, device.id)") == 2, (
            "и список, и карточка роутера должны спрашивать подписку по устройству"
        )

    def test_manual_activation_grants_a_subscription(self):
        body = self.ACTIVATION[self.ACTIVATION.index("async def activate_manually("):]
        body = body[: body.find("\nasync def ", 1)]
        assert "grant_manual" in body, (
            "ручная активация обязана заводить подписку на этот роутер, "
            "иначе парк пишет «нет» у работающего роутера"
        )

    @pytest.mark.asyncio
    async def test_grant_manual_reuses_the_routers_own_subscription(self, monkeypatch):
        """Повторная активация продлевает ту же запись, а не заводит вторую."""

        existing = Subscription(
            id=7,
            user_id=1,
            device_id=42,
            status=SubscriptionStatus.ACTIVE,
            expires_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        )
        added: list = []

        class _Session:
            def add(self, item):
                added.append(item)

            async def flush(self):
                return None

        async def _found(_session, device_id):
            assert device_id == 42
            return existing

        monkeypatch.setattr(service, "get_for_device", _found)
        monkeypatch.setattr(service, "_record", lambda *a, **k: None)

        result = await service.grant_manual(_Session(), user_id=1, device_id=42, days=30)

        assert result is existing
        assert added == [], "завелась вторая подписка на тот же роутер"
        assert result.device_id == 42


class TestReleaseOnUnbind:
    """Отвязка клиента снимает подписку с роутера.

    Раньше это делал только сброс на склад, и после «Отвязать клиента»
    подписка оставалась висеть на роутере без владельца: парк показывал
    у него «активна», а настоящий роутер того же клиента — «нет».
    Это и была та самая «активна на другом роутере».
    """

    SOURCE = (
        Path(__file__).resolve().parents[1] / "api/routes/fleet_api.py"
    ).read_text(encoding="utf-8")

    def test_unbind_releases_the_subscription(self):
        body = self.SOURCE[self.SOURCE.index("async def _unbind("):]
        body = body[: body.index("\n\n\n")]
        assert "release_device" in body, (
            "отвязка обязана снимать подписку с роутера, иначе она зависает "
            "на устройстве без клиента"
        )

    def test_reset_uses_the_same_release(self):
        """Два пути не должны расходиться в том, что делают с подпиской."""
        assert self.SOURCE.count("release_device(session, device.id)") == 2

    @pytest.mark.asyncio
    async def test_paid_subscription_goes_back_to_waiting(self, monkeypatch):
        """Клиент за неё заплатил — сжигать вместе с железом нельзя."""
        paid = Subscription(
            id=1, user_id=1, device_id=42, plan_id=5,
            status=SubscriptionStatus.ACTIVE,
            expires_at=dt.datetime(2026, 9, 26, tzinfo=dt.UTC),
        )
        session = _SessionReturning(paid)

        assert await service.release_device(session, 42) == "pending"
        assert paid.device_id is None
        assert paid.status is SubscriptionStatus.PENDING
        assert paid.expires_at is None
        assert paid.pending_expires_at is not None

    @pytest.mark.asyncio
    async def test_manual_subscription_is_cancelled(self, monkeypatch):
        """Без тарифа её нечем активировать заново — в ожидании она зависнет навсегда."""
        manual = Subscription(
            id=2, user_id=1, device_id=42, plan_id=None,
            status=SubscriptionStatus.ACTIVE, source="manual",
            expires_at=dt.datetime(2026, 9, 26, tzinfo=dt.UTC),
        )
        session = _SessionReturning(manual)

        assert await service.release_device(session, 42) == "cancelled"
        assert manual.device_id is None
        assert manual.status is SubscriptionStatus.CANCELLED
        assert manual.cancelled_at is not None

    @pytest.mark.asyncio
    async def test_router_without_a_subscription_is_fine(self):
        assert await service.release_device(_SessionReturning(None), 42) == ""


class _SessionReturning:
    """Сессия, отдающая одну заранее выбранную подписку."""

    def __init__(self, subscription):
        self._subscription = subscription

    async def scalar(self, _statement):
        return self._subscription
