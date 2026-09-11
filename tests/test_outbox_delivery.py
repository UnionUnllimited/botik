"""Временный отказ Telegram не должен стоить сообщению попытки.

У сообщения в очереди пять попыток: после них его перестают предлагать боту
насовсем. Это правильно для клиента, закрывшегося от бота, — доставить ему
уже нечем. Но лимит частоты Telegram отвечает тем же способом, а он
временный: полоса лимитов на утренней рассылке съедала попытки одну за
другой, и «оплата получена» не доходила уже никогда.

Модуль доставки получает бота и клиента API извне — зовём его по-настоящему.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parents[1] / "bot"


class _Shop:
    """Наш API со стороны бота: что отдали и о чём отчитались."""

    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages
        self.acks: list[tuple] = []

    async def outbox(self, limit: int = 20):
        return {"messages": self.messages[:limit]}, ""

    async def outbox_ack(self, message_id, *, ok, error="", blocked=False, thread_id=0):
        self.acks.append((message_id, ok, error, blocked))
        return {}, ""


def _module(shop: _Shop):
    stubs = {
        "src": types.ModuleType("src"),
        "src.shop_api": shop,
        "src.order_topics": types.ModuleType("src.order_topics"),
        "loguru": types.ModuleType("loguru"),
    }
    stubs["src"].__path__ = [str(BOT / "src")]
    stubs["loguru"].logger = types.SimpleNamespace(
        info=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
        debug=lambda *_a, **_k: None,
    )

    async def _send_card(*_args, **_kwargs):
        return 0

    stubs["src.order_topics"].send_card = _send_card

    saved = {key: sys.modules.get(key) for key in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(
            "_shop_outbox_probe", BOT / "src" / "shop_outbox.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, was in saved.items():
            if was is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = was


class _Bot:
    """Телеграм, который отвечает так, как велено."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.raises = raises
        self.sent: list[int] = []

    async def send_message(self, chat_id, _text, **_kwargs):
        if self.raises is not None:
            raise self.raises
        self.sent.append(chat_id)


def _skip_the_wait(monkeypatch, module) -> None:
    """Ждать выданные Telegram секунды в тесте незачем.

    Настоящий `sleep` запоминаем до подмены: `module.asyncio` — это тот же
    самый модуль, и лямбда, зовущая `asyncio.sleep`, позвала бы саму себя.
    """
    real = asyncio.sleep

    async def _instant(_seconds):
        await real(0)

    monkeypatch.setattr(module.asyncio, "sleep", _instant)


def _letter(message_id: int = 1) -> dict:
    return {
        "id": message_id,
        "tg_id": 777,
        "text": "Оплата получена",
        "buttons": [],
        "kind": "payment",
        "chat_id": None,
        "thread_id": None,
        "topic_title": "",
        "order_id": None,
    }


@pytest.mark.asyncio
async def test_a_rate_limit_leaves_the_attempt_alone(monkeypatch):
    from aiogram.exceptions import TelegramRetryAfter

    shop = _Shop([_letter()])
    module = _module(shop)
    _skip_the_wait(monkeypatch, module)

    sent = await module.deliver_once(
        _Bot(raises=TelegramRetryAfter(method=None, message="flood", retry_after=1))
    )

    assert sent == 0
    # Ни одного отчёта: отчёт об отказе засчитал бы попытку, а их всего пять.
    assert shop.acks == []


@pytest.mark.asyncio
async def test_a_blocked_client_does_burn_the_message():
    """Ему доставить уже нечем — очередь копить незачем."""
    from aiogram.exceptions import TelegramForbiddenError

    shop = _Shop([_letter()])
    module = _module(shop)

    await module.deliver_once(_Bot(raises=TelegramForbiddenError(method=None, message="blocked")))

    ((message_id, ok, _error, blocked),) = shop.acks
    assert (message_id, ok, blocked) == (1, False, True)


@pytest.mark.asyncio
async def test_any_other_failure_counts_as_an_attempt():
    """Случайный отказ временный, но бесконечно пробовать его тоже нельзя."""
    shop = _Shop([_letter()])
    module = _module(shop)

    await module.deliver_once(_Bot(raises=RuntimeError("что-то не то")))

    ((_id, ok, error, blocked),) = shop.acks
    assert ok is False
    assert blocked is False
    assert "что-то не то" in error


@pytest.mark.asyncio
async def test_a_delivered_letter_is_reported_as_sent():
    shop = _Shop([_letter()])
    module = _module(shop)
    bot = _Bot()

    assert await module.deliver_once(bot) == 1
    assert bot.sent == [777]
    ((message_id, ok, _error, _blocked),) = shop.acks
    assert (message_id, ok) == (1, True)


@pytest.mark.asyncio
async def test_a_rate_limit_stops_the_whole_batch(monkeypatch):
    """Остальные из пачки всё равно не пройдут — незачем жечь и их."""
    from aiogram.exceptions import TelegramRetryAfter

    shop = _Shop([_letter(1), _letter(2), _letter(3)])
    module = _module(shop)
    _skip_the_wait(monkeypatch, module)

    await module.deliver_once(
        _Bot(raises=TelegramRetryAfter(method=None, message="flood", retry_after=1))
    )

    assert shop.acks == []
