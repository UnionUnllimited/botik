"""Массовые операции их админки обходят роутерных клиентов.

«Удалить без ключа» отбирает по пустому их xui_client_uuid — он пуст
у каждого роутерного клиента, и одно нажатие снесло бы всех. «Удалить
истёкших» смотрит на срок, который у роутерных приезжает зеркалом, —
и снёс бы тех, кому срок просто ещё не продлили в магазине.

Проверка по исходнику: модуль тянет их db_helpers, а тот при импорте
прогоняет миграцию боевой базы.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parents[1] / "bot"
BULK = (BOT / "web_admin" / "core" / "bulk_ops.py").read_text(encoding="utf-8")
USERS = (BOT / "web_admin" / "routes" / "users.py").read_text(encoding="utf-8")


def _function(source: str, name: str) -> str:
    start = source.index(f"async def {name}(")
    tail = source[start + 1 :]
    match = re.search(r"\n(async )?def ", tail)
    return tail[: match.start()] if match else tail


@pytest.mark.parametrize(
    "name",
    ["count_expired_users", "fetch_expired_user_ids",
     "count_empty_uuid_users", "fetch_empty_uuid_user_ids"],
)
def test_bulk_query_leaves_shop_clients_alone(name):
    """Счётчик и выборка одной операции обязаны фильтровать одинаково:
    иначе кнопка обещает одно число, а удаляет другое."""
    assert "_not_shop_client_filter()" in _function(BULK, name)


def test_the_filter_checks_both_marks():
    """Флаг срока и ключ учётки: любого из них хватает, чтобы клиент был
    роутерным, и терять его по отсутствию второго нельзя."""
    body = _function(BULK, "_not_shop_client_filter") if "async def _not_shop_client_filter" in BULK \
        else BULK[BULK.index("def _not_shop_client_filter"):]
    assert "shop_subscription" in body
    assert "shop_panel_short_uuid" in body


@pytest.mark.parametrize("name", ["renew_subscription", "reduce_subscription"])
def test_manual_term_changes_refuse_shop_clients(name):
    """grant_subscription завёл бы роутерному клиенту телефонную учётку,
    а зеркало через круг вернуло бы прежний срок."""
    assert "_is_shop_client(telegram_id)" in _function(USERS, name)
