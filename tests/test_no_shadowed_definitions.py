"""Функция, объявленная в файле дважды, — это ловушка.

Питон оставляет последнюю, а первая остаётся лежать в файле и выглядит
рабочей. В `db_helpers` их было четыре, и среди них `get_active_subscription`
и `get_last_subscription` — те самые, через которые бот решает, продлевать
подписку или заводить новую. Правка в верхней копии не давала ничего, и
понять почему, читая файл сверху вниз, нельзя.

Проверка на весь код: и наш, и их. Файл на три тысячи строк ловит такое
только так — глазами двойное объявление не видно никогда.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TREES = ("core", "api", "worker", "bot")


def _modules() -> list[Path]:
    found: list[Path] = []
    for tree in TREES:
        found += [
            path
            for path in sorted((ROOT / tree).rglob("*.py"))
            if "__pycache__" not in path.parts
        ]
    return found


def _shadowed(path: Path) -> list[str]:
    """Имена, объявленные в модуле или классе больше одного раза."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        # Чужой файл может быть под другую версию питона: не наше дело.
        return []

    def clashes(body) -> list[str]:
        seen: defaultdict[str, int] = defaultdict(int)
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                # Перегрузки и «одна ветка на платформу» пишутся декоратором
                # или внутри `if`, а сюда попадает только прямое объявление.
                if any(
                    "overload" in ast.unparse(d) or "setter" in ast.unparse(d)
                    for d in node.decorator_list
                ):
                    continue
                seen[node.name] += 1
        return sorted(name for name, count in seen.items() if count > 1)

    names = clashes(tree.body)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            names += [f"{node.name}.{name}" for name in clashes(node.body)]
    return names


MODULES = _modules()


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(ROOT)))
def test_nothing_is_declared_twice(path: Path):
    shadowed = _shadowed(path)
    assert not shadowed, (
        f"{path.relative_to(ROOT)}: объявлено дважды — {shadowed}. "
        f"Работает последнее объявление, первое лежит мёртвым и вводит в "
        f"заблуждение. Оставьте одно."
    )


def test_there_are_modules_to_check():
    """Сломайся обход — проверка выше прошла бы на пустом списке."""
    assert len(MODULES) > 100
