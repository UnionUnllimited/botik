"""Цена выглядит одинаково в письме, в боте и в приложении.

Формат денег написан трижды: в `core/texts` (письма и карточки заказов), в
каталоге бота и в приложении. Расходились они на копейках: приложение
показывало «8 900,5 ₽» — одна цифра вместо двух, — а бот в тот же момент
«8 900,50 ₽». Обрезанное число читается как ошибка счёта, а не как половина
рубля.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import types
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest

from core.texts import money as money_core

ROOT = Path(__file__).resolve().parents[1]

NBSP = " "
"""Неразрывный пробел между тысячами — его ставят все трое."""

CASES = [
    "0",
    "149.50",
    "1990",
    "8900.00",
    "8900.05",
    "8900.50",
    "12345678.00",
]


def _bot_money():
    """`router_catalog` тянет окружение бота — подсовываем заглушки."""
    import importlib.util

    stubs = {
        "src": types.ModuleType("src"),
        "src.shop_api": types.ModuleType("src.shop_api"),
        "app_config": types.ModuleType("app_config"),
        "button_helpers": types.ModuleType("button_helpers"),
        "keyboards": types.ModuleType("keyboards"),
        "db_helpers": types.ModuleType("db_helpers"),
        "loguru": types.ModuleType("loguru"),
    }
    stubs["src"].__path__ = [str(ROOT / "bot" / "src")]
    stubs["app_config"].app_conf = types.SimpleNamespace(get=lambda *_a, **_k: "")
    stubs["button_helpers"].btn = lambda *_a, **_k: None
    stubs["loguru"].logger = types.SimpleNamespace(
        info=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
        debug=lambda *_a, **_k: None,
    )

    saved = {key: sys.modules.get(key) for key in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(
            "_catalog_money_probe", ROOT / "bot" / "src" / "router_catalog.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.money
    finally:
        for key, was in saved.items():
            if was is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = was


def _app_money() -> dict[str, str]:
    """Гоняем ту же функцию из приложения — она на другом языке."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node не установлен — сравнить с приложением нечем")
    script = (
        "const fs=require('fs');"
        f"const src=fs.readFileSync({json.dumps(str(ROOT / 'api/static/miniapp/app.js'))},'utf8');"
        "const head=src.indexOf('function money(value, currency)');"
        "const body=src.slice(head);"
        "const fn=new Function('return '+body.slice(0, body.indexOf('\\n  }')+4))();"
        f"const cases={json.dumps(CASES)};"
        "const out={};for(const v of cases){out[v]=fn(v);}"
        "process.stdout.write(JSON.stringify(out));"
    )
    done = subprocess.run(  # noqa: S603 — свой же файл, свой же node
        [node, "-e", script],
        capture_output=True,
        text=True,
        # Без этого вывод читается кодировкой системы: на Windows знак рубля
        # превращается в «в‚Ѕ», а неразрывный пробел — в два знака, и сравнение
        # падает на разнице, которой нет.
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    return json.loads(done.stdout)


def _admin_money():
    """Фильтр их админки. Файл большой и тянет Quart — берём одну функцию.

    Разбираем исходником: `run.py` при импорте поднимает всё приложение.
    """
    import ast

    source = (ROOT / "bot" / "web_admin" / "run.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "money_filter":
            node.decorator_list = []
            module = ast.Module(body=[node], type_ignores=[])
            scope: dict = {"Decimal": Decimal, "InvalidOperation": InvalidOperation}
            exec(compile(module, "<money_filter>", "exec"), scope)  # noqa: S102 — свой же файл
            return scope["money_filter"]
    raise AssertionError("money_filter не нашёлся")


# Модули поднимаются небыстро — поднимаем их один раз на файл.
MONEY_BOT = _bot_money()
MONEY_ADMIN = _admin_money()


@pytest.mark.parametrize("raw", CASES)
def test_the_bot_and_the_letters_agree(raw):
    assert MONEY_BOT(raw) == money_core(Decimal(raw))


@pytest.mark.parametrize("raw", CASES)
def test_the_admin_panel_agrees_too(raw):
    """Её докстрока прямо обещает тот же формат — пусть обещание держится."""
    assert MONEY_ADMIN(raw) == money_core(Decimal(raw))


def test_the_app_agrees_with_the_rest():
    """Сравниваем знак в знак, включая неразрывный пробел между тысячами."""
    shown = _app_money()
    for raw in CASES:
        assert shown[raw] == money_core(Decimal(raw)), raw


def test_the_separator_is_unbreakable_everywhere():
    """Обычный пробел делал ту же цену разной строкой в трёх местах.

    На вид он тот же, но перенос строки посреди числа им не разорвать —
    и «8» на одной строке, «900 ₽» на другой оператор читает как две суммы.
    """
    assert " " in money_core(Decimal("8900.00"))
    assert " " in MONEY_BOT("8900.00")


def test_kopecks_are_both_or_none():
    """Половина рубля — «,50», а не «,5»."""
    assert money_core(Decimal("8900.50")) == f"8{NBSP}900,50 ₽"
    assert money_core(Decimal("8900.00")) == f"8{NBSP}900 ₽"


def test_the_app_asks_for_both_digits():
    """Проверка по исходнику на случай, если node недоступен."""
    source = (ROOT / "api" / "static" / "miniapp" / "app.js").read_text(encoding="utf-8")
    head = source.index("function money(value, currency)")
    body = source[head : source.index("\n  //", head)]
    assert "minimumFractionDigits: cents" in body
    assert "minimumFractionDigits: 0," not in body
