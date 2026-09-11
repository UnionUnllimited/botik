"""Даты человеку показываются по Москве, а не в UTC.

Внутри всё живёт в UTC — и правильно. Но оператор и клиент читают даты
глазами, и с девяти вечера до полуночи московская дата уже следующая.
Журнал роутера писал «срок продлён до 11.10», а клиент в приложении в ту же
секунду видел «до 12.10»; в утренней сводке оператор получал дату на сутки
раньше и звонил не в тот день.

Тем же промахом в сводке считались и дни: `(expires_at - now).days` вместо
общего `days_left`, по которому уходят напоминания клиенту.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from core.dates import days_left, to_display

ROOT = Path(__file__).resolve().parents[1]


def _body(path: str, name: str) -> str:
    source = (ROOT / path).read_text(encoding="utf-8")
    head = source.index(f"async def {name}(")
    rest = source[head + 1 :]
    ends = [rest.index(mark) for mark in ("\ndef ", "\nasync def ") if mark in rest]
    return rest[: min(ends)] if ends else rest


class TestWhereTheOperatorReadsDates:
    @pytest.mark.parametrize(
        ("path", "name"),
        [
            ("core/services/activation.py", "activate_manually"),
            ("core/services/activation.py", "sync_panel_expiry"),
            ("worker/tasks/monitoring.py", "daily_digest"),
        ],
    )
    def test_nothing_is_printed_straight_from_utc(self, path, name):
        body = _body(path, name)
        for line in body.splitlines():
            if "%d.%m" not in line:
                continue
            assert "to_display" in line or "_msk(" in line, line.strip()


def test_an_evening_in_moscow_is_already_tomorrow():
    """Ровно тот случай, ради которого перевод и нужен."""
    late = dt.datetime(2026, 10, 11, 22, 0, tzinfo=dt.UTC)

    assert late.strftime("%d.%m") == "11.10"
    assert to_display(late).strftime("%d.%m") == "12.10"


def test_the_digest_counts_days_the_same_way_as_reminders():
    """Раньше здесь было своё вычитание — и оно расходилось с напоминаниями."""
    source = (ROOT / "worker" / "tasks" / "monitoring.py").read_text(encoding="utf-8")
    assert "days_left(subscription.expires_at" in source
    assert "(subscription.expires_at - now).days" not in source


def test_the_shared_counter_is_by_full_days():
    """Подстраховка: на это опираются и сводка, и напоминания, и экран."""
    now = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC)
    assert days_left(now + dt.timedelta(hours=84), now=now) == 3
    assert days_left(now + dt.timedelta(hours=6), now=now) == 0
