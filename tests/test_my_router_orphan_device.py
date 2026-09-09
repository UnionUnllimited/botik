"""«Мой роутер» видит роутер, привязанный к заказу клиента, но ещё ни к кому.

Привязка «к заказу» не всегда проставляет клиента, и экран отвечал «роутер
за вами пока не числится» рядом с заказом «✓ Роутер работает» — так это
и выглядело в боте у первого покупателя. Чужой роутер по своему заказу при
этом показывать нельзя: его могли передать другому клиенту по гарантии.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes.catalog_api import my_router
from core.enums import OrderStatus
from core.models import Device, Order, User
from core.models.base import Base

KARL = 8269414826


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


def _order(user: User, number: str) -> Order:
    return Order(
        public_number=number,
        user=user,
        status=OrderStatus.PAID,
        subtotal=Decimal("6900.00"),
        discount_total=Decimal("0.00"),
        delivery_price=Decimal("0.00"),
        total=Decimal("6900.00"),
        customer_name="",
        customer_phone="",
        customer_city="",
    )


async def _engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine


@pytest.mark.asyncio
async def test_router_bound_to_the_order_but_to_nobody_is_shown():
    engine = await _engine()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            karl = User(tg_id=KARL, username="karl")
            order = _order(karl, "R-260803-0001")
            session.add(order)
            await session.flush()
            session.add(Device(mac="AA:BB:CC:DD:EE:01", order_id=order.id, user_id=None))
            await session.commit()

            data = await my_router(tg_id=KARL, device_id=0, session=session)

        assert data["router"]["mac"] == "AA:BB:CC:DD:EE:01"
        assert [r["mac"] for r in data["routers"]] == ["AA:BB:CC:DD:EE:01"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_router_handed_to_another_client_stays_hidden():
    """Роутер по своему заказу, но уже чужой — не показываем: клиент увидел бы
    показания и срок устройства, которое стоит у другого человека."""
    engine = await _engine()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            karl = User(tg_id=KARL, username="karl")
            other = User(tg_id=1, username="other")
            order = _order(karl, "R-260803-0001")
            session.add_all([order, other])
            await session.flush()
            session.add(Device(mac="AA:BB:CC:DD:EE:02", order_id=order.id, user_id=other.id))
            await session.commit()

            data = await my_router(tg_id=KARL, device_id=0, session=session)

        assert data["router"] is None
        assert data["routers"] == []
        # Заказ при этом виден: он клиента, и его состояние ему интересно.
        assert data["order"]["number"] == "R-260803-0001"
    finally:
        await engine.dispose()
