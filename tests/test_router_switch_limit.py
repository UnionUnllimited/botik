"""Предел переключений сервера: отказ называет срок, а окно — короткое.

«Шесть в час» не давало перебрать семь серверов даже по разу: клиент упирался
в «слишком часто» на середине списка и читал это как поломку. Окно теперь
десять минут, попыток восемь, а отказ говорит, сколько ждать.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from api.routes import catalog_api


class FakeLimiter:
    calls: ClassVar[list] = []
    allowed: ClassVar[bool] = True

    def __init__(self, *args, **kwargs):
        pass

    async def hit(self, bucket, *, limit, window_sec):
        FakeLimiter.calls.append((bucket, limit, window_sec))
        return FakeLimiter.allowed, 0

    async def seconds_left(self, bucket):
        return 437


class _Device:
    id = 5


@pytest.fixture(autouse=True)
def limiter(monkeypatch):
    FakeLimiter.calls = []
    FakeLimiter.allowed = True
    monkeypatch.setattr(catalog_api, "RateLimiter", FakeLimiter)


@pytest.mark.asyncio
async def test_window_lets_a_client_try_every_server():
    wait = await catalog_api._switch_too_often(_Device())

    assert wait == 0
    bucket, limit, window = FakeLimiter.calls[0]
    assert bucket == "router_node:5"
    assert limit >= 7, "серверов в списке семь — перебрать их по разу должно быть можно"
    assert window <= 600


@pytest.mark.asyncio
async def test_refusal_names_the_wait():
    FakeLimiter.allowed = False

    wait = await catalog_api._switch_too_often(_Device())

    assert wait == 437


@pytest.mark.asyncio
async def test_switch_and_toggle_share_one_counter():
    """Роутер перезапускает сервис в обоих случаях; два счётчика удвоили бы
    число перезапусков, ради которого предел и стоит."""
    await catalog_api._switch_too_often(_Device())
    await catalog_api._switch_too_often(_Device())

    assert {call[0] for call in FakeLimiter.calls} == {"router_node:5"}
