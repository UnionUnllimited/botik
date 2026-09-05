"""Снимок платежей для зеркала в базе бота.

Проверяется не «ручка отвечает», а договор с их таблицей: имена статусов,
тип платежа и число дней — ровно те, по которым их карточка клиента и
страница платежей раскрашивают строки. Разъедутся — и у клиента снова
«Платежей 0» при оплаченном заказе, только теперь с зеркалом, которое
«работает».
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes.catalog_api import _since, payments_snapshot
from core.enums import OrderStatus, PaymentProviderName, PaymentPurpose, PaymentStatus
from core.models import Order, Payment, Plan, User
from core.models.base import Base


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


T0 = dt.datetime(2026, 9, 1, 12, 0, 0)
T1 = dt.datetime(2026, 9, 2, 12, 0, 0)
T2 = dt.datetime(2026, 9, 3, 12, 0, 0)


async def _engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[User.__table__, Order.__table__, Plan.__table__, Payment.__table__]
            )
        )
    return engine


def _payment(user, *, key, purpose, status, amount, at, order=None, plan=None,
             provider=PaymentProviderName.PLATEGA):
    return Payment(
        user=user,
        order=order,
        plan_id=plan.id if plan else None,
        provider=provider,
        purpose=purpose,
        status=status,
        idempotency_key=key,
        amount=Decimal(amount),
        currency="RUB",
        description="",
        created_at=at,
        updated_at=at,
        paid_at=at if status is PaymentStatus.SUCCEEDED else None,
    )


@pytest.mark.asyncio
async def test_order_payment_lands_in_their_markup():
    """Оплата роутера: у неё заказ, а не тариф, и их страница должна
    отличать её от подписки, а не рисовать «—» в колонке тарифа."""
    engine = await _engine()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            user = User(tg_id=8152081864, username="kelvin")
            order = Order(
                public_number="R-260830-0012", user=user, status=OrderStatus.PAID,
                subtotal=Decimal("122.99"), discount_total=Decimal("0"),
                delivery_price=Decimal("0"), total=Decimal("122.99"),
                customer_name="", customer_phone="", customer_city="",
            )
            session.add(_payment(user, key="k1", purpose=PaymentPurpose.ORDER,
                                 status=PaymentStatus.SUCCEEDED, amount="122.99",
                                 at=T0, order=order))
            await session.commit()

            data = await payments_snapshot(since="", limit=500, session=session)

        row = data["payments"][0]
        assert row["payment_id"].startswith("SHOP_")
        assert row["tg_id"] == 8152081864
        assert row["amount"] == "122.99"
        assert row["status"] == "succeeded"
        assert row["metadata"]["payment_type"] == "router_order"
        assert row["metadata"]["order_number"] == "R-260830-0012"
        assert row["metadata"]["payment_method"] == "Platega"
        assert row["metadata"]["source"] == "shop"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_subscription_payment_carries_days_and_their_tariff_id():
    """Продление — их родной случай: типа не получает, а число дней и номер
    их тарифа кладём рядом, чтобы карточка показала название срока."""
    engine = await _engine()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            plan = Plan(slug="tariff-42", title="90 дней", months=3, extra_days=0,
                        price=Decimal("900"))
            session.add(plan)
            await session.flush()
            user = User(tg_id=614685408, username="union")
            session.add(_payment(user, key="k2", purpose=PaymentPurpose.SUBSCRIPTION,
                                 status=PaymentStatus.PENDING, amount="900",
                                 at=T0, plan=plan))
            await session.commit()

            data = await payments_snapshot(since="", limit=500, session=session)

        meta = data["payments"][0]["metadata"]
        assert "payment_type" not in meta
        assert meta["subscription_days"] == 90
        assert meta["tariff_id"] == 42
        assert data["payments"][0]["status"] == "pending"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cursor_returns_only_what_changed_since():
    """Бот забирает изменения с прошлого круга, а не всю историю каждый раз.

    Смена статуса «ожидает → оплачен» обязана доехать так же, как новый
    платёж: иначе заказ навсегда останется в их таблице неоплаченным.
    """
    engine = await _engine()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            user = User(tg_id=1, username="a")
            session.add_all([
                _payment(user, key="old", purpose=PaymentPurpose.ORDER,
                         status=PaymentStatus.SUCCEEDED, amount="1", at=T0),
                _payment(user, key="fresh", purpose=PaymentPurpose.DELIVERY,
                         status=PaymentStatus.SUCCEEDED, amount="2", at=T2),
            ])
            await session.commit()

            everything = await payments_snapshot(since="", limit=500, session=session)
            later = await payments_snapshot(since=T1.isoformat(), limit=500, session=session)

        assert len(everything["payments"]) == 2
        assert [p["amount"] for p in later["payments"]] == ["2"]
        assert later["payments"][0]["metadata"]["payment_type"] == "delivery"
        assert everything["next_since"].startswith("2026-09-03")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_clients_without_telegram_are_not_offered():
    """В их таблице клиент — это telegram_id. Платёж без него положить некуда."""
    engine = await _engine()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            ghost = User(tg_id=None, username="ghost")
            session.add(_payment(ghost, key="k3", purpose=PaymentPurpose.ORDER,
                                 status=PaymentStatus.SUCCEEDED, amount="5", at=T0))
            await session.commit()

            data = await payments_snapshot(since="", limit=500, session=session)

        assert data["payments"] == []
        assert data["next_since"] == ""
    finally:
        await engine.dispose()


@pytest.mark.parametrize("raw", ["", "not a date", "2026-13-45"])
def test_broken_cursor_means_from_the_beginning(raw):
    """Снимок зовёт фоновый круг: упасть на кривом курсоре значит остановить зеркало."""
    assert _since(raw) is None


def test_naive_cursor_is_read_as_utc():
    assert _since("2026-09-02T12:00:00").tzinfo is not None
