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
