"""Даты подписки: календарные месяцы, остаток дней, русские формулировки."""

from __future__ import annotations

import datetime as dt

import pytest

from core.dates import (
    add_months,
    add_period,
    days_left,
    days_phrase,
    ensure_utc,
    format_date_ru,
    format_datetime_ru,
    plural_ru,
    to_display,
)


class TestAddMonths:
    @pytest.mark.parametrize(
        ("start", "months", "expected"),
        [
            ("2026-01-15", 1, "2026-02-15"),
            ("2026-01-31", 1, "2026-02-28"),  # в феврале нет 31-го
            ("2028-01-31", 1, "2028-02-29"),  # високосный год
            ("2026-08-03", 12, "2027-08-03"),
            ("2026-12-15", 1, "2027-01-15"),  # переход через год
            ("2026-03-31", 6, "2026-09-30"),
            ("2026-08-03", 0, "2026-08-03"),
        ],
    )
    def test_calendar_arithmetic(self, start, months, expected):
        value = dt.datetime.fromisoformat(f"{start}T12:00:00+00:00")
        assert add_months(value, months).date().isoformat() == expected

    def test_year_of_months_does_not_drift(self):
        """12 продлений по месяцу должны дать ровно год, а не 360 дней."""
        value = dt.datetime(2026, 8, 3, 12, tzinfo=dt.UTC)
        for _ in range(12):
            value = add_months(value, 1)
        assert value == dt.datetime(2027, 8, 3, 12, tzinfo=dt.UTC)

    def test_time_of_day_is_preserved(self):
        value = dt.datetime(2026, 8, 3, 23, 47, 11, tzinfo=dt.UTC)
        assert add_months(value, 3).timetz() == value.timetz()


class TestAddPeriod:
    def test_months_and_bonus_days(self):
        start = dt.datetime(2026, 8, 3, 10, tzinfo=dt.UTC)
        assert add_period(start, months=12, days=30) == dt.datetime(2027, 9, 2, 10, tzinfo=dt.UTC)

    def test_days_only(self):
        start = dt.datetime(2026, 8, 3, 10, tzinfo=dt.UTC)
        assert add_period(start, days=3) == dt.datetime(2026, 8, 6, 10, tzinfo=dt.UTC)


class TestDaysLeft:
    now = dt.datetime(2026, 8, 3, 12, tzinfo=dt.UTC)

    @pytest.mark.parametrize(
        ("expires", "expected"),
        [
            (dt.datetime(2026, 8, 10, 12, tzinfo=dt.UTC), 7),
            (dt.datetime(2026, 8, 4, 12, tzinfo=dt.UTC), 1),
            (dt.datetime(2026, 8, 4, 11, tzinfo=dt.UTC), 0),  # меньше суток — уже 0
            (dt.datetime(2026, 8, 3, 12, tzinfo=dt.UTC), 0),
            (dt.datetime(2026, 8, 2, 12, tzinfo=dt.UTC), -1),
            (dt.datetime(2026, 7, 31, 12, tzinfo=dt.UTC), -3),
        ],
    )
    def test_counting(self, expires, expected):
        assert days_left(expires, now=self.now) == expected

    def test_naive_datetime_treated_as_utc(self):
        naive = dt.datetime(2026, 8, 10, 12)
        assert days_left(naive, now=self.now) == 7


class TestDisplay:
    def test_utc_converted_to_moscow(self):
        value = dt.datetime(2026, 8, 3, 21, 30, tzinfo=dt.UTC)
        local = to_display(value)
        assert local.hour == 0  # +3 часа, уже следующие сутки
        assert local.day == 4

    def test_date_formatting_is_russian(self):
        value = dt.datetime(2026, 8, 3, 9, 5, tzinfo=dt.UTC)
        assert format_date_ru(value) == "3 августа 2026"
        assert format_datetime_ru(value) == "3 августа 2026, 12:05"

    def test_ensure_utc_normalizes(self):
        naive = dt.datetime(2026, 8, 3, 12)
        assert ensure_utc(naive).tzinfo is dt.UTC


class TestPlural:
    @pytest.mark.parametrize(
        ("number", "expected"),
        [(1, "день"), (2, "дня"), (4, "дня"), (5, "дней"), (11, "дней"), (21, "день"), (114, "дней")],
    )
    def test_forms(self, number, expected):
        assert plural_ru(number, "день", "дня", "дней") == expected

    def test_days_phrase(self):
        assert days_phrase(3) == "3 дня"
        assert days_phrase(10) == "10 дней"
