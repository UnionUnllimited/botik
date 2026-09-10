"""Оплата мимо провайдера тоже заводит подписку.

Оператор ставит «Оплачен» руками, когда деньги пришли переводом, наличными
курьеру или картой в чате. Платежа у нас при этом нет, а подписку заводило
как раз проведение платежа — и её не появлялось вовсе. Роутер приезжал,
клиент нажимал «Активировать» и читал «Нет оплаченной подписки», хотя
заплатил всё до копейки.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core.enums import OrderItemType, OrderStatus, SubscriptionStatus
from core.models import Order, OrderItem, Plan, Product, Subscription, SubscriptionEvent, User
from core.models.base import Base
from core.services import subscriptions as subscription_service


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
                    Plan.__table__,
                    Product.__table__,
                    Order.__table__,
                    OrderItem.__table__,
                    Subscription.__table__,
                    SubscriptionEvent.__table__,
                ],
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _paid_by_hand(session, *, with_plan: bool = True, number: str = "R-1"):
    """Заказ, который оператор отметил оплаченным: роутер и, может быть, тариф."""
    user = User(tg_id=900, username="transfer")
    plan = Plan(slug="m3", title="90 дней", months=3, extra_days=0, price=Decimal("900.00"))
    product = Product(slug="basic", title="Роутер Basic", price=Decimal("8900.00"), stock=5)
    session.add_all([user, plan, product])
    await session.flush()

    order = Order(
        public_number=number,
        user_id=user.id,
        status=OrderStatus.PAID,
        subtotal=Decimal("9800.00"),
        discount_total=Decimal("0.00"),
        delivery_price=Decimal("0.00"),
        total=Decimal("9800.00"),
        customer_name="",
        customer_phone="",
        customer_city="",
    )
    session.add(order)
    await session.flush()

    items = [
        OrderItem(
            order_id=order.id,
            item_type=OrderItemType.PRODUCT,
            product_id=product.id,
            title=product.title,
            quantity=1,
            unit_price=product.price,
            total_price=product.price,
        )
    ]
    if with_plan:
        items.append(
            OrderItem(
                order_id=order.id,
                item_type=OrderItemType.PLAN,
                plan_id=plan.id,
                title=plan.title,
                quantity=1,
                unit_price=plan.price,
                total_price=plan.price,
            )
        )
    session.add_all(items)
    await session.flush()
    return user, plan, order


@pytest.mark.asyncio
async def test_a_hand_marked_order_gets_its_subscription():
    engine, factory = await _world()
    try:
        async with factory() as session:
            user, plan, order = await _paid_by_hand(session)
            await session.commit()

            made = await subscription_service.ensure_for_order(session, order)
            await session.commit()

            assert made is not None
            assert made.status is SubscriptionStatus.PENDING
            assert made.plan_id == plan.id
            assert made.order_id == order.id
            # Срок ещё не идёт: он начнётся, когда роутер выйдет на связь.
            assert made.expires_at is None
            assert made.pending_expires_at is not None
            # И активация теперь её найдёт.
            found = await subscription_service.get_pending(session, user.id, order_id=order.id)
            assert found is not None and found.id == made.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_order_that_already_has_one_is_left_alone():
    """Оператор может переставить статус туда-сюда — второй подписки быть не должно."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            _, _, order = await _paid_by_hand(session)
            await session.commit()

            first = await subscription_service.ensure_for_order(session, order)
            await session.commit()
            again = await subscription_service.ensure_for_order(session, order)
            await session.commit()

            assert first is not None
            assert again is None
            rows = list(await session.scalars(select(Subscription)))
            assert len(rows) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_order_without_a_plan_gets_nothing():
    """Один роутер к уже работающей подписке — заводить нечего."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            _, _, order = await _paid_by_hand(session, with_plan=False)
            await session.commit()

            assert await subscription_service.ensure_for_order(session, order) is None
            assert list(await session.scalars(select(Subscription))) == []
    finally:
        await engine.dispose()


def test_the_admin_route_calls_it_on_paid():
    """Иначе исправление живёт в сервисе и не срабатывает ни разу."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "api" / "routes" / "catalog_api.py"
    ).read_text(encoding="utf-8")
    head = source.index("async def manage_order_status(")
    body = source[head : source.index("\n@router.", head + 1)]

    assert "ensure_for_order" in body
    assert "OrderStatus.PAID" in body
