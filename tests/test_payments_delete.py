"""Удаление отменённых платежей: мусор уходит, деньги остаются.

В списке платежей единственный настоящий платёж терялся среди десятка
«отменён» — ссылок на оплату, которые клиент так и не открыл. Их теперь можно
стереть, по одной или все разом. Стерегутся две вещи: стирается только то, за
чем нет денег, и «удалить все» не задевает ни оплаченные, ни ждущие.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes import catalog_api
from core.enums import PaymentProviderName, PaymentPurpose, PaymentStatus
from core.models import Order, Payment, Plan, Subscription, User
from core.models.base import Base

ROOT = Path(__file__).resolve().parents[1]


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "INTEGER"


async def _world():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[
                    User.__table__,
                    Plan.__table__,
                    Order.__table__,
                    Subscription.__table__,
                    Payment.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user = User(tg_id=1, username="union")
        session.add(user)
        await session.flush()
        for number, state in enumerate(
            (
                PaymentStatus.SUCCEEDED,
                PaymentStatus.PENDING,
                PaymentStatus.CANCELED,
                PaymentStatus.CANCELED,
                PaymentStatus.FAILED,
                PaymentStatus.REFUNDED,
            ),
            start=1,
        ):
            session.add(
                Payment(
                    user_id=user.id,
                    provider=PaymentProviderName.PLATEGA,
                    purpose=PaymentPurpose.ORDER,
                    status=state,
                    idempotency_key=f"key-{number}",
                    amount=Decimal("300"),
                )
            )
        await session.commit()
    return engine, factory


async def _states(factory) -> list[str]:
    async with factory() as session:
        return sorted(str(p.status) for p in await session.scalars(select(Payment)))


@pytest.mark.asyncio
async def test_one_cancelled_is_deleted_and_a_paid_one_is_not():
    engine, factory = await _world()
    try:
        async with factory() as session:
            cancelled = await session.scalar(select(Payment).where(Payment.status == PaymentStatus.CANCELED))
            paid = await session.scalar(select(Payment).where(Payment.status == PaymentStatus.SUCCEEDED))
            assert (await catalog_api.manage_payment_delete(cancelled.id, session))["ok"] is True
            refused = await catalog_api.manage_payment_delete(paid.id, session)
            await session.commit()
        assert refused["ok"] is False
        assert "только отменённый" in refused["error"]
        assert (await _states(factory)).count("canceled") == 1
        assert "succeeded" in await _states(factory)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_delete_all_spares_money():
    engine, factory = await _world()
    try:
        async with factory() as session:
            result = await catalog_api.manage_payments_delete_cancelled(session)
            await session.commit()
        assert result == {"ok": True, "count": 3}
        assert await _states(factory) == ["pending", "refunded", "succeeded"]
    finally:
        await engine.dispose()


def test_only_the_admin_sees_and_uses_deletion():
    page = (ROOT / "bot" / "web_admin" / "templates" / "payments_shop.html").read_text(encoding="utf-8")
    head = page.index("macro delete_button")
    assert "current_user.is_admin" in page[head : page.index("endmacro", head)]
    route = (ROOT / "bot" / "web_admin" / "routes" / "orders_shop.py").read_text(encoding="utf-8")
    for name in ("async def payment_shop_delete", "async def payments_shop_delete_cancelled"):
        body = route[route.index(name) : route.index(name) + 400]
        assert "_admin_only()" in body, name
