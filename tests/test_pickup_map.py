"""Карту пунктов выдачи видно в обеих дверях, а не только в чате.

Своего списка пунктов у нас нет и не будет: они открываются и закрываются
каждую неделю, и устаревший список отправил бы клиента к закрытой двери.
Поэтому пункт человек выбирает на карте перевозчика и присылает адрес.

Сервер отдаёт эту карту в списке перевозчиков с самого начала, и чат её
показывал кнопкой. Приложение — нет: адрес там просили полем «Пункт выдачи»
с пустым плейсхолдером. Человек упирался в него и шёл искать карту сам —
или не шёл, и оформление на этом заканчивалось.

Это ровно тот разъезд, который дверей и касается: одно и то же оформление
в чате можно довести до конца, а в приложении нет.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "api" / "static" / "miniapp" / "app.js").read_text(encoding="utf-8")
PAGE = (ROOT / "api" / "static" / "miniapp" / "index.html").read_text(encoding="utf-8")
BOT = (ROOT / "bot" / "src" / "router_catalog.py").read_text(encoding="utf-8")
API = (ROOT / "api" / "routes" / "catalog_api.py").read_text(encoding="utf-8")
SERVICE = (ROOT / "core" / "services" / "delivery.py").read_text(encoding="utf-8")


def _delivery_step() -> str:
    """Шаг «куда везти» в приложении."""
    head = APP.index("'<h2>Куда везти</h2>'")
    return APP[head : APP.index("function checkField", head)]


class TestTheServerGivesTheMap:
    def test_it_travels_with_the_carrier(self):
        """У каждого перевозчика карта своя — значит и ссылка своя."""
        head = API.index('"carriers": [')
        assert '"pickup_url": option.pickup_url' in API[head : head + 400]

    def test_where_it_is_configured(self):
        """Оператор правит адрес карты настройкой, а не правкой кода."""
        assert "delivery.cdek_pickup_url" in SERVICE

    def test_the_post_office_has_none(self):
        """Отделения Почты ищут не по карте перевозчика — кнопки там быть
        не должно, и её отсутствие не поломка."""
        head = SERVICE.index("PICKUP_SETTING_KEYS = {")
        block = SERVICE[head : SERVICE.index("}", head)]
        assert "POST" not in block


class TestBothDoorsShowIt:
    def test_the_chat_has_the_button(self):
        assert "btn_shop_pickup_map" in BOT

    def test_the_app_has_the_button(self):
        assert "pickup-map" in _delivery_step()

    def test_the_app_opens_it_outside(self):
        """Карта чужая и в окно приложения не помещается."""
        step = _delivery_step()
        head = step.index("mapBtn.addEventListener")
        assert "tg.openLink" in step[head : head + 300]


class TestItAppearsExactlyWhenItHelps:
    def test_hidden_when_the_courier_brings_it(self):
        """Курьеру адрес двери, а не пункта: карта там ни при чём."""
        step = _delivery_step()
        head = step.index("function applyMode")
        body = step[head : step.index("applyMode();", head)]
        assert "form.toPvz && pickupUrl()" in body

    def test_hidden_when_the_carrier_has_no_map(self):
        """Пустая ссылка — это Почта. Кнопка, которая никуда не ведёт,
        хуже её отсутствия."""
        step = _delivery_step()
        head = step.index("function pickupUrl")
        body = step[head : step.index("function applyMode", head)]
        assert "pickup_url" in body
        assert "|| ''" in body, "без запасной пустой строки кнопка ведёт в undefined"

    def test_it_follows_the_chosen_carrier(self):
        """Переключили СДЭК на Яндекс — карта обязана смениться вместе с ним."""
        step = _delivery_step()
        head = step.index("input[name=\"carrier\"]")
        assert "applyMode()" in step[head : head + 300]

    def test_a_click_without_a_map_does_nothing(self):
        """Подстраховка: кнопку можно увидеть на долю секунды между
        переключением перевозчика и перерисовкой."""
        step = _delivery_step()
        head = step.index("mapBtn.addEventListener")
        assert "if (!url) { return; }" in step[head : head + 300]


USED_ICONS = sorted(set(re.findall(r"icon\('([a-z0-9-]+)'", APP)))
DRAWN_ICONS = set(re.findall(r'id="i-([a-z0-9-]+)"', PAGE))


class TestEveryIconExists:
    """`icon('pin')` без значка в наборе рисует пустоту, и молча: браузер
    на несуществующий `use href` не жалуется никак. Проверка общая, а не
    про одну кнопку, — промахнуться именем можно в любой."""

    def test_there_are_icons_to_check(self):
        assert len(USED_ICONS) >= 10

    @pytest.mark.parametrize("name", USED_ICONS)
    def test_it_is_in_the_sprite(self, name):
        assert name in DRAWN_ICONS, f"значка «{name}» нет в наборе — кнопка выйдет пустой"


def test_the_assets_version_moved():
    """Иначе у клиента останется старый app.js из кеша, и кнопки он
    не увидит — при том что на сервере она есть."""
    versions = {int(found) for found in re.findall(r"app\.(?:js|css)\?v=(\d+)", PAGE)}
    assert len(versions) == 1, f"версии ассетов разъехались: {versions}"
    assert versions.pop() >= 24
