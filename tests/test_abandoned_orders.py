"""Заказ, за который не заплатили, возвращает роутер на полку.

Остаток стал пределом продаж и считается по живым заказам. Списывать его
нельзя — забытый возврат тихо съел бы склад, — но и брошенная корзина
держала роутер вечно: ссылка на оплату гасла, заказ оставался «ждёт
оплаты», и витрина писала «нет в наличии», пока роутеры лежали на складе.
Оплатить такой заказ клиент уже не мог: новой ссылки к старому заказу не
выдаётся, а старая мертва.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core.enums import (
    OrderItemType,
    OrderStatus,
    PaymentProviderName,
    PaymentPurpose,
    PaymentStatus,
    PromoDiscountType,
)
from core.models import (
    Delivery,
    Device,
    Notification,
    Order,
    OrderItem,
    Payment,
    Product,
    PromoCode,
    PromoUsage,
    User,
)
from core.models.base import Base
from core.services import order_topics
from core.services import orders as order_service
from worker.tasks import orders as task


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_as_integer(_type, _compiler, **_kwargs) -> str:
    return "INTEGER"


TABLES = [
    User.__table__,
    Delivery.__table__,
    Device.__table__,
    Product.__table__,
    Order.__table__,
    OrderItem.__table__,
    Payment.__table__,
    PromoCode.__table__,
    PromoUsage.__table__,
    Notification.__table__,
]


def _run(monkeypatch, factory) -> list[str]:
    """Задача под тестом, с подменённым источником сессий.

    Карточку в топике подменяем целиком: у неё своя машинерия — настройки
    через Redis, кнопки, поиск роутера по заказу, — и свои тесты. Здесь важно
    одно: уходит ли она вообще, когда заказ отменён не человеком.
    """
    cards: list[str] = []

    async def _card(_session, order, *, note=""):
        cards.append(note)
        return None

    monkeypatch.setattr(task, "session_scope", factory.begin)
    monkeypatch.setattr(order_topics, "push", _card)
    return cards


async def _world():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _shop(session) -> tuple[User, Product]:
    user = User(tg_id=1, username="buyer")
    product = Product(
        slug="basic", title="Роутер Basic", price=Decimal("8900.00"), stock=1, is_active=True
    )
    session.add_all([user, product])
    await session.flush()
    return user, product


async def _order(
    session,
    user: User,
    product: Product,
    *,
    number: str = "R-1",
    status: OrderStatus = OrderStatus.AWAITING_PAYMENT,
    hours_ago: int = 9,
    is_cod: bool = False,
) -> Order:
    order = Order(
        public_number=number,
        user_id=user.id,
        status=status,
        is_cod=is_cod,
        subtotal=product.price,
        discount_total=Decimal("0.00"),
        delivery_price=Decimal("0.00"),
        total=product.price,
        customer_name="Клиент",
        customer_phone="+79000000000",
        customer_city="Москва",
        created_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours_ago),
    )
    session.add(order)
    await session.flush()
    session.add(
        OrderItem(
            order_id=order.id,
            item_type=OrderItemType.PRODUCT,
            product_id=product.id,
            title=product.title,
            quantity=1,
            unit_price=product.price,
            total_price=product.price,
        )
    )
    await session.flush()
    return order


async def _payment(session, order: Order, status: PaymentStatus) -> Payment:
    payment = Payment(
        user_id=order.user_id,
        order_id=order.id,
        provider=PaymentProviderName.PLATEGA,
        purpose=PaymentPurpose.ORDER,
        status=status,
        idempotency_key=f"k{order.id}{status}",
        amount=order.total,
        currency="RUB",
        description="Заказ",
    )
    session.add(payment)
    await session.flush()
    return payment


@pytest.mark.asyncio
async def test_a_dead_link_frees_the_router(monkeypatch):
    engine, factory = await _world()
    try:
        async with factory() as session:
            user, product = await _shop(session)
            order = await _order(session, user, product)
            await _payment(session, order, PaymentStatus.CANCELED)
            await session.commit()
            # До уборки роутер числится проданным и витрина пуста.
            assert await order_service.units_left(session, product) == 0

        cards = _run(monkeypatch, factory)
        assert await task.cancel_abandoned_orders() == 1
        # Оператор должен увидеть, что заказ закрыла машина, а не человек.
        assert cards == ["↻ Отменён автоматически: не оплачен"]

        async with factory() as session:
            fresh = await session.get(Order, order.id)
            assert fresh.status is OrderStatus.CANCELLED
            product = await session.get(Product, product.id)
            assert await order_service.units_left(session, product) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_live_link_is_left_alone(monkeypatch):
    """Ссылка ещё жива — клиент как раз вводит карту."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            user, product = await _shop(session)
            order = await _order(session, user, product)
            await _payment(session, order, PaymentStatus.PENDING)
            await session.commit()

        _run(monkeypatch, factory)
        assert await task.cancel_abandoned_orders() == 0

        async with factory() as session:
            assert (await session.get(Order, order.id)).status is OrderStatus.AWAITING_PAYMENT
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_paid_order_is_never_touched(monkeypatch):
    """Оплату могли провести, а статус заказа — не догнать."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            user, product = await _shop(session)
            order = await _order(session, user, product, status=OrderStatus.NEW)
            await _payment(session, order, PaymentStatus.SUCCEEDED)
            await session.commit()

        _run(monkeypatch, factory)
        assert await task.cancel_abandoned_orders() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_fresh_order_waits_its_turn(monkeypatch):
    """Заказ часа от роду ещё может быть оплачен: ссылка живёт дольше."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            user, product = await _shop(session)
            order = await _order(session, user, product, hours_ago=1)
            await _payment(session, order, PaymentStatus.CANCELED)
            await session.commit()

        _run(monkeypatch, factory)
        assert await task.cancel_abandoned_orders() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cash_on_delivery_is_not_abandoned(monkeypatch):
    """Наложенный платёж ждёт перевозчика, а не ссылку.

    «Не оплачен» для него — нормальное состояние на всю дорогу до клиента,
    и отменять такой заказ через шесть часов значит не отправить посылку.
    """
    engine, factory = await _world()
    try:
        async with factory() as session:
            user, product = await _shop(session)
            order = await _order(
                session, user, product, status=OrderStatus.NEW, hours_ago=48, is_cod=True
            )
            await session.commit()

        _run(monkeypatch, factory)
        assert await task.cancel_abandoned_orders() == 0

        async with factory() as session:
            assert (await session.get(Order, order.id)).status is OrderStatus.NEW
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_order_that_never_got_a_link_is_cleaned_up_too(monkeypatch):
    """Провайдер мог не ответить вовсе: заказ есть, платежа нет ни одного."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            user, product = await _shop(session)
            order = await _order(session, user, product, status=OrderStatus.NEW)
            await session.commit()

        _run(monkeypatch, factory)
        assert await task.cancel_abandoned_orders() == 1

        async with factory() as session:
            assert (await session.get(Order, order.id)).status is OrderStatus.CANCELLED
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_promo_code_comes_back_with_the_router(monkeypatch):
    engine, factory = await _world()
    try:
        async with factory() as session:
            user, product = await _shop(session)
            promo = PromoCode(
                code="SALE",
                discount_type=PromoDiscountType.FIXED,
                value=Decimal("500.00"),
                used_count=1,
                per_user_limit=1,
                is_active=True,
            )
            session.add(promo)
            await session.flush()
            order = await _order(session, user, product)
            session.add(
                PromoUsage(
                    promo_code_id=promo.id,
                    user_id=user.id,
                    order_id=order.id,
                    amount_discounted=Decimal("500.00"),
                )
            )
            await _payment(session, order, PaymentStatus.CANCELED)
            await session.commit()

        _run(monkeypatch, factory)
        assert await task.cancel_abandoned_orders() == 1

        async with factory() as session:
            assert (await session.get(PromoCode, promo.id)).used_count == 0
            assert list(await session.scalars(select(PromoUsage))) == []
    finally:
        await engine.dispose()
