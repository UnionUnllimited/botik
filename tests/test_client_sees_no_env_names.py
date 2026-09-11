"""Покупателю не показывают имена переменных окружения.

Ошибки из нашего API приходят в бота одним полем и деловые, и служебные.
Первые написаны клиенту — «Роутера нет в наличии», «Заказ не найден», — и
показывать их нужно как есть. Вторые написаны тому, кто правит `.env`:

    Токен не подошёл: FLEET_API_TOKEN здесь и API_FLEET_TOKEN там должны
    совпадать.

Показывали одинаково. Покупатель, зашедший в каталог в неудачную минуту,
видел это вместо роутеров — и читал как «магазин сломан», а заодно узнавал,
как у нас называются переменные.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parents[1] / "bot"


def _load(name: str, path: Path, stubs: dict[str, types.ModuleType]):
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


def _journal() -> types.ModuleType:
    """loguru живёт в окружении бота, у нас его нет."""
    module = types.ModuleType("loguru")
    module.logger = types.SimpleNamespace(
        info=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
        debug=lambda *_a, **_k: None,
    )
    return module


@pytest.fixture(scope="module")
def shop_api():
    package = types.ModuleType("src")
    package.__path__ = [str(BOT / "src")]
    return _load(
        "src.shop_api",
        BOT / "src" / "shop_api.py",
        {"src": package, "loguru": _journal(), "app_config": types.ModuleType("app_config")},
    )


@pytest.fixture(scope="module")
def catalog(shop_api):
    package = types.ModuleType("src")
    package.__path__ = [str(BOT / "src")]
    settings = types.ModuleType("app_config")
    settings.app_conf = types.SimpleNamespace(get=lambda *_a, **_k: "")
    buttons = types.ModuleType("button_helpers")
    buttons.btn = lambda *_a, **_k: None
    return _load(
        "_router_catalog_probe",
        BOT / "src" / "router_catalog.py",
        {
            "src": package,
            "src.shop_api": shop_api,
            "app_config": settings,
            "button_helpers": buttons,
            "keyboards": types.ModuleType("keyboards"),
            "db_helpers": types.ModuleType("db_helpers"),
            "loguru": _journal(),
        },
    )


class TestWhatTheClientReads:
    def test_a_broken_token_becomes_an_apology(self, catalog, shop_api):
        outage = shop_api.Outage(
            "Токен не подошёл: FLEET_API_TOKEN здесь и API_FLEET_TOKEN там должны совпадать."
        )
        shown = catalog.for_client(outage)

        assert "FLEET_API_TOKEN" not in shown
        assert "поддержку" in shown

    def test_an_unreachable_app_becomes_the_same_apology(self, catalog, shop_api):
        shown = catalog.for_client(shop_api.Outage("Основное приложение не ответило: ConnectTimeout"))
        assert "Основное приложение" not in shown

    def test_a_missing_config_becomes_the_same_apology(self, catalog, shop_api):
        assert "FLEET_API_URL" not in catalog.for_client(shop_api.NO_CONFIG)

    def test_a_business_refusal_reaches_the_client_word_for_word(self, catalog):
        """«Роутера нет в наличии» написано клиенту — портить его нельзя."""
        assert catalog.for_client("Роутера нет в наличии") == "Роутера нет в наличии"

    def test_a_missing_record_is_a_business_refusal(self, catalog):
        assert catalog.for_client("Эта модель больше не продаётся.") == (
            "Эта модель больше не продаётся."
        )


class TestWhatTheOperatorGets:
    def test_the_detail_is_written_to_the_journal(self, catalog, shop_api, monkeypatch):
        """Скрытое от клиента обязано лечь в журнал целиком."""
        said: list[str] = []
        monkeypatch.setattr(catalog.logger, "warning", said.append)

        catalog.for_client(shop_api.Outage("Токен не подошёл: FLEET_API_TOKEN"))

        assert said and "FLEET_API_TOKEN" in said[0]

    def test_nobody_logs_it_twice(self):
        """Запись живёт в переводе — у зовущих её быть не должно.

        Мест показа шесть: продублировав строчку у каждого, мы получили бы
        одну беду двумя строками в журнале и третью — у седьмого места,
        где её забыли бы.
        """
        source = (BOT / "src" / "router_catalog.py").read_text(encoding="utf-8")
        assert source.count('logger.warning(f"[CATALOG] {error}")') == 1


class TestWhichErrorsAreWhich:
    def test_the_shop_marks_its_own_diagnostics(self, shop_api):
        assert isinstance(shop_api.NO_CONFIG, shop_api.Outage)

    def test_an_outage_is_still_a_string(self, shop_api):
        """Наследник строки: `error` печатают и сравнивают в двух десятках мест."""
        outage = shop_api.Outage("что-то сломалось")
        assert outage == "что-то сломалось"
        assert f"{outage}" == "что-то сломалось"
        assert bool(outage) is True
