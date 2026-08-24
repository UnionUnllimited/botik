"""Перепосев главного меню должен догонять любой прошлый дефолт.

Тексты и подписи кнопок лежат в базе бота: код задаёт их только при создании
базы, дальше правит оператор. Поэтому редизайн доезжает до сервера не правкой
кода, а перепосевом — обновлением тех строк, которых оператор не касался.

Первый заход этого не сделал: он искал только предыдущий дефолт, а на сервере
лежал самый первый. Совпадений не нашлось, отметка «применено» встала, и
интерфейс остался прежним. Отсюда два требования: перечислять **все** прошлые
дефолты и менять номер отметки, когда список пополняется.
"""

from __future__ import annotations

import re
from pathlib import Path

BOT = Path(__file__).resolve().parents[1] / "bot"
DB_HELPERS = (BOT / "db_helpers.py").read_text(encoding="utf-8")

ORIGINAL_WELCOME = (
    "Здравствуйте, {user_name}! Это {project_name} — роутеры с подпиской "
    "на сервис стабильного доступа к зарубежным ресурсам. Выберите раздел ниже."
)


def _reseed_block() -> str:
    start = DB_HELPERS.index("legacy_menu_defaults = {")
    return DB_HELPERS[start : DB_HELPERS.index("new_menu_defaults =")]


def _literals(block: str) -> str:
    """Склеиваем строковые куски так же, как это сделает Python."""
    return "".join(re.findall(r"'([^']*)'", block))


class TestEveryOldDefaultIsListed:
    def test_the_very_first_welcome_is_there(self):
        """Именно он стоит на боевом сервере — без него перепосев впустую."""
        assert ORIGINAL_WELCOME in _literals(_reseed_block())

    def test_values_are_tuples_not_single_strings(self):
        """Дефолт менялся не раз; один на ключ обновит только один стенд."""
        block = _reseed_block()
        assert "legacy_values" in DB_HELPERS
        assert "for legacy_value in legacy_values" in DB_HELPERS
        assert block.count("(") >= 4


class TestMarkForcesRerun:
    def test_mark_is_versioned(self):
        """Прошлая отметка уже стоит на сервере: без нового номера круг
        не пройдёт заново и правка ничего не изменит."""
        assert "ui_redesign_2026_08_menu_v2" in DB_HELPERS

    def test_mark_write_survives_an_existing_row(self):
        """Строка с прошлым номером уже есть; обычный INSERT упал бы
        на уникальном ключе и уронил создание базы."""
        block = DB_HELPERS[DB_HELPERS.index("menu_redesign_mark = ") :]
        block = block[: block.index("Редизайн главного меню: нетронутые")]
        assert "INSERT OR REPLACE" in block


class TestOperatorEditsSurvive:
    def test_update_is_conditional(self):
        """Обновляем только строки, равные прошлому дефолту: правленный
        оператором текст трогать нельзя ни при каком редизайне."""
        assert "UPDATE settings SET value = ? WHERE key = ? AND value = ?" in DB_HELPERS


class TestButtonsHaveTheirOwnReset:
    def test_reset_endpoint_exists(self):
        """Подписи кнопок перепосевом не трогаются вовсе — у них своя
        кнопка сброса, и вызывать её должен человек."""
        settings = (BOT / "web_admin" / "routes" / "settings.py").read_text(encoding="utf-8")
        assert "settings_buttons_reset" in settings
        assert "default_text" in settings
