"""Кнопка «Мой роутер» — только тем, у кого роутер есть или едет.

Клиент, впервые зашедший в бота, видел её в главном меню и попадал на экран
про роутер, которого не покупал. Решение о показе принимает наша ручка,
а бот его только исполняет — поэтому проверяется разбор ответа, включая
случай, когда ручка не ответила.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"


def _load_shop_api():
    """Грузит `bot/src/shop_api.py` по пути: `bot/` — не пакет, импортом не взять."""
    spec = importlib.util.spec_from_file_location(
        "shop_api_under_test", BOT_DIR / "src" / "shop_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def shop_api():
    return _load_shop_api()


class TestMyRouterAvailability:
    @pytest.mark.asyncio
    async def test_shown_when_api_says_so(self, shop_api, monkeypatch):
        async def _get(path, params):
            assert path == "/api/v1/catalog/my-router/available"
            assert params == {"tg_id": 42}
            return {"show": True}, ""

        monkeypatch.setattr(shop_api, "get", _get)
        assert await shop_api.my_router_available(42) is True

    @pytest.mark.asyncio
    async def test_hidden_when_client_has_nothing(self, shop_api, monkeypatch):
        async def _get(path, params):
            return {"show": False}, ""

        monkeypatch.setattr(shop_api, "get", _get)
        assert await shop_api.my_router_available(42) is False

    @pytest.mark.asyncio
    async def test_shown_when_api_is_down(self, shop_api, monkeypatch):
        """Спрятать из-за недоступного API значит отобрать вход к устройству."""

        async def _get(path, params):
            return {}, "Каталог не отвечает"

        monkeypatch.setattr(shop_api, "get", _get)
        assert await shop_api.my_router_available(42) is True

    @pytest.mark.asyncio
    async def test_missing_field_is_not_a_yes(self, shop_api, monkeypatch):
        """Ответ без `show` — не повод показывать: ручка ответила, ей верим."""

        async def _get(path, params):
            return {}, ""

        monkeypatch.setattr(shop_api, "get", _get)
        assert await shop_api.my_router_available(42) is False
