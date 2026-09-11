"""Закрытый заказ с работающим роутером не проходит молча.

«Возврат» и «Отменён» меняют только статус заказа — и правильно: роутер
едет назад не мгновенно, а отбирать доступ в день, когда вернули деньги,
рано. Но подписка идёт до конца оплаченного срока, и клиент с возвращёнными
деньгами продолжает пользоваться сервисом, пока кто-нибудь не вспомнит
сбросить роутер на склад. Вспоминать было не по чему.

Сбрасываем не мы: это решение человека, и кнопка у него есть. Наше дело —
сказать.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes import catalog_api
from core.enums import DeviceStatus, OrderStatus
from core.models import Device, Order, User
from core.models.base import Base


@compiles(JSONB, "sqlite")
def _jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "INTEGER"


async def _world():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[User.__table__, Order.__table__, Device.__table__]
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _order(session, status: OrderStatus) -> tuple[Order, User]:
    user = User(tg_id=1, username="buyer")
    session.add(user)
    await session.flush()
    order = Order(
        public_number="R-1",
        user_id=user.id,
        status=status,
        subtotal=Decimal("8900.00"),
        discount_total=Decimal("0.00"),
        delivery_price=Decimal("0.00"),
        total=Decimal("8900.00"),
        customer_name="Клиент",
        customer_phone="+79000000000",
        customer_city="Москва",
        paid_at=dt.datetime.now(dt.UTC),
    )
    session.add(order)
    await session.flush()
    return order, user


async def _router(session, order: Order, *, owner_id: int | None) -> Device:
    device = Device(
        mac="A0:B1:C2:D3:E4:F5",
        order_id=order.id,
        user_id=owner_id,
        status=DeviceStatus.ACTIVE,
        activated_at=dt.datetime.now(dt.UTC),
    )
    session.add(device)
    await session.flush()
    return device


@pytest.mark.asyncio
async def test_a_refund_says_the_router_is_still_working():
    engine, factory = await _world()
    try:
        async with factory() as session:
            order, user = await _order(session, OrderStatus.REFUNDED)
            await _router(session, order, owner_id=user.id)
            await session.commit()

            note = await catalog_api._router_still_running(session, order)

        assert "A0:B1:C2:D3:E4:F5" in note
        assert "на склад" in note
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_cancelled_order_gets_the_same_warning():
    """Отмена после отгрузки — то же самое: роутер уже уехал."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            order, user = await _order(session, OrderStatus.CANCELLED)
            await _router(session, order, owner_id=user.id)
            await session.commit()

            assert "на склад" in await catalog_api._router_still_running(session, order)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_router_already_returned_to_the_shelf_is_not_mentioned():
    """Оператор уже сбросил его — повторять нечего."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            order, _ = await _order(session, OrderStatus.REFUNDED)
            await _router(session, order, owner_id=None)
            await session.commit()

            assert await catalog_api._router_still_running(session, order) == ""
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_order_without_a_router_says_nothing():
    engine, factory = await _world()
    try:
        async with factory() as session:
            order, _ = await _order(session, OrderStatus.REFUNDED)
            await session.commit()

            assert await catalog_api._router_still_running(session, order) == ""
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_live_order_is_not_this_warning_business():
    """Отгруженный заказ с работающим роутером — это норма, а не повод."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            order, user = await _order(session, OrderStatus.SHIPPED)
            await _router(session, order, owner_id=user.id)
            await session.commit()

            assert await catalog_api._router_still_running(session, order) == ""
    finally:
        await engine.dispose()


def test_the_warning_reaches_the_topic_card():
    """Оператор смотрит в карточку заказа, а не в журнал."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "api" / "routes" / "catalog_api.py"
    ).read_text(encoding="utf-8")
    start = source.index("async def manage_order_status(")
    body = source[start : source.index("\n@router.", start + 1)]
    assert "_router_still_running" in body
    assert "order_topics.push(session, order, note=note)" in body
