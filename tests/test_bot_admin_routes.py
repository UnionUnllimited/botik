"""Каждый `url_for` в шаблонах админки должен вести на зарегистрированный маршрут.

Ошибка тут выглядит хуже, чем стоит: `url_for` на несуществующий endpoint
роняет `base.html`, а его наследуют все страницы — падает не один экран,
а вся админка разом. Так и случилось, когда декоратор `@route` остался
на прежней функции, а тело переехало в новую.

Проверяется именно **декоратор**, а не наличие функции с таким именем:
прошлая версия этой проверки смотрела на `async def` и пропустила ровно
тот случай, ради которого писалась.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest

ADMIN = Path(__file__).resolve().parents[1] / "bot" / "web_admin"
TEMPLATES = ADMIN / "templates"


def _between(source: str, start: str, end: str) -> str:
    head = source.index(start)
    return source[head : source.index(end, head)]

_ROUTE_DECORATED = re.compile(
    r"@\w+\.route\([^)]*\)\s*(?:@\w+[^\n]*\s*)*async def (\w+)\(", re.MULTILINE
)
_ENDPOINT_KWARG = re.compile(r"endpoint=['\"](\w+)['\"]")
_URL_FOR = re.compile(r"url_for\(\s*['\"]admin\.(\w+)['\"]")


def _registered() -> set[str]:
    names: set[str] = set()
    for path in [*(ADMIN / "routes").rglob("*.py"), ADMIN / "run.py"]:
        source = path.read_text(encoding="utf-8", errors="replace")
        names |= set(_ROUTE_DECORATED.findall(source))
        names |= set(_ENDPOINT_KWARG.findall(source))
    return names


def _referenced() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for path in TEMPLATES.rglob("*.html"):
        source = path.read_text(encoding="utf-8", errors="replace")
        for match in _URL_FOR.finditer(source):
            used.setdefault(match.group(1), set()).add(path.name)
    return used


@pytest.mark.parametrize("endpoint", sorted(_referenced()))
def test_endpoint_has_a_route(endpoint):
    registered = _registered()
    where = ", ".join(sorted(_referenced()[endpoint]))
    assert endpoint in registered, (
        f"`url_for('admin.{endpoint}')` есть в {where}, но маршрут не заведён. "
        "Это роняет base.html, а с ним всю админку."
    )


class TestSettingsSaveCreatesMissingRows:
    """«Сохранить все настройки» должна сохранять и то, чего в базе ещё нет.

    Настройки заводятся при создании базы, а код добавляет новые со временем.
    `UPDATE ... WHERE key = ?` по отсутствующему ключу молча ничего не делает,
    и для оператора это выглядит как «кнопка не работает»: страница
    перезагрузилась, «успешно обновлены» показано, значение прежнее.
    """

    SOURCE = (
        Path(__file__).resolve().parents[1] / "bot/web_admin/routes/settings.py"
    ).read_text(encoding="utf-8")

    def _save_block(self) -> str:
        start = self.SOURCE.index("for key, value in form.items():")
        return self.SOURCE[start : self.SOURCE.index("# Перезагружаем кэш настроек")]

    def test_values_are_upserted(self):
        block = self._save_block()
        assert "ON CONFLICT(key) DO UPDATE" in block
        assert "UPDATE settings SET value = ? WHERE key = ?" not in block, (
            "обновление по отсутствующему ключу теряет настройку без единого слова"
        )

    def test_toggles_are_upserted_too(self):
        """Тумблеры пишутся отдельным кругом — и той же ловушкой страдали."""
        block = self._save_block()
        toggles = block[block.index("for key in toggle_button_keys:") :]
        assert "ON CONFLICT(key) DO UPDATE" in toggles


class TestSpecsWithoutAColon:
    """«Поддержка Wi-Fi 6» — характеристика без второй половины.

    Форма требовала пару «Название: значение» и отказывала на такой строке,
    теряя всю правку карточки. Оператор при этом не ошибался: не у каждой
    характеристики есть название и значение, иные — просто признак.
    """

    def _parser(self):
        """Грузим модуль по пути: `bot/` не пакет, а тянуть quart ради двух
        чистых функций незачем — берём их исходник и выполняем отдельно."""
        source = (
            Path(__file__).resolve().parents[1] / "bot/web_admin/routes/catalog_shop.py"
        ).read_text(encoding="utf-8")
        start = source.index("def _specs_from_form")
        end = source.index("def _mapping_from_form")
        namespace: dict = {"json": __import__("json")}
        exec(compile(source[start:end], "catalog_shop_specs", "exec"), namespace)  # noqa: S102
        return namespace["_specs_from_form"], namespace["specs_to_text"]

    def test_line_without_a_colon_is_accepted(self):
        parse, _ = self._parser()
        raw, error = parse("Порты: 3 LAN\nПоддержка Wi-Fi 6")
        assert error == ""
        import json

        assert json.loads(raw) == {"Порты": "3 LAN", "Поддержка Wi-Fi 6": ""}

    def test_empty_name_is_still_refused(self):
        """Строка, начинающаяся с двоеточия, — опечатка, а не характеристика."""
        parse, _ = self._parser()
        _, error = parse(": 3 LAN")
        assert "название" in error.lower()

    def test_round_trip_keeps_the_line_as_written(self):
        """Дописав двоеточие обратно, мы правили бы то, что оператор не просил."""
        parse, to_text = self._parser()
        import json

        raw, _ = parse("Поддержка Wi-Fi 6\nПорты: 3 LAN")
        assert to_text(json.loads(raw)) == "Поддержка Wi-Fi 6\nПорты: 3 LAN"

    def test_bot_card_does_not_dangle_a_colon(self):
        bot = (
            Path(__file__).resolve().parents[1] / "bot/src/router_catalog.py"
        ).read_text(encoding="utf-8")
        card = bot[bot.index("def card_text") : bot.index("def card_keyboard")]
        assert 'if value else f"• {_esc(name)}"' in card


class TestOperatorMovesTheOrderAnywhere:
    """Статус виден и меняется всегда — в самой карточке заказа.

    Схема переходов писана для автоматики: оплата, отгрузка и активация
    ходят по ней и не должны перескакивать через шаг. Человека она защищать
    не может — у него на руках возврат, отказ или заказ, закрытый раньше
    времени, и таких ходов в схеме нет.
    """

    def _source(self, relative: str) -> str:
        return (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")

    def test_service_allows_a_forced_move(self):
        from core.enums import OrderStatus
        from core.models import Order
        from core.services.orders import OrderError, set_status

        order = Order(public_number="R-1", status=OrderStatus.DONE)
        try:
            set_status(order, OrderStatus.PACKING)
        except OrderError:
            pass
        else:  # pragma: no cover — проверка самой проверки
            raise AssertionError("без force схема должна держать")

        set_status(order, OrderStatus.PACKING, force=True)
        assert order.status is OrderStatus.PACKING

    def test_admin_asks_for_force(self):
        """Переводит человек, а не автоматика: он видит, что делает."""
        client = self._source("bot/src/shop_api.py")
        body = client[client.index("async def set_order_status") :]
        body = body[: body.index("async def quote_delivery")]
        assert '"force": True' in body

    def test_card_offers_every_status(self):
        card = self._source("bot/web_admin/templates/orders_shop_card.html")
        assert "all_statuses" in card
        assert "Другие состояния" in card

    def test_card_never_hides_the_form(self):
        """Раньше на конечном статусе селект пропадал вовсе, и заказ
        оставался запертым в том состоянии, куда его завела автоматика."""
        card = self._source("bot/web_admin/templates/orders_shop_card.html")
        assert "переходов больше нет" not in card

    def test_current_status_is_written_out(self):
        card = self._source("bot/web_admin/templates/orders_shop_card.html")
        assert "Сейчас:" in card


class TestSettingsSaveTellsHowMany:
    """«Успешно обновлены» без числа ничего не говорит.

    Молчаливая потеря настройки выглядела ровно так же, как удачное
    сохранение, — и разбирать это приходилось по базе.
    """

    SOURCE = (
        Path(__file__).resolve().parents[1] / "bot/web_admin/routes/settings.py"
    ).read_text(encoding="utf-8")

    def test_counts_saved_keys(self):
        assert "saved_keys" in self.SOURCE
        assert "Настройки сохранены: {len(saved_keys)}" in self.SOURCE

    def test_logs_them(self):
        assert "[SETTINGS] сохранено ключей" in self.SOURCE

    def test_browser_validation_does_not_swallow_the_click(self):
        """Числовое поле с `min` и значением из базы, которое проверку
        не проходит, отменяло отправку целиком. Поле при этом лежало
        на скрытой вкладке: показать подсказку браузер не может и молчит,
        а для оператора это выглядело как «кнопка не работает».
        """
        page = (
            Path(__file__).resolve().parents[1]
            / "bot/web_admin/templates/settings_general.html"
        ).read_text(encoding="utf-8")
        form = page[page.index('id="gen-settings-form"') :][:200]
        assert "novalidate" in form


class TestModeratorGuardCoversEverySection:
    """Карта путь→раздел обязана знать про все разделы админки.

    Права модератора считаются по префиксу пути. Разделы, перенесённые
    из нашей админки, в карту не попали — и путь падал в `dashboard`,
    выданный модератору по умолчанию: он открывал root-пароль клиентского
    роутера, консоль, склад, заказы и списки доменов для всего парка.

    Проверяется по исходникам: код админки живёт в своём venv и тестами
    не импортируется. Зато новая страница без записи в карте видна сразу.
    """

    RUN = (ADMIN / "run.py").read_text(encoding="utf-8", errors="replace")

    _ROUTE_PATH = re.compile(r"@admin_bp(?:_instance)?\.route\(\s*['\"](/[^'\"]*)['\"]")
    _MAPPING = re.compile(r"\(\s*['\"](/[\w\-]+)['\"]\s*,\s*['\"](\w+)['\"]\s*\)")

    OUTSIDE_THE_MAP: ClassVar[set[str]] = {
        # Вход и выход: до проверки разделов и после неё.
        "/login",
        "/logout",
        # Статика и PWA — открыты всякому, кто вошёл, иначе у модератора
        # не грузится сама админка.
        "/admin-static",
        "/sw.js",
        "/offline.html",
        "/manifest.webmanifest",
        "/instructions",
        # Только админ, и это решает отдельная проверка выше по коду.
        "/settings",
        "/remnawave",
        "/panels",
        "/bulk-actions",
        # Конечная точка API — свой blueprint со своей проверкой входа.
        "/api",
    }
    """Пути, которых в карте разделов быть и не должно."""

    def _guard(self) -> str:
        return _between(self.RUN, "_path_to_section = [", "req_section = None")

    def _mapped_prefixes(self) -> set[str]:
        return {prefix for prefix, _section in self._MAPPING.findall(self._guard())}

    def _routes(self) -> set[str]:
        """Пути страниц админки так, как их видит страж: целиком."""
        routes: set[str] = set()
        for path in [*(ADMIN / "routes").rglob("*.py"), ADMIN / "run.py"]:
            source = path.read_text(encoding="utf-8", errors="replace")
            for route in self._ROUTE_PATH.findall(source):
                if "__placeholder__moved" in route:
                    # Мёртвая заглушка от переехавшего маршрута.
                    continue
                if route == "/":
                    # Корень — сам дашборд, у него отдельная ветка в страже.
                    continue
                if any(part in route for part in self.OUTSIDE_THE_MAP):
                    continue
                routes.add(route)
        return routes

    def test_every_section_is_mapped(self):
        mapped = self._mapped_prefixes()
        unmapped = sorted(
            route for route in self._routes() if not any(prefix in route for prefix in mapped)
        )
        assert not unmapped, (
            "эти разделы не знает проверка прав модератора, "
            f"и он попадёт в них по прямой ссылке: {unmapped}"
        )

    def test_unknown_path_is_denied_not_shown(self):
        """Незнакомый путь — отказ, а не дашборд.

        Пока запасным был `dashboard`, любая новая страница открывалась всем
        модераторам в тот же день, когда её написали.
        """
        tail = _between(self.RUN, "req_section = None", "g.moderator_visible_sections = None")
        assert "req_section = 'dashboard'  # корневая страница" not in tail, (
            "запасной раздел `dashboard` открывает модератору всё незнакомое"
        )
        assert "403" in tail
        # Сам дашборд при этом остаётся: закрыть его значило бы не пустить
        # модератора даже на первую страницу.
        assert "ADMIN_SECRET_PATH" in tail and "req_section = 'dashboard'" in tail

    def test_transferred_sections_are_not_grantable(self):
        """Их разделов нет в правах модератора — и выдать их нельзя.

        За этими страницами root-пароль роутера, консоль и списки доменов
        для всего парка: это работа админа, а не «ещё один раздел».
        """
        settings_page = (TEMPLATES / "settings_general.html").read_text(
            encoding="utf-8", errors="replace"
        )
        mapping = dict(self._MAPPING.findall(self._guard()))
        for path in ("/routers", "/stock", "/catalog", "/orders", "/lists"):
            section = mapping.get(path)
            assert section is not None, f"{path} не знает проверка прав"
            assert f'value="{section}"' not in settings_page, (
                f"раздел {section} можно выдать модератору с формы прав"
            )
