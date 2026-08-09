"""Проверки схемы БД, которые важны для бизнес-логики и не должны «уехать»."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import Numeric

from core.enums import SubscriptionStatus
from core.models import Base, Device, Plan, Subscription, User


def test_all_required_tables_declared():
    """Список из ТЗ (п. 7) — минимум, который обязан быть в схеме."""
    required = {
        "users",
        "admin_users",
        "products",
        "plans",
        "orders",
        "order_items",
        "payments",
        "deliveries",
        "devices",
        "device_commands",
        "subscriptions",
        "subscription_events",
        "nodes",
        "node_groups",
        "node_assignments",
        "activation_codes",
        "promo_codes",
        "promo_usages",
        "referrals",
        "tickets",
        "ticket_messages",
        "articles",
        "broadcasts",
        "broadcast_targets",
        "settings",
        "audit_log",
        "heartbeats",
    }
    assert required <= set(Base.metadata.tables)


def test_money_columns_are_numeric_12_2():
    """Деньги — только Numeric(12,2): никаких float в схеме."""
    money_columns = [
        ("products", "price"),
        ("plans", "price"),
        ("orders", "total"),
        ("orders", "subtotal"),
        ("orders", "delivery_price"),
        ("order_items", "unit_price"),
        ("payments", "amount"),
        ("deliveries", "price"),
    ]
    for table_name, column_name in money_columns:
        column = Base.metadata.tables[table_name].columns[column_name]
        assert isinstance(column.type, Numeric), f"{table_name}.{column_name}"
        assert (column.type.precision, column.type.scale) == (12, 2), f"{table_name}.{column_name}"


def test_timestamps_are_timezone_aware():
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if column.type.__class__.__name__ == "DateTime":
                assert column.type.timezone is True, f"{table.name}.{column.name}"


def test_critical_indexes_exist():
    devices = Base.metadata.tables["devices"]
    assert devices.columns["mac"].unique is True

    payments = Base.metadata.tables["payments"]
    assert payments.columns["provider_payment_id"].unique is True

    subscription_indexes = {
        tuple(col.name for col in index.columns) for index in Base.metadata.tables["subscriptions"].indexes
    }
    assert ("expires_at",) in subscription_indexes

    order_indexes = {
        tuple(col.name for col in index.columns) for index in Base.metadata.tables["orders"].indexes
    }
    assert ("user_id",) in order_indexes


def test_site_client_needs_no_telegram():
    """Клиент с сайта заводится без tg_id, а почта остаётся уникальной.

    Обратно tg_id в NOT NULL вернуть нельзя: такой клиент перестанет создаваться.
    """
    users = Base.metadata.tables["users"]
    assert users.columns["tg_id"].nullable is True
    assert users.columns["email"].unique is True
    assert users.columns["email"].nullable is True, "у клиентов из бота почты нет"


def test_no_forbidden_term_in_schema():
    """В именах таблиц и колонок запрещённой регламентом лексики быть не должно."""
    forbidden = "".join(("v", "p", "n"))  # само слово в репозитории не пишем
    for table in Base.metadata.tables.values():
        assert forbidden not in table.name.lower()
        for column in table.columns:
            assert forbidden not in column.name.lower()


class TestModelHelpers:
    def test_plan_period_uses_calendar_months(self):
        plan = Plan(slug="m12", title="12 месяцев", months=12, extra_days=30, price=Decimal("100.00"))
        start = dt.datetime(2026, 8, 3, 12, tzinfo=dt.UTC)
        assert plan.apply_to(start) == dt.datetime(2027, 9, 2, 12, tzinfo=dt.UTC)

    def test_plan_price_per_month(self):
        plan = Plan(slug="m3", title="3 месяца", months=3, price=Decimal("1500.00"))
        assert plan.price_per_month == Decimal("500.00")

    def test_display_name_falls_back_to_email(self):
        """У клиента с сайта нет ни имени из Telegram, ни username — остаётся почта."""
        assert User(email="client@example.com").display_name == "client@example.com"
        assert User(tg_id=42, username="ivan").display_name == "@ivan"
        assert User(first_name="Иван", last_name="Петров").display_name == "Иван Петров"

    def test_device_online_threshold(self):
        now = dt.datetime(2026, 8, 3, 12, tzinfo=dt.UTC)
        device = Device(mac="A0:B1:C2:D3:E4:F5")
        assert device.is_online(threshold_min=15, now=now) is False

        device.last_heartbeat_at = now - dt.timedelta(minutes=10)
        assert device.is_online(threshold_min=15, now=now) is True

        device.last_heartbeat_at = now - dt.timedelta(minutes=16)
        assert device.is_online(threshold_min=15, now=now) is False

    def test_subscription_serving_includes_grace(self):
        now = dt.datetime(2026, 8, 3, 12, tzinfo=dt.UTC)
        subscription = Subscription(
            user_id=1,
            status=SubscriptionStatus.GRACE,
            expires_at=now - dt.timedelta(days=1),
            grace_until=now + dt.timedelta(days=2),
        )
        assert subscription.is_serving(now=now) is True

        subscription.grace_until = now - dt.timedelta(minutes=1)
        assert subscription.is_serving(now=now) is False

        subscription.status = SubscriptionStatus.EXPIRED
        subscription.grace_until = now + dt.timedelta(days=2)
        assert subscription.is_serving(now=now) is False

    def test_pending_subscription_does_not_serve(self):
        now = dt.datetime(2026, 8, 3, 12, tzinfo=dt.UTC)
        subscription = Subscription(user_id=1, status=SubscriptionStatus.PENDING)
        assert subscription.is_serving(now=now) is False
