"""Оплаченный заказ, который никуда не поехал, виден оператору.

Остальные поводы в сводке начинаются с отгрузки, и окно между «оплачен» и
«отгружен» не смотрел никто. Заказ мог стоять неделями — цену доставки не
назначили, счёт на неё не оплатили, посылку не собрали, — а узнавали об
этом от клиента, который заплатил и ждёт.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core.enums import DeliveryMethod, DeliverySpeed, OrderStatus
from core.models import Delivery, Device, Order, Subscription, User
from core.models.base import Base
from core.services import monitoring
from core.texts import fleet_digest


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_as_integer(_type, _compiler, **_kwargs) -> str:
    return "INTEGER"


async def _world():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[
                    User.__table__,
                    Order.__table__,
                    Delivery.__table__,
                    Device.__table__,
                    Subscription.__table__,
                ],
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _paid(session, *, hours_ago: int, number: str = "R-1") -> Order:
    user = User(tg_id=1, username="buyer")
    session.add(user)
    await session.flush()
    order = Order(
        public_number=number,
        user_id=user.id,
        status=OrderStatus.PAID,
        subtotal=Decimal("8900.00"),
        discount_total=Decimal("0.00"),
        delivery_price=Decimal("0.00"),
        total=Decimal("8900.00"),
        customer_name="Клиент",
        customer_phone="+79000000000",
        customer_city="Москва",
        paid_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours_ago),
    )
    session.add(order)
    await session.flush()
    return order


def _delivery(order: Order, *, quoted: bool, paid: bool) -> Delivery:
    now = dt.datetime.now(dt.UTC)
    return Delivery(
        order_id=order.id,
        method=DeliveryMethod.CDEK,
        speed=DeliverySpeed.FAST,
        city="Москва",
        address="ул. Пример, 1",
        recipient_name="Клиент",
        recipient_phone="+79000000000",
        price=Decimal("500.00") if quoted else Decimal("0.00"),
        quoted_at=now if quoted else None,
        paid_at=now if paid else None,
    )


@pytest.mark.asyncio
async def test_an_order_waiting_for_a_delivery_price_is_ours_to_move():
    engine, factory = await _world()
    try:
        async with factory() as session:
            order = await _paid(session, hours_ago=30)
            session.add(_delivery(order, quoted=False, paid=False))
            await session.commit()

            digest = await monitoring.collect(session)

        ((found, why),) = digest.unshipped
        assert found.public_number == "R-1"
        assert why == "цена доставки не назначена"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_unpaid_delivery_invoice_is_the_client_move():
    """Разные строки — разные люди: тут звонить клиенту, а не собирать посылку."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            order = await _paid(session, hours_ago=30)
            session.add(_delivery(order, quoted=True, paid=False))
            await session.commit()

            digest = await monitoring.collect(session)

        ((_, why),) = digest.unshipped
        assert why == "счёт на доставку не оплачен"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_everything_paid_means_the_parcel_is_simply_not_packed():
    engine, factory = await _world()
    try:
        async with factory() as session:
            order = await _paid(session, hours_ago=30)
            session.add(_delivery(order, quoted=True, paid=True))
            await session.commit()

            digest = await monitoring.collect(session)

        ((_, why),) = digest.unshipped
        assert why == "ждёт отгрузки"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_order_without_delivery_is_still_watched():
    """Самовывоз тоже надо отдать, и он тоже может залежаться."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _paid(session, hours_ago=30)
            await session.commit()

            digest = await monitoring.collect(session)

        ((_, why),) = digest.unshipped
        assert why == "ждёт отгрузки"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_order_paid_an_hour_ago_is_not_yet_a_reason():
    """Отправить обещаем за один-два рабочих дня — час это не опоздание."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _paid(session, hours_ago=1)
            await session.commit()

            digest = await monitoring.collect(session)

        assert digest.unshipped == []
        assert digest.is_empty
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_shipped_order_leaves_this_list():
    """Дальше за ним следят другие поводы сводки."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            order = await _paid(session, hours_ago=72)
            order.status = OrderStatus.SHIPPED
            await session.commit()

            digest = await monitoring.collect(session)

        assert digest.unshipped == []
    finally:
        await engine.dispose()


class TestHowItReads:
    def test_money_stands_above_the_hardware(self):
        """Клиент заплатил и ждёт — эту строку не должно унести вниз
        списком молчащих роутеров."""
        text = fleet_digest(
            silent=[("A0:B1:C2:D3:E4:F5", "Иван", "вчера")],
            shipped_silent=[],
            expiring=[],
            unshipped=[("R-1", "ждёт отгрузки", 2)],
        )
        assert text.index("не уехали") < text.index("Молчат больше суток")

    def test_a_quiet_day_says_nothing_about_orders(self):
        text = fleet_digest(silent=[], shipped_silent=[], expiring=[], unshipped=[])
        assert "не уехали" not in text

    def test_a_long_list_is_cut(self):
        text = fleet_digest(
            silent=[],
            shipped_silent=[],
            expiring=[],
            unshipped=[(f"R-{n}", "ждёт отгрузки", 2) for n in range(14)],
        )
        assert "…и ещё 4" in text
