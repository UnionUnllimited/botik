"""Приложение — вторая дверь в ту же комнату, а не замена чату.

Дверей две: кнопка у поля ввода (видна с любого экрана переписки) и кнопка
в главном меню (для тех, кто первую не замечает). Обе появляются, только
когда приложение открыто всем: пока идёт обкатка и список закрыт, кнопка
привела бы любого в «приложение пока открыто не всем» — это хуже, чем её
отсутствие. Позванные на тест открывают приложение командой.

Ответ «открыто всем» бот держит в памяти минуту. Меню рисуется на каждый
`/start`, и запрос к нам в этом месте был бы и задержкой входа, и
зависимостью меню от того, отвечаем ли мы сейчас.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parents[1] / "bot"


def _journal() -> types.ModuleType:
    module = types.ModuleType("loguru")
    module.logger = types.SimpleNamespace(
        info=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
        debug=lambda *_a, **_k: None,
    )
    return module


def _load(name: str, path: Path, stubs: dict):
    saved = {key: sys.modules.get(key) for key in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for key, was in saved.items():
            if was is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = was


@pytest.fixture
def shop_api(monkeypatch):
    package = types.ModuleType("src")
    package.__path__ = [str(BOT / "src")]
    module = _load(
        "src.shop_api",
        BOT / "src" / "shop_api.py",
        {"src": package, "loguru": _journal(), "app_config": types.ModuleType("app_config")},
    )
    module.forget_miniapp_answer()
    monkeypatch.setenv("FLEET_API_URL", "https://shop.example/")
    monkeypatch.setenv("FLEET_API_TOKEN", "token")
    return module


class TestAskingWhetherTheAppIsOpen:
    @pytest.mark.asyncio
    async def test_the_answer_is_remembered(self, shop_api, monkeypatch):
        """Меню рисуется на каждый вход — спрашивать каждый раз незачем."""
        asked = []

        async def _get(path, params=None):
            asked.append(path)
            return {"open_to_all": True}, ""

        monkeypatch.setattr(shop_api, "get", _get)

        assert await shop_api.miniapp_open_to_all() is True
        assert await shop_api.miniapp_open_to_all() is True
        assert len(asked) == 1, "второй раз спрашивать не нужно"

    @pytest.mark.asyncio
    async def test_a_silent_api_means_closed(self, shop_api, monkeypatch):
        """Кнопка вела бы на адрес, который всё равно не отвечает."""

        async def _get(_path, _params=None):
            return {}, "Основное приложение не ответило"

        monkeypatch.setattr(shop_api, "get", _get)
        assert await shop_api.miniapp_open_to_all() is False

    @pytest.mark.asyncio
    async def test_a_failure_is_not_remembered(self, shop_api, monkeypatch):
        """Иначе одна осечка прятала бы кнопку на всю минуту."""
        answers = [({}, "молчит"), ({"open_to_all": True}, "")]

        async def _get(_path, _params=None):
            return answers.pop(0)

        monkeypatch.setattr(shop_api, "get", _get)
        assert await shop_api.miniapp_open_to_all() is False
        assert await shop_api.miniapp_open_to_all() is True

    @pytest.mark.asyncio
    async def test_the_question_carries_no_name(self, shop_api, monkeypatch):
        """Вопрос общий: про конкретного человека спрашивает только команда."""
        seen = {}

        async def _get(_path, params=None):
            seen.update(params or {})
            return {"open_to_all": True}, ""

        monkeypatch.setattr(shop_api, "get", _get)
        await shop_api.miniapp_open_to_all()
        assert seen == {"tg_id": 0}


class TestTheDoorInTheMenu:
    """Кнопка `btn_miniapp` в главном меню."""

    SOURCE = (BOT / "keyboards.py").read_text(encoding="utf-8")
    REGISTRY = (BOT / "button_registry.py").read_text(encoding="utf-8")

    def test_it_is_a_web_app_button(self):
        """Обычная ссылка открыла бы браузер вместо приложения."""
        head = self.SOURCE.index("if key == 'btn_miniapp':")
        body = self.SOURCE[head : self.SOURCE.index("if key == 'btn_renew_sub':", head)]
        assert "WebAppInfo" in body
        assert "web_app=" in body

    def test_it_hides_while_the_app_is_open_by_list(self):
        head = self.SOURCE.index("if key == 'btn_miniapp':")
        body = self.SOURCE[head : self.SOURCE.index("if key == 'btn_renew_sub':", head)]
        assert "miniapp_open_to_all" in body
        assert body.index("miniapp_open_to_all") < body.index("WebAppInfo")

    def test_it_hides_without_https(self):
        """Telegram открывает приложения только по https: кнопка на http молча
        не срабатывает, и человек решит, что сломан бот."""
        head = self.SOURCE.index("if key == 'btn_miniapp':")
        body = self.SOURCE[head : self.SOURCE.index("if key == 'btn_renew_sub':", head)]
        assert "https://" in body

    def test_it_is_allowed_in_the_layout(self):
        assert "'btn_miniapp'," in self.REGISTRY

    def test_it_stands_in_the_default_layout(self):
        assert "['btn_miniapp']," in self.REGISTRY


class TestTheDoorByTheInputField:
    """Кнопка меню у поля ввода — она одна: команды или приложение."""

    SOURCE = (BOT / "main.py").read_text(encoding="utf-8-sig")

    def _body(self) -> str:
        head = self.SOURCE.index("async def miniapp_menu_button(")
        return self.SOURCE[head : self.SOURCE.index("async def show_main_menu(", head)]

    def test_the_app_takes_the_place_when_it_is_open(self):
        assert "MenuButtonWebApp" in self._body()

    def test_commands_stay_while_it_is_not(self):
        body = self._body()
        assert "MenuButtonCommands()" in body
        assert body.index("miniapp_open_to_all") < body.index("MenuButtonWebApp")

    def test_a_failure_does_not_cost_the_menu(self):
        """Кнопка у поля ввода — не повод не открыть само меню."""
        assert "except Exception" in self._body()

    def test_the_menu_uses_it(self):
        assert "menu_button=await miniapp_menu_button()" in self.SOURCE


def test_the_chat_keeps_everything_the_app_has():
    """Приложение не должно стать единственным местом, где что-то работает.

    Ему нужны https, живой API и свежий Telegram; чату — только бот. Как
    только что-то окажется доступно лишь в приложении, появится класс
    клиентов, которые этого не могут.
    """
    registry = (BOT / "button_registry.py").read_text(encoding="utf-8")
    for key in ("btn_catalog", "btn_my_router", "btn_my_orders", "btn_renew_sub"):
        assert f"'{key}'," in registry, f"{key} пропал из меню — в чате не осталось пути"
