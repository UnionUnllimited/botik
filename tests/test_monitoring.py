"""Сводка оператору: правила отбора и текст.

Сводку читают утром и по ней звонят. Лишняя строка стоит звонка клиенту,
у которого всё в порядке; пропущенная — клиента, который звонит сам.
"""

from __future__ import annotations

import pytest

from core.texts import _plural_days, fleet_digest


class TestPluralDays:
    """Дни склоняются: «9 дней в пути» читается, «9 день» — спотыкается."""

    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (1, "1 день"),
            (2, "2 дня"),
            (4, "4 дня"),
            (5, "5 дней"),
            (11, "11 дней"),
            (12, "12 дней"),
            (14, "14 дней"),
            (21, "21 день"),
            (22, "22 дня"),
            (25, "25 дней"),
            (101, "101 день"),
            (111, "111 дней"),
        ],
    )
    def test_forms(self, days, expected):
        assert _plural_days(days) == expected


class TestDigestText:
    def test_empty_digest_has_only_the_heading(self):
        """Пустую сводку не отправляют, но собраться она должна без падения."""
        text = fleet_digest(silent=[], shipped_silent=[], expiring=[])
        assert "Парк роутеров" in text
        assert "Молчат" not in text

    def test_each_line_has_something_to_grab(self):
        """В строке нужен MAC, имя или номер заказа — по ней будут звонить."""
        text = fleet_digest(
            silent=[("D4:0D:AB:28:3B:80", "Union", "12.08 в 14:20")],
            shipped_silent=[("R-1042", "D4:0D:AB:03:4B:CE", 9)],
            expiring=[("Титан Карл Иванович", "16.08", 2)],
        )
        assert "D4:0D:AB:28:3B:80" in text
        assert "Union" in text
        assert "R-1042" in text
        assert "Титан Карл Иванович" in text

    def test_client_without_name_does_not_leave_a_dangling_separator(self):
        text = fleet_digest(
            silent=[("D4:0D:AB:28:3B:80", "", "12.08 в 14:20")], shipped_silent=[], expiring=[]
        )
        assert " · " not in text

    def test_long_lists_are_cut(self):
        """Сводка на тридцать строк не читается; первые десять отвечают
        на вопрос «стало хуже или как вчера»."""
        silent = [(f"AA:BB:CC:DD:EE:{i:02X}", "", "12.08 в 14:20") for i in range(25)]
        text = fleet_digest(silent=silent, shipped_silent=[], expiring=[])
        assert "Молчат больше суток</b> — 25" in text
        assert "…и ещё 15" in text
        assert text.count("AA:BB:CC:DD:EE:") == 10

    def test_only_present_sections_are_shown(self):
        """Раздел без поводов не печатается пустым заголовком."""
        text = fleet_digest(silent=[], shipped_silent=[("R-1", "AA:BB", 8)], expiring=[])
        assert "Отгружены" in text
        assert "Молчат больше суток" not in text
        assert "Подписка кончается" not in text


class TestThresholds:
    """Пороги — это решения, а не константы: их меняют, и надо понимать цену."""

    def test_silence_measured_in_a_full_day(self):
        """Роутер перезагружают и интернет моргает — часы это не повод."""
        from core.services.monitoring import SILENT_HOURS

        assert SILENT_HOURS >= 24

    def test_shipped_waits_longer_than_delivery(self):
        """СДЭК по стране идёт до пяти дней: раньше — это «посылка ещё едет»."""
        from core.services.monitoring import SHIPPED_SILENT_DAYS

        assert SHIPPED_SILENT_DAYS > 5
