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

    def test_client_button_is_a_link_not_a_callback(self):
        """Отвечает клиенту человек, и разговор идёт в его чате с ботом,
        а не в топике: пересылать через нас было бы игрой в испорченный телефон."""
        order = _order(user=User(id=5, tg_id=614685408, username="union"))
        client = [item for item in order_topics.card_buttons(order) if item.get("url")]
        assert client and "614685408" in client[0]["url"]

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
