"""Роутер на связи, а подписка не включилась — оператор про это узнаёт.

В прежние поводы сводки такой роутер не попадал ни одним: он не молчит,
и активированным его тоже не назвать. Автоактивация при этом откладывает
попытку молча — панель не ответила, ссылка не легла по SSH, — и пробует
снова на каждом обходе присутствия. Хоть год. Клиент видит «подписка
настраивается» на работающем роутере, деньги списаны, и узнавали мы об
этом от него.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core import texts as ru
from core.enums import OrderStatus
from core.models import Device, Order, Subscription, User
from core.models.base import Base
from core.services import monitoring


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


async def _world():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[
                    User.__table__,
                    Order.__table__,
                    Device.__table__,
                    Subscription.__table__,
                ],
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _shipped(
    session,
    *,
    shipped_hours_ago: int,
    seen_hours_ago: int | None,
    activated: bool = False,
    status: OrderStatus = OrderStatus.SHIPPED,
    number: str = "R-1",
    mac: str = "AA11BB22CC33",
):
    now = dt.datetime.now(dt.UTC)
    user = User(tg_id=300, username="waiting", first_name="Пётр")
    session.add(user)
    await session.flush()
    order = Order(
        public_number=number,
        user_id=user.id,
        status=status,
        shipped_at=now - dt.timedelta(hours=shipped_hours_ago),
        subtotal=Decimal("8900.00"),
        discount_total=Decimal("0.00"),
        delivery_price=Decimal("0.00"),
        total=Decimal("8900.00"),
        customer_name="",
        customer_phone="",
        customer_city="",
    )
    session.add(order)
    await session.flush()
    device = Device(
        mac=mac,
        order_id=order.id,
        user_id=user.id,
        frp_online=seen_hours_ago is not None,
        frp_last_seen_at=(
            None if seen_hours_ago is None else now - dt.timedelta(hours=seen_hours_ago)
        ),
        activated_at=now if activated else None,
    )
    session.add(device)
    await session.flush()
    return order, device


@pytest.mark.asyncio
async def test_a_talking_router_without_a_subscription_is_reported():
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _shipped(session, shipped_hours_ago=8, seen_hours_ago=1)
            await session.commit()

            digest = await monitoring.collect(session)

        assert len(digest.stuck) == 1
        order, device = digest.stuck[0]
        assert order.public_number == "R-1"
        # Клиента подтянули заранее: сводку пишет уже другой код.
        assert device.user is not None and device.user.display_name
        # И это не «молчит» и не «отгружен, но не включался».
        assert digest.silent == []
        assert digest.shipped_silent == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_router_just_shipped_is_given_time():
    """Туннель после включения поднимается не сразу, и первые попытки падают."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _shipped(session, shipped_hours_ago=1, seen_hours_ago=0)
            await session.commit()

            assert (await monitoring.collect(session)).stuck == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_activated_router_is_not_a_problem():
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _shipped(session, shipped_hours_ago=48, seen_hours_ago=1, activated=True)
            await session.commit()

            assert (await monitoring.collect(session)).stuck == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_router_that_never_showed_up_belongs_to_the_other_list():
    """Посылка в пути или коробка не вскрыта — это другой разговор с клиентом."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _shipped(session, shipped_hours_ago=24 * 9, seen_hours_ago=None)
            await session.commit()

            digest = await monitoring.collect(session)

        assert digest.stuck == []
        assert len(digest.shipped_silent) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_router_silent_for_days_belongs_to_the_other_list_too():
    """Выходил на связь, потом пропал — повод не «не включилась», а «молчит»."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _shipped(session, shipped_hours_ago=24 * 5, seen_hours_ago=72)
            await session.commit()

            assert (await monitoring.collect(session)).stuck == []
    finally:
        await engine.dispose()


class TestTheOperatorCanReadIt:
    def test_the_block_names_the_order_and_the_router(self):
        text = ru.fleet_digest(
            silent=[],
            shipped_silent=[],
            expiring=[],
            stuck=[("R-1", "AA11BB22CC33", "Пётр")],
        )
        assert "не включилась" in text
        assert "R-1" in text and "AA11BB22CC33" in text and "Пётр" in text

    def test_a_client_without_a_name_leaves_no_dangling_dot(self):
        text = ru.fleet_digest(
            silent=[], shipped_silent=[], expiring=[], stuck=[("R-2", "DD44EE55FF66", "")]
        )
        assert "· \n" not in text and not text.rstrip().endswith("·")

    def test_the_old_call_still_works(self):
        """Сводку зовут и без этого повода — падать на нём нельзя."""
        assert ru.fleet_digest(silent=[], shipped_silent=[], expiring=[])

    def test_a_long_list_is_cut(self):
        many = [(f"R-{i}", "AA11BB22CC33", "") for i in range(25)]
        text = ru.fleet_digest(silent=[], shipped_silent=[], expiring=[], stuck=many)
        assert "…и ещё 15" in text
