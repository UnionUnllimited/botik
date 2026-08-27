"""Заказ в топике рабочего чата.

Карточку собираем мы, отправляет бот. Проверяется то, что ломается тихо:
второй топик у одного заказа, кнопка, которой на этом шаге быть не должно,
и выключенная возможность, которая не имеет права мешать заказу создаться.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from core.enums import DeliveryMethod, DeliverySpeed, OrderStatus
from core.models import Delivery, Order, OrderItem, User
from core.services import order_topics


def _order(**kwargs) -> Order:
    values = {
        "id": 12,
        "public_number": "R-260827-0012",
        "status": OrderStatus.NEW,
        "customer_name": "Иванов Иван",
        "customer_phone": "+79001234567",
        "customer_city": "Самара",
        "subtotal": Decimal("9299.00"),
        "discount_total": Decimal("0.00"),
        "total": Decimal("9299.00"),
        "user_id": 5,
        "items": [],
        "tg_topic_id": None,
    }
    values.update(kwargs)
    order = Order(**values)
    return order


def _item(title: str = "Роутер TR3000", total: str = "9299.00") -> OrderItem:
    return OrderItem(
        title=title,
        quantity=1,
        unit_price=Decimal(total),
        total_price=Decimal(total),
    )


class TestTitle:
    def test_number_and_city(self):
        assert order_topics.topic_title(_order()) == "R-260827-0012 · Самара"

    def test_city_may_be_missing(self):
        """Город берётся из заказа или доставки, но обязательным не бывает:
        топик всё равно должен завестись, иначе заказ не уедет в чат вовсе."""
        assert order_topics.topic_title(_order(customer_city="")) == "R-260827-0012"

    def test_title_fits_telegram(self):
        long_city = "Ц" * 300
        assert len(order_topics.topic_title(_order(customer_city=long_city))) <= 128


class TestCardText:
    def test_shows_what_operator_decides_by(self):
        order = _order(items=[_item()], user=User(id=5, tg_id=614685408, username="union"))
        text = order_topics.card_text(order)
        assert "R-260827-0012" in text
        assert "Иванов Иван" in text
        assert "+79001234567" in text
        assert "Роутер TR3000" in text
        assert "9299" in text
        assert "Не оплачен" in text

    def test_delivery_price_not_named_is_not_zero(self):
        """Ноль — законная цена: доставку можно подарить. Отличает их отметка."""
        delivery = Delivery(
            method=DeliveryMethod.CDEK,
            speed=DeliverySpeed.FAST,
            city="Самара",
            price=Decimal("0.00"),
            quoted_at=None,
        )
        text = order_topics.card_text(_order(items=[_item()], delivery=delivery))
        assert "не названа" in text

        delivery.quoted_at = dt.datetime(2026, 8, 27, tzinfo=dt.UTC)
        text = order_topics.card_text(_order(items=[_item()], delivery=delivery))
        assert "не названа" not in text
        assert "ждёт оплаты" in text


class TestCardButtons:
    """Кнопка, которая ответит «сейчас нельзя», хуже отсутствующей: с телефона
    её нажимают вслепую, и отказ читается как поломка."""

    @staticmethod
    def _actions(buttons) -> list[str]:
        return [item["data"].split(":")[-1] for item in buttons if item.get("data")]

    def test_unpaid_order_has_nothing_to_bind(self):
        buttons = order_topics.card_buttons(_order(), has_device=False)
        assert "mac" not in self._actions(buttons)

    def test_paid_order_asks_for_a_router(self):
        order = _order(status=OrderStatus.PAID, paid_at=dt.datetime(2026, 8, 27, tzinfo=dt.UTC))
        assert "mac" in self._actions(order_topics.card_buttons(order, has_device=False))

    def test_already_bound_order_does_not_ask_again(self):
        order = _order(status=OrderStatus.PAID, paid_at=dt.datetime(2026, 8, 27, tzinfo=dt.UTC))
        assert "mac" not in self._actions(order_topics.card_buttons(order, has_device=True))

    def test_cancelled_order_has_no_tracking(self):
        order = _order(status=OrderStatus.CANCELLED)
        assert "track" not in self._actions(order_topics.card_buttons(order))

    def test_paid_delivery_is_not_priced_again(self):
        delivery = Delivery(
            method=DeliveryMethod.CDEK,
            speed=DeliverySpeed.FAST,
            city="Самара",
            price=Decimal("450.00"),
            quoted_at=dt.datetime(2026, 8, 27, tzinfo=dt.UTC),
            paid_at=dt.datetime(2026, 8, 27, tzinfo=dt.UTC),
        )
        assert "dlv" not in self._actions(order_topics.card_buttons(_order(delivery=delivery)))

    def test_writing_to_the_client_goes_through_the_bot(self):
        """Не ссылка в личку оператора: клиент разговаривает с ботом, письмо
        от незнакомого человека он в лучшем случае не узнает. Заодно оператор
        не светит свой аккаунт каждому покупателю."""
        order = _order(user=User(id=5, tg_id=614685408, username="union"))
        buttons = order_topics.card_buttons(order)
        assert not [item for item in buttons if item.get("url")], (
            "кнопка ведёт наружу — сообщение уйдёт не от бота"
        )
        assert "dm" in self._actions(buttons)

    def test_client_without_telegram_gets_no_button(self):
        assert "dm" not in self._actions(order_topics.card_buttons(_order(user=None)))

    def test_callback_fits_telegram_limit(self):
        """64 байта. Длиннее — Telegram обрежет молча, и кнопка перестанет работать."""
        order = _order(id=9_999_999, status=OrderStatus.PAID, paid_at=dt.datetime.now(dt.UTC))
        for item in order_topics.card_buttons(order, has_device=False):
            if item.get("data"):
                assert len(item["data"].encode()) <= 64


class TestPush:
    @pytest.mark.asyncio
    async def test_disabled_chat_does_not_break_the_order(self, monkeypatch):
        """Топики — возможность, а не обязанность: без чата заказ обязан
        создаваться как раньше."""

        async def _no_chat(_session):
            return 0

        monkeypatch.setattr(order_topics, "chat_id", _no_chat)
        assert await order_topics.push(None, _order()) is None

    @pytest.mark.asyncio
    async def test_first_message_asks_to_create_a_topic(self, monkeypatch):
        queued = await self._queue(monkeypatch, _order(items=[_item()]))
        assert queued.topic_title == "R-260827-0012 · Самара"
        assert queued.thread_id is None

    @pytest.mark.asyncio
    async def test_second_message_goes_into_the_same_topic(self, monkeypatch):
        """Название пустое — топик уже есть. Заведи бот второй, переписка
        по заказу разъехалась бы на две ветки, и половина решений потерялась
        бы в той, куда перестали смотреть."""
        queued = await self._queue(monkeypatch, _order(items=[_item()], tg_topic_id=77))
        assert queued.topic_title == ""
        assert queued.thread_id == 77

    @pytest.mark.asyncio
    async def test_note_explains_what_changed(self, monkeypatch):
        queued = await self._queue(monkeypatch, _order(items=[_item()]), note="✓ Заказ оплачен")
        assert queued.text.startswith("✓ Заказ оплачен")

    @staticmethod
    async def _queue(monkeypatch, order, note: str = ""):
        added = []

        class _Session:
            def add(self, item):
                added.append(item)

        async def _chat(_session):
            return -1001234567890

        async def _device(_session, _order):
            return False

        monkeypatch.setattr(order_topics, "chat_id", _chat)
        monkeypatch.setattr(order_topics, "has_device", _device)
        message = await order_topics.push(_Session(), order, note=note)
        assert added == [message]
        assert message.chat_id == -1001234567890
        assert message.order_id == order.id
        assert message.kind == order_topics.KIND
        return message


class TestButtonsMatchWhatTheHandleCanDo:
    """Кнопка должна уметь то, что обещает.

    Оператор жмёт её пальцем с телефона, не читая карточку целиком, и отказ
    «сейчас нельзя» читается как поломка бота, а не как «так задумано».
    """

    @staticmethod
    def _actions(buttons) -> list[str]:
        return [item["data"].split(":")[-1] for item in buttons if item.get("data")]

    def test_order_without_delivery_has_no_tracking_button(self):
        """Трек-номер — колонка доставки: нет её, и класть номер некуда."""
        assert "track" not in self._actions(order_topics.card_buttons(_order(delivery=None)))

    def test_order_with_delivery_has_it(self):
        delivery = Delivery(
            method=DeliveryMethod.CDEK,
            speed=DeliverySpeed.FAST,
            city="Самара",
            price=Decimal("0.00"),
        )
        assert "track" in self._actions(order_topics.card_buttons(_order(delivery=delivery)))


class TestCardReachesTheQueueFromRealCallers:
    """Карточка должна доезжать до чата от тех, кто её отправляет на деле.

    Тесты выше строят заказ в памяти и передают клиента полем — так ленивая
    загрузка связи никогда не срабатывает. А на живой сессии заказ приходит
    из базы, и `order.user` у него не загружен: обращение к нему в async —
    исключение. Оплата его проглатывала (карточка «Заказ оплачен» не уходила
    вовсе), а сохранение заметки отвечало пятисоткой.
    """

    TOPIC_CHAT = -1001234567890

    @staticmethod
    async def _factory():
        from sqlalchemy import BigInteger
        from sqlalchemy.dialects.postgresql import JSONB
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.ext.compiler import compiles

        from core.models import Device, Notification, Payment, Plan, Referral, Subscription, SubscriptionEvent
        from core.models.base import Base

        @compiles(JSONB, "sqlite")
        def _jsonb_as_json(_type, _compiler, **_kwargs) -> str:
            return "JSON"

        @compiles(BigInteger, "sqlite")
        def _bigint_as_integer(_type, _compiler, **_kwargs) -> str:
            """SQLite нумерует сама только INTEGER PRIMARY KEY, у очереди ключ BIGINT."""
            return "INTEGER"

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[
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
                        Notification.__table__,
                    ],
                )
            )
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    @pytest.fixture(autouse=True)
    def _topics_on(self, monkeypatch):
        async def _chat(_session):
            return self.TOPIC_CHAT

        monkeypatch.setattr(order_topics, "chat_id", _chat)

    async def _seed(self, session):
        from core.enums import OrderItemType

        session.add(User(id=5, tg_id=614685408, username="union"))
        order = Order(
            id=12,
            public_number="R-260828-0012",
            user_id=5,
            status=OrderStatus.AWAITING_PAYMENT,
            subtotal=Decimal("9299.00"),
            total=Decimal("9299.00"),
            currency="RUB",
            customer_name="Иванов Иван",
            customer_phone="+79001234567",
            customer_city="Самара",
        )
        order.items.append(
            OrderItem(
                item_type=OrderItemType.PRODUCT,
                title="Роутер TR3000",
                quantity=1,
                unit_price=Decimal("9299.00"),
                total_price=Decimal("9299.00"),
            )
        )
        session.add(order)
        await session.commit()

    @staticmethod
    async def _queued(session) -> list:
        from sqlalchemy import select

        from core.models import Notification

        return list(await session.scalars(select(Notification).where(Notification.kind == order_topics.KIND)))

    @pytest.mark.asyncio
    async def test_payment_pushes_the_card(self, monkeypatch):
        """«✓ Заказ оплачен» обязано доезжать до рабочего чата."""
        from core.enums import PaymentProviderName, PaymentPurpose, PaymentStatus
        from core.models import Payment
        from core.services import activation
        from core.services import payments as payment_service

        async def _no_panel(_session, _subscription):
            return False

        monkeypatch.setattr(activation, "sync_panel_expiry", _no_panel)

        engine, factory = await self._factory()
        try:
            async with factory() as session:
                await self._seed(session)
                session.add(
                    Payment(
                        id=1,
                        user_id=5,
                        order_id=12,
                        provider=PaymentProviderName.PLATEGA,
                        purpose=PaymentPurpose.ORDER,
                        status=PaymentStatus.PENDING,
                        idempotency_key="key-1",
                        amount=Decimal("9299.00"),
                        currency="RUB",
                    )
                )
                await session.commit()

                payment = await session.get(Payment, 1)
                await payment_service.apply_status(session, payment, status=PaymentStatus.SUCCEEDED)
                await session.commit()

                cards = await self._queued(session)
                assert cards, "карточка оплаченного заказа не доехала до рабочего чата"
                assert "оплачен" in cards[0].text.lower()
                assert cards[0].chat_id == self.TOPIC_CHAT
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_note_is_saved_and_pushed(self):
        """Заметка оператора сохраняется, а не отвечает пятисоткой."""
        from api.routes import catalog_api

        engine, factory = await self._factory()
        try:
            async with factory() as session:
                await self._seed(session)

                result = await catalog_api.manage_order_note(
                    12, {"note": "Позвонить после обеда"}, session=session
                )
                await session.commit()

                assert result == {"ok": True}
                saved = await session.get(Order, 12)
                assert saved.admin_note == "Позвонить после обеда"
                cards = await self._queued(session)
                assert cards, "карточка после заметки не ушла в чат"
        finally:
            await engine.dispose()
