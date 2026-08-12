"""Сообщения клиенту ставятся в очередь, а не уходят нашим ботом.

Своего бота у нас нет: клиент разговаривает с ботом стороннего продукта,
и токен есть только у него. Всё, что мы можем, — положить готовый текст
в очередь и дождаться отчёта об отправке.
"""

from __future__ import annotations

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import notifications


class FakeSession:
    """Сессия здесь нужна ровно для `add` — записи проверяем в памяти."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, item) -> None:
        self.added.append(item)


class TestQueueing:
    @pytest.mark.anyio
    async def test_message_goes_to_queue(self):
        session = FakeSession()
        assert await notifications.send_message(42, "Привет", session=session, kind="order") is True

        (queued,) = session.added
        assert queued.tg_id == 42
        assert queued.text == "Привет"
        assert queued.kind == "order"
        assert queued.sent_at is None

    @pytest.mark.anyio
    async def test_no_session_is_refused_loudly(self):
        """Раньше сообщение молча уходило в Telegram. Теперь деть его некуда,
        и терять втихую нельзя — отказ виден вызывающему."""
        assert await notifications.send_message(42, "Привет") is False

    @pytest.mark.anyio
    async def test_no_chat_id_is_not_queued(self):
        session = FakeSession()
        assert await notifications.send_message(None, "Привет", session=session) is False
        assert session.added == []


class TestButtons:
    @pytest.mark.anyio
    async def test_link_buttons_survive(self):
        session = FakeSession()
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отследить", url="https://track.example")]]
        )
        await notifications.send_message(42, "Заказ отправлен", reply_markup=markup, session=session)
        assert session.added[0].buttons == [
            {"text": "Отследить", "url": "https://track.example"}
        ]

    @pytest.mark.anyio
    async def test_callback_buttons_are_dropped(self):
        """Callback обрабатывает конкретный бот, а он сменный: кнопка с уехавшим
        обработчиком молча перестала бы работать у клиента в руках."""
        session = FakeSession()
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Продлить", callback_data="renew")]]
        )
        await notifications.send_message(42, "Подписка кончается", reply_markup=markup, session=session)
        assert session.added[0].buttons == []
