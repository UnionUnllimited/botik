"""Лимит устройств не должен попадать в названия сроков подписки.

Он достался от подписки для телефона, где слоты продавали поштучно.
За роутером сидит вся домашняя сеть, и «30 дней | 1 уст.» на кнопке продления
клиент читает как «работать будет одно устройство» — то есть как обман.

Срезается на переносе тарифов, а не правкой названий руками: тарифы заводят
в их админке, и следующий появился бы с тем же хвостом.
"""

from __future__ import annotations

import ast
import pathlib
import re
import types

import pytest

SHOP_SYNC = pathlib.Path(__file__).resolve().parents[1] / "bot" / "src" / "shop_sync.py"


def _load_stripper():
    """Берёт из модуля только регулярку и функцию: остальное тянет базу бота."""
    tree = ast.parse(SHOP_SYNC.read_text(encoding="utf-8"))
    wanted = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name == "strip_device_limit")
        or (
            isinstance(node, ast.Assign)
            and getattr(node.targets[0], "id", "") == "_DEVICE_LIMIT"
        )
    ]
    module = types.ModuleType("shop_sync_under_test")
    module.re = re
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "<test>", "exec"), module.__dict__)  # noqa: S102
    return module.strip_device_limit


strip_device_limit = _load_stripper()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("30 дней | 1 уст.", "30 дней"),
        ("90 дней | 3 уст.", "90 дней"),
        ("60 дней | 2 устройства", "60 дней"),
        ("180 дней | 1 устройство", "180 дней"),
        ("Год · 5 устройств", "Год"),
        ("1 уст. | 30 дней", "30 дней"),
    ],
)
def test_limit_is_cut(name, expected):
    assert strip_device_limit(name) == expected


@pytest.mark.parametrize("name", ["30 дней", "12 месяцев", "Тариф на 30 дней", "Полгода"])
def test_plain_names_survive(name):
    assert strip_device_limit(name) == name


def test_word_is_not_cut_in_half():
    """«уст» стояло в разборе раньше «устройства» и оставляло хвост «ройство»."""
    assert "ройств" not in strip_device_limit("180 дней | 1 устройство")


def test_name_of_only_a_limit_becomes_empty():
    """Тариф, названный одним лимитом, теряет имя — вызывающий подставит срок."""
    assert strip_device_limit("1 уст.") == ""
