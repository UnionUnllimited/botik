"""Пересчёт цены доставки не оставляет живым старый счёт.

Цену называет оператор, и назвать её заново — обычное дело: не тот вес, не
тот город, договор с другим перевозчиком. Но ссылка на прежний счёт уже
ушла клиенту в переписку, а погасить её у провайдера нечем. Открыв старое
сообщение, клиент платил прежнюю сумму — и доставка молча отмечалась
оплаченной: посылка уезжала, разницы недоставало, и узнать об этом было
неоткуда.

Защита в двух местах. Пересчёт гасит устаревший счёт у нас, чтобы клиент не
получил вторую живую ссылку; разбор колбэка сверяет пришедшую сумму с
нынешней ценой — на случай, если по старой ссылке всё-таки заплатили.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core.enums import (
    DeliveryMethod,
    DeliverySpeed,
    OrderStatus,
    PaymentProviderName,
    PaymentPurpose,
    PaymentStatus,
)
from core.models import Delivery, Notification, Order, Payment, User
from core.models.base import Base
from core.services import payments as payment_service


@compiles(JSONB, "sqlite")
def _jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "INTEGER"


TABLES = [
    User.__table__,
    Order.__table__,
    Delivery.__table__,
    Payment.__table__,
    Notification.__table__,
]


async def _world():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _order_with_quote(session, price: str) -> Order:
    user = User(tg_id=1, username="buyer")
    session.add(user)
    await session.flush()
    order = Order(
        public_number="R-1",
        user_id=user.id,
        status=OrderStatus.PAID,
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
    session.add(
        Delivery(
            order_id=order.id,
            method=DeliveryMethod.CDEK,
            speed=DeliverySpeed.FAST,
            city="Москва",
            recipient_name="Клиент",
            recipient_phone="+79000000000",
            price=Decimal(price),
            quoted_at=dt.datetime.now(dt.UTC),
        )
    )
    await session.flush()
    return order


async def _invoice(session, order: Order, amount: str) -> Payment:
    payment = Payment(
        user_id=order.user_id,
        order_id=order.id,
        provider=PaymentProviderName.PLATEGA,
        purpose=PaymentPurpose.DELIVERY,
        status=PaymentStatus.SUCCEEDED,
        idempotency_key=f"d{amount}",
        amount=Decimal(amount),
        currency="RUB",
        description="Доставка",
        paid_at=dt.datetime.now(dt.UTC),
    )
    session.add(payment)
    await session.flush()
    return payment


@pytest.fixture(autouse=True)
def _no_topic_card(monkeypatch):
    """Карточка заказа в топике здесь ни при чём.

    У неё своя машинерия — настройки через Redis, кнопки, поиск роутера, —
    и свои тесты. Без подмены каждый вызов ждёт Redis по пять секунд.
    """

    async def _quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(payment_service, "_push_topic", _quiet)


async def _loaded(session, order_id: int) -> Order:
    """Заказ со всем, что нужно разбору колбэка."""
    from sqlalchemy.orm import selectinload

    return await session.scalar(
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.delivery), selectinload(Order.user))
    )


@pytest.mark.asyncio
async def test_paying_the_current_price_marks_the_delivery_paid():
    engine, factory = await _world()
    try:
        async with factory() as session:
            order = await _order_with_quote(session, "600.00")
            payment = await _invoice(session, order, "600.00")
            await session.commit()

            await payment_service._apply_delivery_payment(
                session, payment, await _loaded(session, order.id)
            )
            await session.commit()

            fresh = await _loaded(session, order.id)
            assert fresh.delivery.paid_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_old_link_paid_at_the_old_price_does_not_close_the_delivery(monkeypatch):
    """Клиент пролистал переписку вверх и заплатил по прежнему счёту."""
    from core.config import settings

    monkeypatch.setattr(settings.bot, "alerts_chat_id", -100500)
    engine, factory = await _world()
    try:
        async with factory() as session:
            order = await _order_with_quote(session, "600.00")
            payment = await _invoice(session, order, "500.00")
            await session.commit()

            await payment_service._apply_delivery_payment(
                session, payment, await _loaded(session, order.id)
            )
            await session.commit()

            fresh = await _loaded(session, order.id)
            assert fresh.delivery.paid_at is None

            alarms = list(await session.scalars(select(Notification)))
            assert len(alarms) == 1
            assert "не полностью" in alarms[0].text
            assert "500" in alarms[0].text
            assert "600" in alarms[0].text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_paying_more_than_asked_still_lets_the_parcel_go(monkeypatch):
    """Доставку могли подешевить после того, как счёт ушёл.

    Держать посылку из-за переплаты нельзя — разницу разбирает человек.
    """
    from core.config import settings

    monkeypatch.setattr(settings.bot, "alerts_chat_id", -100500)
    engine, factory = await _world()
    try:
        async with factory() as session:
            order = await _order_with_quote(session, "400.00")
            payment = await _invoice(session, order, "600.00")
            await session.commit()

            await payment_service._apply_delivery_payment(
                session, payment, await _loaded(session, order.id)
            )
            await session.commit()

            fresh = await _loaded(session, order.id)
            assert fresh.delivery.paid_at is not None
    finally:
        await engine.dispose()


class TestTheRequoteItself:
    """Проверка по исходнику: ручка тянет за собой половину приложения."""

    SOURCE = (
        Path(__file__).resolve().parents[1] / "api" / "routes" / "catalog_api.py"
    ).read_text(encoding="utf-8")

    def _body(self) -> str:
        start = self.SOURCE.index("async def manage_delivery_quote(")
        return self.SOURCE[start : self.SOURCE.index("\n@router.", start + 1)]

    def test_an_outdated_invoice_is_cancelled(self):
        body = self._body()
        assert "PaymentStatus.CANCELED" in body
        assert "Payment.amount != price" in body

    def test_an_invoice_for_the_same_price_is_reused(self):
        """Оператор мог менять перевозчика, не трогая цену: вторая живая
        ссылка на те же деньги клиенту не нужна."""
        body = self._body()
        assert "_alive_payment" in body
        assert "if alive is None:" in body

    def test_the_callback_checks_the_amount(self):
        """Ссылку у провайдера не погасить — вторая половина защиты там."""
        payments = (
            Path(__file__).resolve().parents[1] / "core" / "services" / "payments.py"
        ).read_text(encoding="utf-8")
        start = payments.index("async def _apply_delivery_payment(")
        body = payments[start : payments.index("\nasync def ", start + 1)]
        assert "payment.amount >= price" in body
