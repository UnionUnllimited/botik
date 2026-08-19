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


def _load_catalog():
    """Грузит `bot/src/router_catalog.py` с заглушками вместо тяжёлых зависимостей.

    Модуль наш, но живёт в чужом дереве и импортируется плоско: тянет `loguru`,
    менеджер настроек и сборщик кнопок, а те — драйвер SQLite. Ставить их
    в окружение тестов ради двух чистых функций смысла нет, поэтому подменяем.

    Подменяется ровно то, что не участвует в проверяемом: заглушка настроек
    возвращает значение по умолчанию, заглушка кнопки — сам `InlineKeyboardButton`.
    """
    import importlib.util
    import sys
    import types

    from aiogram.types import InlineKeyboardButton

    loguru = types.ModuleType("loguru")
    loguru.logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None
    )

    app_config = types.ModuleType("app_config")
    app_config.app_conf = types.SimpleNamespace(get=lambda key, default=None: default)

    button_helpers = types.ModuleType("button_helpers")

    def _btn(key, *, text="", callback_data=None, url=None):
        return InlineKeyboardButton(
            text=text or key, callback_data=callback_data, url=url
        )

    button_helpers.btn = _btn

    root = str(BOT_DIR)
    added = root not in sys.path
    if added:
        sys.path.insert(0, root)
    saved = {name: sys.modules.get(name) for name in ("loguru", "app_config", "button_helpers")}
    sys.modules.update(loguru=loguru, app_config=app_config, button_helpers=button_helpers)
    try:
        spec = importlib.util.spec_from_file_location(
            "router_catalog_under_test", BOT_DIR / "src" / "router_catalog.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        if added:
            sys.path.remove(root)


@pytest.fixture(scope="module")
def catalog():
    return _load_catalog()


class TestSeveralRouters:
    """Купил второй на дачу — первый не должен пропадать с экрана.

    Раньше ручка отдавала только последнее устройство по номеру, и подписка
    первого становилась невидимой: клиент не знал, что она кончается.
    """

    ONE = {
        "routers": [{"id": 7, "mac": "AA:BB:CC:DD:EE:01", "model": "AX3000", "online": True}],
        "router": {"id": 7, "mac": "AA:BB:CC:DD:EE:01", "activated": True, "active": True},
    }
    TWO = {
        "routers": [
            {"id": 7, "mac": "AA:BB:CC:DD:EE:01", "model": "AX3000", "online": True},
            {"id": 9, "mac": "AA:BB:CC:DD:EE:02", "model": "AX1800", "online": False},
        ],
        "router": {"id": 7, "mac": "AA:BB:CC:DD:EE:01", "activated": True, "active": True},
    }

    def _callbacks(self, markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]

    def test_single_router_has_no_switch(self, catalog):
        """У большинства клиентов роутер один — лишний ряд кнопок им не нужен."""
        callbacks = self._callbacks(catalog.my_router_keyboard(self.ONE))
        assert not any(c.startswith("shop_my_router:9") for c in callbacks)

    def test_second_router_is_offered(self, catalog):
        callbacks = self._callbacks(catalog.my_router_keyboard(self.TWO))
        assert "shop_my_router:9" in callbacks

    def test_current_router_is_not_offered_to_itself(self, catalog):
        callbacks = self._callbacks(catalog.my_router_keyboard(self.TWO))
        assert callbacks.count("shop_my_router:7") <= 1

    def test_refresh_keeps_the_chosen_router(self, catalog):
        """Обновление не должно перекидывать на первый по списку."""
        callbacks = self._callbacks(catalog.my_router_keyboard(self.TWO))
        assert "shop_my_router:7" in callbacks
