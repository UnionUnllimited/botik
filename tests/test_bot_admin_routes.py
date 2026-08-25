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

import pytest

ADMIN = Path(__file__).resolve().parents[1] / "bot" / "web_admin"
TEMPLATES = ADMIN / "templates"

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
