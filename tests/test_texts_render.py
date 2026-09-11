"""Подстановки в текстах сходятся с тем, что в них передают.

Шаблон с лишней фигурной скобкой не падает при правке — он падает при
отправке, в тот момент, когда клиенту нужно сказать «оплата получена».
Ошибка выглядит как `KeyError: 'days'` в глубине обработчика, и найти её
оттуда трудно: в тексте она видна, а в трассировке нет.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import core.texts as texts

ROOT = Path(__file__).resolve().parents[1]

TEMPLATES = {
    name: value
    for name, value in vars(texts).items()
    if name.isupper() and isinstance(value, str) and "{" in value
}


def _format_calls():
    """Где эти шаблоны подставляются: файл, строка, имя, переданные ключи."""
    for tree_name in ("core", "api", "worker"):
        for path in sorted((ROOT / tree_name).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr != "format":
                    continue
                target = node.func.value
                name = getattr(target, "attr", None) or getattr(target, "id", None)
                if name in TEMPLATES:
                    keys = {kw.arg for kw in node.keywords if kw.arg}
                    yield f"{path.relative_to(ROOT)}:{node.lineno}", name, keys


CALLS = list(_format_calls())


def test_there_are_templates_and_calls_to_check():
    """Сломайся разбор — проверки ниже прошли бы на пустоте."""
    assert len(TEMPLATES) >= 10
    assert len(CALLS) >= 10


@pytest.mark.parametrize(("where", "name", "keys"), CALLS, ids=[c[0] for c in CALLS])
def test_every_call_passes_what_the_text_asks(where, name, keys):
    needed = set(re.findall(r"\{(\w+)", TEMPLATES[name]))
    assert not (needed - keys), f"{where}: {name} просит {sorted(needed - keys)}"
    assert not (keys - needed), f"{where}: {name} не знает {sorted(keys - needed)}"


@pytest.mark.parametrize("status", list(texts.ORDER_STATUS_TEXTS))
def test_status_texts_ask_only_for_what_the_handler_has(status):
    """Обработчик подставляет ровно номер и причину, больше у него ничего нет."""
    needed = set(re.findall(r"\{(\w+)", texts.ORDER_STATUS_TEXTS[status]))
    assert not (needed - {"number", "reason"}), sorted(needed)


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_template_is_renderable(name):
    """Ловит и незакрытую скобку, и `{}` без имени — оба падают при отправке."""
    template = TEMPLATES[name]
    values = dict.fromkeys(re.findall(r"\{(\w+)", template), "…")
    rendered = template.format(**values)
    assert rendered, name
