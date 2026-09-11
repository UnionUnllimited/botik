"""Состояния называются словами, и всюду одними и теми же.

Словарей два, и это намеренно: их админка живёт в другом процессе, с другим
venv, и до `core` не дотягивается. Но разъехаться они не должны — оператор
видит обе страницы в одной панели, а клиент читает те же слова в боте.

Опаснее другое: новое состояние, забытое в словаре. Тогда оператору вылезает
сырой код — `revoked`, `awaiting_payment`, — и он идёт спрашивать, что это.
В коде про это уже написано: «иначе `active` рано или поздно вылезет
оператору — так и вышло».
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.enums import DeviceStatus, OrderStatus, SubscriptionStatus

ROOT = Path(__file__).resolve().parents[1]


def _mapping(path: str, name: str) -> dict[str, str]:
    """Словарь-литерал из файла, который нельзя импортировать."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))
    for node in tree.body:
        target = node.targets[0] if isinstance(node, ast.Assign) and node.targets else None
        if getattr(target, "id", "") != name:
            continue
        out: dict[str, str] = {}
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            # Ключ бывает строкой ("paid") и членом енума (OrderStatus.PAID).
            plain = key.value if isinstance(key, ast.Constant) else key.attr.lower()
            out[str(plain)] = value.value
        return out
    raise AssertionError(f"{name} не нашёлся в {path}")


ORDERS_OURS = _mapping("core/texts.py", "ORDER_STATUS_TITLES")
ORDERS_THEIRS = _mapping("bot/web_admin/routes/orders_shop.py", "STATUS_TITLES")
DEVICES_OURS = _mapping("api/routes/fleet_api.py", "DEVICE_LABELS")
DEVICES_THEIRS = _mapping("bot/web_admin/routes/devices_stock.py", "STATUS_TITLES")
SUBSCRIPTIONS = _mapping("api/routes/fleet_api.py", "SUBSCRIPTION_LABELS")


class TestNothingIsLeftWithoutAName:
    """Забытое состояние вылезает оператору сырым кодом."""

    @pytest.mark.parametrize("status", [str(member) for member in OrderStatus])
    def test_every_order_status_is_named_on_both_sides(self, status):
        assert status in ORDERS_OURS, "нет в core/texts.py"
        assert status in ORDERS_THEIRS, "нет в админке бота"

    @pytest.mark.parametrize("status", [str(member) for member in DeviceStatus])
    def test_every_device_status_is_named_on_both_sides(self, status):
        assert status in DEVICES_OURS, "нет в fleet_api"
        assert status in DEVICES_THEIRS, "нет в складской вкладке"

    @pytest.mark.parametrize("status", [str(member) for member in SubscriptionStatus])
    def test_every_subscription_status_is_named(self, status):
        assert status in SUBSCRIPTIONS


class TestTheTwoVocabulariesAgree:
    def test_orders_are_called_the_same_word_for_word(self):
        """Один заказ в двух вкладках одной панели — одно название."""
        assert ORDERS_OURS == ORDERS_THEIRS

    def test_routers_are_called_the_same_by_meaning(self):
        """Регистр у них свой: у нас слово стоит в строке, у них в ячейке.

        Сравниваем без него — важно, что состояние не называется двумя
        разными словами. «Изъят» на одной вкладке и «Отвязано» на другой
        оператор читает как два разных состояния одного роутера.
        """
        for status, ours in DEVICES_OURS.items():
            theirs = DEVICES_THEIRS[status]
            root = theirs.lower().rstrip("оаы")[:6]
            assert root in ours.lower(), f"{status}: «{ours}» против «{theirs}»"

    def test_nobody_shows_a_raw_code(self):
        """Название не должно совпадать с самим кодом."""
        for vocabulary in (ORDERS_OURS, ORDERS_THEIRS, DEVICES_OURS, DEVICES_THEIRS, SUBSCRIPTIONS):
            for status, title in vocabulary.items():
                assert title.lower() != status, f"{status} осталось без перевода"
