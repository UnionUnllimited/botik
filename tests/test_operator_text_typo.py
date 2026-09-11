"""Опечатка в тексте на странице админки не роняет экран.

Тексты бота правятся оператором на странице «Тексты». Подстановки в них
пишутся фигурными скобками, и опечатка — `{имя}` вместо `{user_name}`,
лишняя `{` — это не кривая строка, а `KeyError` в момент показа.

Дороже всего два места. Приветствие в главном меню: `/start` перестаёт
работать у всех клиентов сразу, и починить его можно только через базу — до
самой страницы с текстами уже не дойти. И сообщение после оплаты: деньги
взяты, а подтверждения нет.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parents[1] / "bot"


def _all(haystack: str, needle: str) -> list[int]:
    found, at = [], haystack.find(needle)
    while at != -1:
        found.append(at)
        at = haystack.find(needle, at + 1)
    return found


@pytest.fixture(scope="module")
def utils():
    """Модуль их текстов — там же и подстановка. Ничего не тянет за собой."""
    spec = importlib.util.spec_from_file_location(
        "_bot_texts_probe", BOT / "src" / "texts.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WELCOME = "Здравствуйте, {user_name}! Это {project_name}."


class TestWhenTheTextIsFine:
    def test_it_is_used_as_written(self, utils):
        shown = utils.format_text_template(WELCOME, "запасной", user_name="Иван", project_name="Магазин")
        assert shown == "Здравствуйте, Иван! Это Магазин."


class TestWhenTheOperatorMistyped:
    @pytest.mark.parametrize(
        "broken",
        [
            "Здравствуйте, {имя}!",  # русское имя переменной
            "Здравствуйте, {user_name!",  # незакрытая скобка
            "Здравствуйте, {}!",  # без имени вовсе
            "Скидка {0} процентов",  # позиционная подстановка
        ],
    )
    def test_the_default_saves_the_screen(self, utils, broken):
        shown = utils.format_text_template(broken, WELCOME, user_name="Иван", project_name="Магазин")
        assert shown == "Здравствуйте, Иван! Это Магазин."

    def test_an_empty_setting_falls_back_too(self, utils):
        """Настройку можно стереть — тогда берём умолчание из кода."""
        shown = utils.format_text_template("", WELCOME, user_name="Иван", project_name="Магазин")
        assert shown == "Здравствуйте, Иван! Это Магазин."

    def test_nothing_returns_empty(self, utils):
        """Пустой экран читается так же плохо, как ошибка."""
        assert utils.format_text_template("{нет}", "{тоже нет}", user_name="Иван")


class TestWhereItIsUsed:
    SOURCE = (BOT / "main.py").read_text(encoding="utf-8-sig")

    @pytest.mark.parametrize(
        "setting",
        [
            "text_welcome_message",
            "text_subscription_info",
            "text_about_service",
            "text_promo_code_success",
        ],
    )
    def test_the_text_is_substituted_safely(self, setting):
        """Имя настройки встречается и в списках — смотрим все вхождения."""
        places = [
            self.SOURCE[max(0, at - 500) : at + 500]
            for at in _all(self.SOURCE, f"'{setting}'")
        ]
        assert places, setting
        assert any("format_text_template" in place for place in places), setting

    def test_nothing_formats_an_operator_text_unguarded(self):
        """Проверка на весь файл: седьмое такое место забыли бы наверняка."""
        tree = ast.parse(self.SOURCE)
        guarded = {
            inner.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Try)
            for inner in ast.walk(node)
            if hasattr(inner, "lineno")
        }
        left = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
            and (
                "app_conf.get" in ast.unparse(node.func.value)
                or ast.unparse(node.func.value) in ("tpl", "_tpl")
            )
            and node.lineno not in guarded
        ]
        assert not left, f"строки без защиты: {left}"
