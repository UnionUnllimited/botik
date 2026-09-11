"""Что написано о наличии, то и должно быть у кнопки «Купить».

Остаток стал пределом продаж и считается по живым заказам. Кнопка покупки
смотрит на этот предел, а строка наличия в боте — на сырое число со склада,
и карточка одного товара считала наличие тоже по нему. Выходило два разных
ответа на один вопрос: в списке роутер «нет в наличии», внутри карточки —
с кнопкой «Купить», по которой приходил отказ. Клиент читает это как
поломку бота, а не как «разобрали».
"""

from __future__ import annotations

import sys
import types
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes import catalog_api
from core.enums import OrderItemType, OrderStatus
from core.models import Order, OrderItem, Product, User
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
                sync,
                tables=[
                    User.__table__,
                    Product.__table__,
                    Order.__table__,
                    OrderItem.__table__,
                ],
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _sold_out(session, *, stock: int, preorder: bool = False) -> Product:
    """Роутер, которого на складе `stock`, и все они уже разобраны заказами."""
    user = User(tg_id=1, username="buyer")
    product = Product(
        slug="basic",
        title="Роутер Basic",
        price=Decimal("8900.00"),
        stock=stock,
        allow_preorder=preorder,
        is_active=True,
    )
    session.add_all([user, product])
    await session.flush()
    for number in range(stock):
        order = Order(
            public_number=f"R-{number}",
            user_id=user.id,
            status=OrderStatus.PAID,
            subtotal=product.price,
            discount_total=Decimal("0.00"),
            delivery_price=Decimal("0.00"),
            total=product.price,
            customer_name="",
            customer_phone="",
            customer_city="",
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
    return product


@pytest.mark.asyncio
async def test_the_card_of_a_sold_out_router_does_not_offer_to_buy():
    engine, factory = await _world()
    try:
        async with factory() as session:
            product = await _sold_out(session, stock=2)
            await session.commit()

            answer = await catalog_api.product_card(product.id, session=session)

        card = answer["product"]
        # Число на складе прежнее — списывать его нельзя, забытый возврат
        # тихо съел бы остаток. Продавать при этом нечего.
        assert card["stock"] == 2
        assert card["stock_left"] == 0
        assert card["in_stock"] is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_card_with_units_left_still_offers_to_buy():
    engine, factory = await _world()
    try:
        async with factory() as session:
            product = await _sold_out(session, stock=3)
            product.stock = 5  # два ещё лежат
            await session.commit()

            answer = await catalog_api.product_card(product.id, session=session)

        assert answer["product"]["stock_left"] == 2
        assert answer["product"]["in_stock"] is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_preorder_survives_the_limit():
    """Предзаказ на то и предзаказ: продаём то, чего ещё нет."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            product = await _sold_out(session, stock=1, preorder=True)
            await session.commit()

            answer = await catalog_api.product_card(product.id, session=session)

        assert answer["product"]["stock_left"] == 0
        assert answer["product"]["in_stock"] is True
    finally:
        await engine.dispose()


class TestTheLineInTheBot:
    """`router_catalog` тянет за собой их окружение: настройки, кнопки, клиента
    нашего API. Подсовываем заглушки — нужна одна чистая функция."""

    @staticmethod
    def _module():
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "bot"

        settings = types.ModuleType("app_config")
        settings.app_conf = types.SimpleNamespace(get=lambda *_a, **_k: "")
        buttons = types.ModuleType("button_helpers")
        buttons.btn = lambda *_a, **_k: None
        package = types.ModuleType("src")
        package.__path__ = [str(root / "src")]
        shop = types.ModuleType("src.shop_api")
        keyboards = types.ModuleType("keyboards")
        helpers = types.ModuleType("db_helpers")
        # loguru живёт в окружении бота, у нас его нет: модуль пишет им
        # в журнал, а нам нужна одна чистая функция.
        journal = types.ModuleType("loguru")
        journal.logger = types.SimpleNamespace(
            info=lambda *_a, **_k: None,
            warning=lambda *_a, **_k: None,
            error=lambda *_a, **_k: None,
            debug=lambda *_a, **_k: None,
        )

        saved = {name: sys.modules.get(name) for name in (
            "app_config", "button_helpers", "src", "src.shop_api",
            "keyboards", "db_helpers", "loguru",
        )}
        sys.modules.update({
            "app_config": settings, "button_helpers": buttons, "src": package,
            "src.shop_api": shop, "keyboards": keyboards, "db_helpers": helpers,
            "loguru": journal,
        })
        try:
            spec = importlib.util.spec_from_file_location(
                "_router_catalog_probe", root / "src" / "router_catalog.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        finally:
            for name, was in saved.items():
                if was is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = was

    @pytest.mark.parametrize(
        ("product", "expected"),
        [
            ({"stock": 3, "stock_left": 0}, "✕ Нет в наличии"),
            ({"stock": 3, "stock_left": 2}, "✓ В наличии"),
            ({"stock": 0, "stock_left": 0, "allow_preorder": True}, "▸ Под заказ"),
            # Предел не считали — судим по складу, как было до счёта.
            ({"stock": 3, "stock_left": None}, "✓ В наличии"),
            ({"stock": 0, "stock_left": None}, "✕ Нет в наличии"),
        ],
    )
    def test_the_line_follows_the_limit(self, product, expected):
        assert self._module().stock_line(product) == expected
