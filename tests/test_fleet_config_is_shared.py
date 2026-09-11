"""Адрес нашего API и токен к нему читаются одним кодом на обе службы.

К нам ходят двое: бот — за каталогом и заказами, админка — за парком
роутеров. Обе читали `FLEET_API_URL` и `FLEET_API_TOKEN` своим кодом, слово
в слово одинаковым. Допиши кто-нибудь запасное имя переменной или другую
обрезку хвостового слэша — и одна половина продолжила бы работать, а вторая
начала бы отвечать «токен не подошёл». Искать такое пришлось бы в двух
файлах, ни один из которых не выглядит виноватым.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parents[1] / "bot"
SHOP_API = (BOT / "src" / "shop_api.py").read_text(encoding="utf-8")
FLEET_PAGE = (BOT / "web_admin" / "routes" / "routers_fleet.py").read_text(encoding="utf-8")


def test_the_admin_page_borrows_the_reader():
    assert "from src.shop_api import fleet_config" in FLEET_PAGE


def test_it_does_not_read_the_variables_itself():
    """Своё чтение — это и есть то, что разъезжается.

    Имя переменной в файле остаться может: оно стоит в тексте подсказки
    оператору, «FLEET_API_TOKEN здесь и API_FLEET_TOKEN там должны
    совпадать». Ищем именно чтение окружения.
    """
    assert "getenv" not in FLEET_PAGE


def test_the_old_private_name_still_works():
    """Им пользуется сам `shop_api` — ломать его переименованием незачем."""
    assert "_config = fleet_config" in SHOP_API


@pytest.mark.parametrize("variable", ["FLEET_API_URL", "FLEET_API_TOKEN"])
def test_each_variable_is_read_in_exactly_one_place(variable):
    places = [
        f"{path.relative_to(BOT)}:{number}"
        for path in sorted(BOT.rglob("*.py"))
        if "__pycache__" not in path.parts
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1)
        if f'getenv("{variable}"' in line or f"getenv('{variable}'" in line
    ]
    assert len(places) == 1, f"{variable} читается в {places}"


def test_the_reader_is_still_a_pair_of_strings():
    """Оба зовущих распаковывают её в две переменные."""
    tree = ast.parse(SHOP_API)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "fleet_config":
            assert ast.unparse(node.returns) == "tuple[str, str]"
            return
    raise AssertionError("fleet_config не нашёлся")
