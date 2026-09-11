"""Колонка «Остаток» в админке показывает предел продаж, а не полку.

Остаток считается по живым заказам. В списке товаров стояло число со
склада — и роутер, разобранный заказами, показывался как «3 шт.», пока
витрина писала «нет в наличии». Оператор видел товар в наличии и не
понимал, почему его не покупают.

Шаблон живёт в их админке; поднимать её целиком ради одной колонки незачем,
поэтому рисуем ту же ячейку отдельно — из того же файла.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from jinja2 import Environment

TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "bot"
    / "web_admin"
    / "templates"
    / "catalog_shop.html"
).read_text(encoding="utf-8")


def _cell_source() -> str:
    """Та самая ячейка из шаблона — по её собственному комментарию."""
    head = TEMPLATE.index("Остаток — предел продаж")
    start = TEMPLATE.rindex("{#", 0, head)
    end = TEMPLATE.index("</td>", start) + len("</td>")
    return TEMPLATE[start:end]


def _render(**item) -> str:
    template = Environment(autoescape=True).from_string(_cell_source())
    text = template.render(item=item)
    # Ячейка свёрстана в несколько строк — сравнивать удобнее одной.
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def test_free_units_are_the_number_shown():
    assert _render(stock=5, stock_left=2, allow_preorder=False) == "2 шт. из 5"


def test_nothing_extra_when_nothing_is_taken():
    """«3 шт. из 3» — лишний шум: ничего не продано."""
    assert _render(stock=3, stock_left=3, allow_preorder=False) == "3 шт."


def test_a_router_taken_by_orders_says_so():
    """Раньше здесь стояло «3 шт.» — оператор искал причину не там."""
    shown = _render(stock=3, stock_left=0, allow_preorder=False)
    assert "разобран" in shown
    assert "3 на складе" in shown


def test_an_empty_shelf_is_plain_missing():
    assert _render(stock=0, stock_left=0, allow_preorder=False) == "нет"


def test_preorder_wins_over_an_empty_shelf():
    assert _render(stock=0, stock_left=0, allow_preorder=True) == "под заказ"


@pytest.mark.parametrize("stock", [0, 4])
def test_without_a_counted_limit_the_shelf_decides(stock):
    """Предел мог не приехать — тогда судим по складу, как было до счёта."""
    shown = _render(stock=stock, stock_left=None, allow_preorder=False)
    assert shown == (f"{stock} шт." if stock else "нет")
