"""Отрицательная цена — опечатка, и принимать её нельзя.

Поле цены в форме текстовое, минус в нём никто не ловил. Отрицательная цена
не роняет оформление: сумма заказа считается, упирается в ноль — и роутер
уезжает даром, а заказ выглядит оплаченным на ноль рублей. Заметить это
можно только по выручке в конце дня.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes import catalog_api
from core.models import Plan, Product
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
                sync, tables=[Product.__table__, Plan.__table__]
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class TestTheParser:
    def test_a_normal_price_passes(self):
        assert catalog_api._price("8900", "0") == Decimal("8900")

    def test_a_comma_is_a_decimal_point(self):
        """Оператор пишет «8900,50» — так пишут цену по-русски."""
        assert catalog_api._price("8900,50", "0") == Decimal("8900.50")

    def test_a_minus_is_refused(self):
        assert catalog_api._price("-8900", "0") is None

    def test_zero_is_left_alone(self):
        """Ноль — не опечатка: подарок или цена, которую назовут потом."""
        assert catalog_api._price("0", "0") == Decimal("0")

    def test_nonsense_falls_back_to_what_was(self):
        assert catalog_api._price("не число", "8900") == Decimal("8900")


@pytest.mark.asyncio
async def test_a_router_cannot_be_saved_at_a_negative_price():
    engine, factory = await _world()
    try:
        async with factory() as session:
            answer = await catalog_api.save_product(
                0,
                payload={"slug": "basic", "title": "Роутер Basic", "price": "-8900"},
                session=session,
            )

        assert answer["ok"] is False
        assert "отрицательной" in answer["error"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_negative_old_price_is_refused_too():
    """Зачёркнутая цена — тоже цена: минус в ней читается как скидка."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            answer = await catalog_api.save_product(
                0,
                payload={
                    "slug": "basic",
                    "title": "Роутер Basic",
                    "price": "8900",
                    "old_price": "-9900",
                },
                session=session,
            )

        assert answer["ok"] is False
        assert "Старая цена" in answer["error"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_plan_cannot_be_saved_at_a_negative_price():
    engine, factory = await _world()
    try:
        async with factory() as session:
            answer = await catalog_api.save_plan(
                0,
                payload={"slug": "m1", "title": "30 дней", "months": 1, "price": "-300"},
                session=session,
            )

        assert answer["ok"] is False
        assert "отрицательной" in answer["error"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_honest_price_still_saves():
    """Проверка не должна мешать обычной правке."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            answer = await catalog_api.save_product(
                0,
                payload={
                    "slug": "basic",
                    "title": "Роутер Basic",
                    "price": "8900",
                    "old_price": "9900",
                    "stock": 3,
                },
                session=session,
            )

        assert answer["ok"] is True
        assert answer["product"]["price"] == "8900"
        assert answer["product"]["old_price"] == "9900"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_brand_new_plan_saves_without_extra_days():
    """У только что заведённого тарифа поле ещё пустое.

    Умолчание колонки проставляется при записи, а сравнение с нулём считалось
    до неё: создание тарифа без «дополнительных дней» падало пятисоткой, и
    оператор видел «Основное приложение ответило 500» без причины.
    """
    engine, factory = await _world()
    try:
        async with factory() as session:
            answer = await catalog_api.save_plan(
                0,
                payload={"slug": "m1", "title": "30 дней", "months": 1, "price": "300"},
                session=session,
            )

        assert answer["ok"] is True
        assert answer["plan"]["extra_days"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_tariff_with_a_broken_price_is_not_sold():
    """Тарифы приезжают зеркалом из их админки — цену там тоже печатают руками.

    Отрицательная цена тарифа не отказ оформления: она вычтется из суммы
    заказа и уйдёт клиенту скидкой. Такой тариф пропускаем и выключаем —
    продавать по цене, которой быть не может, хуже, чем не продавать.
    """
    engine, factory = await _world()
    try:
        async with factory() as session:
            answer = await catalog_api.sync_plans(
                payload={
                    "tariffs": [
                        {"id": 1, "days": 30, "name": "30 дней", "price": "300"},
                        {"id": 2, "days": 90, "name": "90 дней", "price": "-900"},
                    ]
                },
                session=session,
            )

            assert answer["ok"] is True
            assert answer["created"] == 1, "заведён только тариф с честной ценой"

            from sqlalchemy import select

            plans = list(await session.scalars(select(Plan)))
            assert [p.title for p in plans] == ["30 дней"]
    finally:
        await engine.dispose()
