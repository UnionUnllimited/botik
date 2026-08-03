"""Проверка данных, которые вводит покупатель."""

from __future__ import annotations

import re

import phonenumbers

_NAME_RE = re.compile(r"^[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\s\-']{1,99}$")
_CITY_RE = re.compile(r"^[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\s\-.']{1,79}$")


def clean_full_name(raw: str) -> str:
    """ФИО: минимум два слова, только буквы, дефис и апостроф."""
    value = " ".join(raw.split())[:200]
    if len(value.split()) < 2 or not _NAME_RE.match(value):
        return ""
    return value


def clean_phone(raw: str) -> str:
    """Российский номер в формате E.164 (+79001234567). Пустая строка — невалиден."""
    candidate = raw.strip()
    if candidate.startswith("8") and len(re.sub(r"\D", "", candidate)) == 11:
        candidate = "+7" + re.sub(r"\D", "", candidate)[1:]
    try:
        parsed = phonenumbers.parse(candidate, "RU")
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_valid_number(parsed):
        return ""
    if phonenumbers.region_code_for_number(parsed) != "RU":
        return ""
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def format_phone(e164: str) -> str:
    """+79001234567 -> +7 900 123-45-67."""
    try:
        parsed = phonenumbers.parse(e164, "RU")
    except phonenumbers.NumberParseException:
        return e164
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)


def clean_city(raw: str) -> str:
    value = " ".join(raw.split())[:120]
    return value if _CITY_RE.match(value) else ""


def clean_address(raw: str) -> str:
    value = " ".join(raw.split())[:500]
    return value if len(value) >= 10 and any(ch.isdigit() for ch in value) else ""


def clean_pvz(raw: str) -> str:
    value = " ".join(raw.split())[:500]
    return value if len(value) >= 5 else ""
