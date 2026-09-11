"""Остаток — предел продаж, а не подпись под карточкой.

Число в админке проверялось при оформлении, но не значило ничего: заказов
принималось сколько угодно, и оператор узнавал об этом при отгрузке. Теперь
предел считается по заказам — списывать его нельзя, иначе забытый возврат
тихо съедает остаток и товар «кончается», пока роутеры лежат.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core.enums import OrderItemType, OrderStatus
from core.models import Order, OrderItem, Product, User
from core.models.base import Base
from core.services import orders as order_service


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


async def _session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[
                    User.__table__, Product.__table__, Order.__table__, OrderItem.__table__,
                ],
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _product(stock: int, *, preorder: bool = False) -> Product:
    return Product(
        slug="basic", title="Роутер Basic", price=Decimal("8900.00"),
        stock=stock, allow_preorder=preorder, is_active=True,
    )


async def _order(session, user: User, product: Product, status: OrderStatus, number: str, qty: int = 1):
    order = Order(
        public_number=number, user_id=user.id, status=status,
        subtotal=Decimal("8900.00"), discount_total=Decimal("0.00"),
        delivery_price=Decimal("0.00"), total=Decimal("8900.00"),
        customer_name="", customer_phone="", customer_city="",
    )
    session.add(order)
    await session.flush()
    session.add(
        OrderItem(
            order_id=order.id, item_type=OrderItemType.PRODUCT, product_id=product.id,
            title=product.title, quantity=qty, unit_price=product.price, total_price=product.price * qty,
        )
    )
    await session.flush()
    return order


@pytest.mark.asyncio
async def test_limit_counts_live_orders_and_ignores_cancelled():
    engine, factory = await _session()
    try:
        async with factory() as session:
            user = User(tg_id=1, username="buyer")
            product = _product(stock=3)
            session.add_all([user, product])
            await session.flush()

            await _order(session, user, product, OrderStatus.PAID, "R-1")
            await _order(session, user, product, OrderStatus.SHIPPED, "R-2")
            # Отменённый и возвращённый предел не занимают: роутер вернулся
            # на полку, и продать его можно снова.
            await _order(session, user, product, OrderStatus.CANCELLED, "R-3")
            await _order(session, user, product, OrderStatus.REFUNDED, "R-4")
            await session.commit()

            assert await order_service.sold_units(session, product.id) == 2
            assert await order_service.units_left(session, product) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_order_above_the_limit_is_refused():
    engine, factory = await _session()
    try:
        async with factory() as session:
            user = User(tg_id=1, username="buyer")
            product = _product(stock=1)
            session.add_all([user, product])
            await session.flush()
            await _order(session, user, product, OrderStatus.PAID, "R-1")
            await session.commit()

            draft = order_service.OrderDraft(product_id=product.id)
            with pytest.raises(order_service.OrderError) as exc:
                await order_service.calculate_totals(session, draft=draft, user_id=user.id)

            assert "наличии" in str(exc.value)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_order_within_the_limit_goes_through():
    engine, factory = await _session()
    try:
        async with factory() as session:
            user = User(tg_id=1, username="buyer")
            product = _product(stock=2)
            session.add_all([user, product])
            await session.flush()
            await _order(session, user, product, OrderStatus.PAID, "R-1")
            await session.commit()

            totals = await order_service.calculate_totals(
                session, draft=order_service.OrderDraft(product_id=product.id), user_id=user.id
            )

            assert totals.product is product
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_preorder_sells_past_the_limit():
    """Предзаказ на то и предзаказ: продаём то, чего ещё нет."""
    engine, factory = await _session()
    try:
        async with factory() as session:
            user = User(tg_id=1, username="buyer")
            product = _product(stock=0, preorder=True)
            session.add_all([user, product])
            await session.flush()
            await session.commit()

            totals = await order_service.calculate_totals(
                session, draft=order_service.OrderDraft(product_id=product.id), user_id=user.id
            )

            assert totals.product is product
    finally:
        await engine.dispose()


def test_sellable_falls_back_to_the_raw_number():
    """Где предел не считали, судим по самому числу — как было до счёта."""
    assert order_service.sellable(_product(stock=5), None) is True
    assert order_service.sellable(_product(stock=0), None) is False
    assert order_service.sellable(_product(stock=5), 0) is False
    assert order_service.sellable(_product(stock=0, preorder=True), 0) is True


class TestTheLastRouterDoesNotGoTwice:
    """Два заказа в одну секунду считали предел одновременно.

    Ни один не видит чужой незавершённой сделки: оба читают «остался один»,
    оба проходят проверку, оба оформляются. Клиент получает «заказ принят»,
    а роутера на складе нет — и узнаёт об этом оператор при отгрузке, когда
    деньги уже взяты. Настоящую одновременность здесь не воспроизвести:
    тесты идут на SQLite, где сделки не параллельны. Поэтому проверяем то,
    что от нас зависит, — что замок запрашивается и вовремя.
    """

    def test_the_lock_is_a_real_lock_on_postgres(self):
        from sqlalchemy import select
        from sqlalchemy.dialects import postgresql

        from core.models import Product

        statement = select(Product.id).where(Product.id == 1).with_for_update()
        assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))

    def test_it_is_taken_before_the_limit_is_counted(self):
        """Замок после подсчёта бесполезен: предел уже прочитан устаревшим."""
        import inspect

        body = inspect.getsource(order_service.create_order)
        assert body.index("_hold_the_shelf") < body.index("calculate_totals")

    @pytest.mark.asyncio
    async def test_an_order_without_a_router_locks_nothing(self):
        """Заказ на одну подписку товара не занимает — запирать нечего."""
        engine, factory = await _session()
        try:
            async with factory() as session:
                await order_service._hold_the_shelf(session, None)
        finally:
            await engine.dispose()


class TestTheLastPromoUseDoesNotGoTwice:
    """Та же гонка, что и с последним роутером, только про скидку.

    Предел «столько-то раз» проверяется чтением, а растёт записью: два заказа
    в одну секунду читают одно и то же число и оба проходят последнее
    применение. Код на один раз срабатывает дважды, и вторую скидку никто не
    назначал.
    """

    def test_the_lock_is_a_real_lock_on_postgres(self):
        from sqlalchemy import select
        from sqlalchemy.dialects import postgresql

        from core.models import PromoCode

        statement = select(PromoCode.id).where(PromoCode.code == "SALE").with_for_update()
        assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))

    def test_it_is_taken_before_the_discount_is_counted(self):
        import inspect

        body = inspect.getsource(order_service.create_order)
        assert body.index("_hold_the_promo") < body.index("calculate_totals")

    @pytest.mark.asyncio
    async def test_an_order_without_a_promo_locks_nothing(self):
        engine, factory = await _session()
        try:
            async with factory() as session:
                await order_service._hold_the_promo(session, "")
                await order_service._hold_the_promo(session, "   ")
        finally:
            await engine.dispose()

    def test_showing_the_price_does_not_hold_anything(self):
        """Код вводят в поле и смотрят цену — очередь там ни к чему."""
        import inspect

        assert "_hold_the_promo" not in inspect.getsource(order_service.calculate_totals)
