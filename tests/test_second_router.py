"""Клиент с двумя роутерами: у каждого своя подписка.

Самый дорогой сценарий из всех, что есть у этого товара: человек покупает
второй роутер — на дачу, родителям, взамен сломанного. Подписка привязана
к устройству, значит и оплата второго заказа обязана заводить **свою**
подписку, а не продлевать ту, что уже идёт на первом роутере.

Раньше оплата брала «текущую подписку клиента» и продлевала её. Второй роутер
приезжал, выходил на связь и получал отказ «Подписка уже активна»: заказ
навсегда оставался в «Отправлен», доступа не было, деньги были приняты.

Сессия здесь настоящая (SQLite через async-движок), а не заглушка: половина
цепочки — это запросы к базе, и подставленный объект их не проверяет.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core.enums import (
    OrderItemType,
    OrderStatus,
    PaymentProviderName,
    PaymentPurpose,
    PaymentStatus,
    SubscriptionStatus,
)
from core.models import (
    Delivery,
    Device,
    Order,
    OrderItem,
    Payment,
    Plan,
    Referral,
    Subscription,
    SubscriptionEvent,
    User,
)
from core.models.base import Base
from core.services import payments as payment_service

NOW = dt.datetime(2026, 8, 28, 12, tzinfo=dt.UTC)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(_type, _compiler, **_kwargs) -> str:
    """В этом тесте JSONB Postgres хранится обычным JSON."""
    return "JSON"


TABLES = (
    User.__table__,
    Plan.__table__,
    Order.__table__,
    OrderItem.__table__,
    Delivery.__table__,
    Payment.__table__,
    Subscription.__table__,
    SubscriptionEvent.__table__,
    Device.__table__,
    Referral.__table__,
)


async def _factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(sync_connection, tables=TABLES)
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _quiet_outside_world(monkeypatch):
    """Панель и рабочий чат в этом тесте ни при чём — их отвечает заглушка."""
    from core.services import activation, order_topics

    async def _no_panel(_session, _subscription):
        return False

    async def _topics_off(_session):
        return 0

    monkeypatch.setattr(activation, "sync_panel_expiry", _no_panel)
    monkeypatch.setattr(order_topics, "chat_id", _topics_off)


def _plan(plan_id: int, months: int, price: str) -> Plan:
    return Plan(
        id=plan_id,
        slug=f"m{months}",
        title=f"{months} мес.",
        months=months,
        extra_days=0,
        price=Decimal(price),
    )


def _order(order_id: int, *, user_id: int, plan_id: int, total: str) -> Order:
    order = Order(
        id=order_id,
        public_number=f"R-260828-{order_id:04d}",
        user_id=user_id,
        status=OrderStatus.AWAITING_PAYMENT,
        subtotal=Decimal(total),
        total=Decimal(total),
        currency="RUB",
        customer_name="Токарев Тимур",
        customer_phone="+79001234567",
        customer_city="Казань",
    )
    order.items.append(
        OrderItem(
            item_type=OrderItemType.PLAN,
            plan_id=plan_id,
            title="Подписка",
            quantity=1,
            unit_price=Decimal(total),
            total_price=Decimal(total),
        )
    )
    return order


def _payment(payment_id: int, *, user_id: int, order_id: int | None, amount: str, **extra) -> Payment:
    return Payment(
        id=payment_id,
        user_id=user_id,
        order_id=order_id,
        provider=PaymentProviderName.PLATEGA,
        purpose=PaymentPurpose.ORDER if order_id else PaymentPurpose.SUBSCRIPTION,
        status=PaymentStatus.PENDING,
        idempotency_key=f"key-{payment_id}",
        amount=Decimal(amount),
        currency="RUB",
        **extra,
    )


class TestSecondRouterGetsItsOwnSubscription:
    @pytest.mark.asyncio
    async def test_paid_order_does_not_touch_the_running_subscription(self):
        """Оплата второго роутера не должна продлевать подписку первого."""
        engine, factory = await _factory()
        try:
            async with factory() as session:
                session.add_all([User(id=1, tg_id=614685408), _plan(1, 1, "399.00"), _plan(2, 12, "3490.00")])
                running = Subscription(
                    id=1,
                    user_id=1,
                    plan_id=1,
                    device_id=10,
                    status=SubscriptionStatus.ACTIVE,
                    started_at=NOW - dt.timedelta(days=10),
                    expires_at=NOW + dt.timedelta(days=20),
                )
                session.add(running)
                session.add(_order(2, user_id=1, plan_id=2, total="3490.00"))
                session.add(_payment(2, user_id=1, order_id=2, amount="3490.00"))
                await session.commit()

                payment = await session.get(Payment, 2)
                await payment_service.apply_status(
                    session, payment, status=PaymentStatus.SUCCEEDED
                )
                await session.commit()

                first = await session.get(Subscription, 1)
                assert first.expires_at == NOW + dt.timedelta(days=20), (
                    "срок первого роутера сдвинулся — клиент оплатил второй, "
                    "а дни ушли первому"
                )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_paid_order_creates_a_subscription_of_its_own(self):
        """У второго заказа появляется своя подписка, ждущая своего роутера."""
        engine, factory = await _factory()
        try:
            async with factory() as session:
                session.add_all([User(id=1, tg_id=614685408), _plan(1, 1, "399.00"), _plan(2, 12, "3490.00")])
                session.add(
                    Subscription(
                        id=1,
                        user_id=1,
                        plan_id=1,
                        device_id=10,
                        status=SubscriptionStatus.ACTIVE,
                        expires_at=NOW + dt.timedelta(days=20),
                    )
                )
                session.add(_order(2, user_id=1, plan_id=2, total="3490.00"))
                session.add(_payment(2, user_id=1, order_id=2, amount="3490.00"))
                await session.commit()

                payment = await session.get(Payment, 2)
                await payment_service.apply_status(session, payment, status=PaymentStatus.SUCCEEDED)
                await session.commit()

                born = await session.scalar(
                    select(Subscription).where(Subscription.order_id == 2)
                )
                assert born is not None, "второй заказ остался без подписки"
                assert born.status is SubscriptionStatus.PENDING, (
                    "подписка ждёт своего роутера: дни не идут, пока он не включён"
                )
                assert born.plan_id == 2, "срок должен быть тот, что купили во втором заказе"
                assert payment.subscription_id == born.id
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_repeated_processing_does_not_double_the_subscription(self):
        """Повторное проведение той же оплаты не заводит вторую подписку."""
        engine, factory = await _factory()
        try:
            async with factory() as session:
                session.add_all([User(id=1, tg_id=614685408), _plan(1, 1, "399.00")])
                session.add(_order(1, user_id=1, plan_id=1, total="399.00"))
                session.add(_payment(1, user_id=1, order_id=1, amount="399.00"))
                await session.commit()

                payment = await session.get(Payment, 1)
                await payment_service._apply_success(session, payment)
                await session.flush()
                await payment_service._apply_success(session, payment)
                await session.commit()

                found = list(await session.scalars(select(Subscription).where(Subscription.order_id == 1)))
                assert len(found) == 1, "у заказа завелись две подписки"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_renewal_moves_the_named_subscription(self):
        """Продление двигает ту подписку, за которую заплатили.

        У клиента их две, и «текущая» — та, чей срок дальше. Пока продление
        брало её, владелец двух роутеров не мог продлить первый вовсе.
        """
        engine, factory = await _factory()
        try:
            async with factory() as session:
                session.add_all([User(id=1, tg_id=614685408), _plan(1, 1, "399.00")])
                near = Subscription(
                    id=1,
                    user_id=1,
                    plan_id=1,
                    device_id=10,
                    status=SubscriptionStatus.ACTIVE,
                    expires_at=NOW + dt.timedelta(days=5),
                )
                far = Subscription(
                    id=2,
                    user_id=1,
                    plan_id=1,
                    device_id=11,
                    status=SubscriptionStatus.ACTIVE,
                    expires_at=NOW + dt.timedelta(days=300),
                )
                session.add_all([near, far])
                session.add(
                    _payment(3, user_id=1, order_id=None, amount="399.00", plan_id=1, subscription_id=1)
                )
                await session.commit()

                payment = await session.get(Payment, 3)
                await payment_service.apply_status(session, payment, status=PaymentStatus.SUCCEEDED)
                await session.commit()

                near = await session.get(Subscription, 1)
                far = await session.get(Subscription, 2)
                assert near.expires_at > NOW + dt.timedelta(days=30), (
                    "продлили не ту подписку: деньги за первый роутер ушли второму"
                )
                assert far.expires_at == NOW + dt.timedelta(days=300)
        finally:
            await engine.dispose()


class TestActivationPicksTheRightSubscription:
    """Роутер включает подписку своего заказа, а не самую старую.

    У клиента, купившего два роутера с разными сроками, порядок активации
    произвольный: приедут в разные дни. Взяв первую попавшуюся ожидающую,
    мы дали бы годовой срок роутеру, купленному на месяц.
    """

    @pytest.mark.asyncio
    async def test_pending_of_this_order_wins(self):
        from core.services import subscriptions as subscription_service

        engine, factory = await _factory()
        try:
            async with factory() as session:
                session.add_all([User(id=1, tg_id=614685408), _plan(1, 1, "399.00"), _plan(2, 12, "3490.00")])
                session.add(_order(1, user_id=1, plan_id=1, total="399.00"))
                session.add(_order(2, user_id=1, plan_id=2, total="3490.00"))
                session.add_all(
                    [
                        Subscription(
                            id=1, user_id=1, plan_id=1, order_id=1, status=SubscriptionStatus.PENDING
                        ),
                        Subscription(
                            id=2, user_id=1, plan_id=2, order_id=2, status=SubscriptionStatus.PENDING
                        ),
                    ]
                )
                await session.commit()

                own = await subscription_service.get_pending(session, 1, order_id=2)
                assert own is not None and own.id == 2, (
                    "роутер второго заказа должен включить срок, купленный в нём"
                )

                # Заказа не знаем — берём самую старую, как было.
                oldest = await subscription_service.get_pending(session, 1)
                assert oldest is not None and oldest.id == 1
        finally:
            await engine.dispose()


class TestOrderFoundByClient:
    """Роутер, привязанный к клиенту в обход заказа, всё равно активируется.

    Устройство привязывают кнопкой в топике — тогда у него есть и заказ,
    и клиент. Но привязать его к клиенту можно и из парка, по MAC с наклейки:
    тогда `order_id` пуст, и автоактивация раньше не начиналась вовсе. Роутер
    стоял на связи неделями, а оплаченная подписка ждала устройства — снаружи
    это выглядело тремя разными неисправностями сразу.
    """

    async def _prepare(self, session, *, orders: int):
        from core.enums import OrderStatus
        from core.models import Device, Order, User

        user = User(tg_id=500, username="kelvin")
        session.add(user)
        await session.flush()
        made = []
        for number in range(orders):
            order = Order(
                public_number=f"R-{number}",
                user_id=user.id,
                status=OrderStatus.DONE,
                paid_at=dt.datetime(2026, 8, 30, tzinfo=dt.UTC),
                subtotal=0, discount_total=0, delivery_price=0, total=0,
            )
            session.add(order)
            made.append(order)
        device = Device(mac="D4:0D:AB:2B:A4:EE", user_id=user.id)
        session.add(device)
        await session.flush()
        return device, made

    @pytest.mark.asyncio
    async def test_single_order_is_found(self):
        from core.services.activation import _order_awaiting_router

        engine, factory = await _factory()
        try:
            async with factory() as session:
                device, orders = await self._prepare(session, orders=1)
                found = await _order_awaiting_router(session, device)
                assert found is not None
                assert found.id == orders[0].id
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_two_orders_are_left_to_the_operator(self):
        """С двумя купленными роутерами выбор неоднозначен: ошибка означала бы,
        что дни ушли не тому устройству."""
        from core.services.activation import _order_awaiting_router

        engine, factory = await _factory()
        try:
            async with factory() as session:
                device, _ = await self._prepare(session, orders=2)
                assert await _order_awaiting_router(session, device) is None
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_order_with_its_own_router_is_not_taken(self):
        """Заказ, которому роутер уже достался, чужому устройству не отдаём."""
        from core.models import Device
        from core.services.activation import _order_awaiting_router

        engine, factory = await _factory()
        try:
            async with factory() as session:
                device, orders = await self._prepare(session, orders=1)
                session.add(Device(mac="AA:BB:CC:DD:EE:FF", order_id=orders[0].id))
                await session.flush()
                assert await _order_awaiting_router(session, device) is None
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_device_without_client_finds_nothing(self):
        from core.models import Device
        from core.services.activation import _order_awaiting_router

        engine, factory = await _factory()
        try:
            async with factory() as session:
                assert await _order_awaiting_router(session, Device(mac="A0:B1:C2:D3:E4:F5")) is None
        finally:
            await engine.dispose()
