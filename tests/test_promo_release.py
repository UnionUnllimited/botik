"""Отменённый заказ возвращает промокод.

Применение записывается при оформлении, а не при оплате: иначе предел
«столько-то раз» обходился бы десятком неоплаченных заказов сразу. Но
брошенный заказ забирал код навсегда — клиент видел «вы уже использовали
этот промокод», не заплатив ни разу, а код на сто применений выгорал
корзинами, которые никто не оплатил.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core.enums import OrderStatus, PromoDiscountType
from core.models import Order, PromoCode, PromoUsage, User
from core.models.base import Base
from core.services import promo as promo_service


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
                    User.__table__, Order.__table__, PromoCode.__table__, PromoUsage.__table__,
                ],
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _order(session, user: User, number: str) -> Order:
    order = Order(
        public_number=number, user_id=user.id, status=OrderStatus.NEW,
        subtotal=Decimal("8900.00"), discount_total=Decimal("500.00"),
        delivery_price=Decimal("0.00"), total=Decimal("8400.00"),
        customer_name="", customer_phone="", customer_city="",
    )
    session.add(order)
    await session.flush()
    return order


@pytest.mark.asyncio
async def test_release_returns_the_use_to_the_code_and_the_client():
    engine, factory = await _world()
    try:
        async with factory() as session:
            user = User(tg_id=1, username="buyer")
            promo = PromoCode(
                code="SALE", discount_type=PromoDiscountType.FIXED,
                value=Decimal("500.00"), per_user_limit=1, max_uses=100,
                used_count=0, is_active=True,
            )
            session.add_all([user, promo])
            await session.flush()
            order = await _order(session, user, "R-1")
            await promo_service.register_usage(
                session, promo=promo, user_id=user.id, order_id=order.id,
                discount=Decimal("500.00"),
            )
            await session.commit()
            assert promo.used_count == 1

            released = await promo_service.release_usage(session, order_id=order.id)
            await session.commit()

            assert released is True
            assert promo.used_count == 0
            left = await session.scalars(select(PromoUsage))
            assert list(left) == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_releasing_twice_does_not_drive_the_counter_below_zero():
    """Оператор может отменить заказ, потом вернуть и отменить снова.

    Отрицательный счётчик в админке читался бы как поломка, а не как возврат.
    """
    engine, factory = await _world()
    try:
        async with factory() as session:
            user = User(tg_id=1, username="buyer")
            promo = PromoCode(
                code="SALE", discount_type=PromoDiscountType.FIXED,
                value=Decimal("500.00"), used_count=0, is_active=True,
            )
            session.add_all([user, promo])
            await session.flush()
            order = await _order(session, user, "R-1")
            await promo_service.register_usage(
                session, promo=promo, user_id=user.id, order_id=order.id,
                discount=Decimal("500.00"),
            )
            await session.commit()

            await promo_service.release_usage(session, order_id=order.id)
            await session.commit()
            again = await promo_service.release_usage(session, order_id=order.id)

            assert again is False
            assert promo.used_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_order_without_a_promo_is_not_a_problem():
    engine, factory = await _world()
    try:
        async with factory() as session:
            user = User(tg_id=1, username="buyer")
            session.add(user)
            await session.flush()
            order = await _order(session, user, "R-1")
            await session.commit()

            assert await promo_service.release_usage(session, order_id=order.id) is False
    finally:
        await engine.dispose()


def test_both_cancel_paths_return_the_code():
    """Клиент отменяет сам, оператор — из админки. Возврат нужен в обоих."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "api" / "routes" / "catalog_api.py"
    ).read_text(encoding="utf-8")

    def body(name: str) -> str:
        head = source.index(f"async def {name}(")
        return source[head : source.index("\n@router.", head + 1)]

    assert "promo_service.release_usage" in body("cancel_order")
    assert "promo_service.release_usage" in body("manage_order_status")
