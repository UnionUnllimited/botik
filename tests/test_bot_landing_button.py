"""Кнопка витрины в меню и возврат с витрины на карточку модели.

Витрина и бот связаны двумя ниточками: кнопка «Узнать о роутере» из меню
наружу и ссылка `?start=buy_<id>` обратно. Обе легко порвать незаметно —
кнопка с пустым адресом роняет отправку всего меню, а нераспознанный
аргумент `/start` молча открывает обычное меню, и клиент не понимает,
куда делась выбранная модель.

Модули бота грузятся по пути: `bot/` — не пакет, а `main.py` и `keyboards.py`
тянут aiogram и loguru из чужого venv, поэтому они проверяются как текст.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def shop_api():
    return _load("shop_api_landing_under_test", BOT_DIR / "src" / "shop_api.py")


@pytest.fixture(scope="module")
def registry():
    return _load("button_registry_under_test", BOT_DIR / "button_registry.py")


class TestLandingAddress:
    def test_setting_wins(self, shop_api, monkeypatch):
        """Витрина переедет на свой домен, а ручки останутся на прежнем."""
        monkeypatch.setenv("FLEET_API_URL", "https://api.example.com")
        assert shop_api.landing_url("https://routers.example.ru/") == "https://routers.example.ru"

    def test_falls_back_to_the_api_domain(self, shop_api, monkeypatch):
        """Пока настройка пуста, витрина стоит в корне того же домена."""
        monkeypatch.setenv("FLEET_API_URL", "https://vbotrouters.example.click")
        assert shop_api.landing_url("") == "https://vbotrouters.example.click"

    def test_path_in_the_api_url_is_dropped(self, shop_api, monkeypatch):
        monkeypatch.setenv("FLEET_API_URL", "https://example.click/api/v1")
        assert shop_api.landing_url("") == "https://example.click"

    def test_without_anything_there_is_no_address(self, shop_api, monkeypatch):
        """Пустой url в кнопке — ошибка отправки всего меню, а не одной кнопки."""
        monkeypatch.delenv("FLEET_API_URL", raising=False)
        assert shop_api.landing_url("") == ""

    def test_garbage_is_not_an_address(self, shop_api, monkeypatch):
        monkeypatch.setenv("FLEET_API_URL", "localhost:8000")
        assert shop_api.landing_url("") == ""


class TestButtonInMenu:
    def test_button_is_registered(self, registry):
        """Не в реестре — значит подпись не правится в админке."""
        assert "btn_landing" in {button["key"] for button in registry.BUTTON_REGISTRY}

    def test_button_may_be_placed_in_the_layout(self, registry):
        assert "btn_landing" in registry.MAIN_MENU_LAYOUT_KEYS

    def test_button_stands_first(self, registry):
        """С неё начинает тот, кто ещё не знает, что такое роутер с подпиской."""
        assert registry.DEFAULT_MAIN_MENU_LAYOUT[0] == ["btn_landing"]

    def test_glyph_is_from_the_same_family(self, registry):
        """Значки типографские: цветные эмодзи убирали в редизайне."""
        button = next(b for b in registry.BUTTON_REGISTRY if b["key"] == "btn_landing")
        assert button["default_text"].startswith("◎ ")

    def test_saved_layout_gets_the_new_button(self, registry):
        """У оператора своя раскладка — кнопка должна доехать и к нему."""
        saved = json.dumps([["btn_my_router"], ["btn_catalog", "btn_my_orders"]])
        layout = registry.parse_main_menu_layout(saved)
        assert ["btn_landing"] in layout


class TestLayoutReseed:
    """Правка дефолта не двигает раскладку, уже лежащую в базе на сервере."""

    SOURCE = (BOT_DIR / "db_helpers.py").read_text(encoding="utf-8")

    def test_mark_and_update_exist(self):
        assert "ui_landing_button_2026_08" in self.SOURCE
        assert "layouts_without_landing" in self.SOURCE

    def test_every_previous_default_is_listed(self):
        """Прошлых дефолтов раскладки два — на разных стендах лежат разные."""
        block = self.SOURCE[self.SOURCE.index("layouts_without_landing = (") :]
        block = block[: block.index("for legacy_layout in")]
        assert block.count("_json.dumps([") == 2
        assert "'btn_renew_sub'" in block and "'btn_catalog', 'btn_my_orders'" in block

    def test_mark_write_survives_an_existing_row(self):
        """Обычный INSERT упал бы на уникальном ключе и уронил создание базы."""
        block = self.SOURCE[self.SOURCE.index("landing_button_mark = ") :]
        block = block[: block.index("Кнопка витрины: раскладка")]
        assert "INSERT OR REPLACE INTO settings" in block

    def test_landing_url_setting_is_seeded(self):
        """Настройки нет в базе — оператору негде задать адрес витрины."""
        texts = (BOT_DIR / "src" / "shop_texts.py").read_text(encoding="utf-8")
        assert '"landing_url"' in texts


class TestButtonWiring:
    SOURCE = (BOT_DIR / "keyboards.py").read_text(encoding="utf-8")

    def test_button_is_resolved_as_a_link(self):
        block = self.SOURCE[self.SOURCE.index("if key == 'btn_landing':") :]
        block = block[: block.index("# Каталог роутеров")]
        assert "shop_api.landing_url" in block
        assert "url=url" in block

    def test_empty_address_hides_the_button(self):
        block = self.SOURCE[self.SOURCE.index("if key == 'btn_landing':") :]
        block = block[: block.index("# Каталог роутеров")]
        assert "if not url:" in block and "return None" in block

    def test_button_follows_the_catalog_toggle(self):
        """Выключенный каталог прячет покупку — витрина зовёт ровно туда."""
        block = self.SOURCE[self.SOURCE.index("if key == 'btn_landing':") :]
        block = block[: block.index("from src import shop_api")]
        assert "catalog_enabled" in block


class TestDeepLink:
    SOURCE = (BOT_DIR / "main.py").read_text(encoding="utf-8")

    def test_start_understands_the_product_link(self):
        assert "arg.startswith('buy_')" in self.SOURCE
        assert "wanted_product_id = int(arg[4:])" in self.SOURCE

    def test_digits_are_still_a_referral(self):
        """Разбор `buy_` стоит перед реферальным и не должен его перекрыть."""
        block = self.SOURCE[self.SOURCE.index("wanted_product_id = None") :]
        block = block[: block.index("user_id = message.from_user.id")]
        assert "elif arg.isdigit():" in block
        assert "arg.startswith('par_')" in block

    def test_card_is_shown_after_the_menu(self):
        """Сначала меню, потом карточка: клиент пришёл именно за ней."""
        menu = self.SOURCE.index("    await show_main_menu(message)\n    await show_wanted_product")
        assert menu > 0

    def test_failure_does_not_break_entering_the_bot(self):
        block = self.SOURCE[self.SOURCE.index("async def show_wanted_product") :]
        block = block[: block.index("# --- Логирование входа/выхода")]
        assert "except Exception" in block
        assert "catalog_enabled()" in block


class TestCardSender:
    SOURCE = (BOT_DIR / "src" / "router_catalog.py").read_text(encoding="utf-8")

    def test_card_is_sent_as_a_new_message(self):
        """Команду клиента бот не удаляет — правится только свой экран."""
        block = self.SOURCE[self.SOURCE.index("async def send_product_card") :]
        block = block[: block.index("# --- Регистрация")]
        assert "message.answer_photo" in block
        assert "message.delete" not in block

    def test_long_card_falls_back_to_text(self):
        block = self.SOURCE[self.SOURCE.index("async def send_product_card") :]
        block = block[: block.index("# --- Регистрация")]
        assert "CAPTION_LIMIT" in block
        assert "card_preview(product)" in block
