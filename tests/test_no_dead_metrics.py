"""Объявленная метрика должна кем-то заполняться.

Счётчик, который никто не трогает, показывает вечный ноль. На графике это
читается как «активаций не было, всё спокойно», а не как «мы их не считаем»
— и такой график хуже отсутствующего: он отвечает на вопрос, которого ему
не задавали.

Шесть из девятнадцати оказались такими. Две заполнили (показания роутеров и
попытки активации), четыре убрали: счётчики бота — своего бота у нас нет, а
чужой до `core` не дотягивается; обращения роутеров за подпиской — они ходят
прямо в панель; сообщения рассылок — рассылок нет.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
METRICS_FILE = ROOT / "core" / "metrics.py"
LINES = METRICS_FILE.read_text(encoding="utf-8").splitlines()

DECLARED = {
    found.group(1): number
    for number, line in enumerate(LINES, 1)
    if (found := re.match(r"(\w+)\s*=\s*(?:Counter|Gauge|Histogram)\(", line))
}


def _everything_else() -> str:
    """Весь код, кроме строк объявления: там имя стоит и в названии метрики."""
    parts = [
        "\n".join(line for number, line in enumerate(LINES, 1) if number not in DECLARED.values())
    ]
    for tree in ("core", "api", "worker", "bot"):
        for path in sorted((ROOT / tree).rglob("*.py")):
            if "__pycache__" in path.parts or path.name == "metrics.py":
                continue
            parts.append(path.read_text(encoding="utf-8-sig"))
    return "\n".join(parts)


HAYSTACK = _everything_else()


def test_there_are_metrics_to_check():
    assert len(DECLARED) >= 10


@pytest.mark.parametrize("name", sorted(DECLARED))
def test_somebody_fills_it(name):
    assert re.search(rf"\b{name}\b", HAYSTACK), (
        f"{name} объявлена и никем не обновляется — на графике будет вечный ноль. "
        "Заполните её или уберите."
    )
