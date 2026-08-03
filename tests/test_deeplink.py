"""Разбор payload у /start."""

from __future__ import annotations

import pytest

from bot.utils.deeplink import PayloadKind, build_deeplink, parse_start_payload, referral_link


@pytest.mark.parametrize(
    ("raw", "kind", "value"),
    [
        ("ref_123456789", PayloadKind.REFERRAL, "123456789"),
        ("dev_a1b2c3d4e5", PayloadKind.DEVICE, "a1b2c3d4e5"),
        ("order_42", PayloadKind.ORDER, "42"),
        ("utm_youtube", PayloadKind.UTM, "youtube"),
        ("utm_yandex_direct", PayloadKind.UTM, "yandex_direct"),
    ],
)
def test_known_payloads(raw, kind, value):
    payload = parse_start_payload(raw)
    assert payload is not None
    assert payload.kind is kind
    assert payload.value == value
    assert payload.raw == raw


def test_empty_payload():
    assert parse_start_payload(None) is None
    assert parse_start_payload("") is None


def test_unknown_payload_is_preserved():
    payload = parse_start_payload("promo2026")
    assert payload is not None
    assert payload.kind is PayloadKind.UNKNOWN
    assert payload.raw == "promo2026"


def test_referral_id_parsing():
    assert parse_start_payload("ref_777").as_int == 777
    assert parse_start_payload("ref_abc").as_int is None


def test_long_payload_is_truncated():
    payload = parse_start_payload("utm_" + "x" * 500)
    assert payload is not None
    assert len(payload.raw) <= 128


def test_links_use_bot_username():
    assert build_deeplink("order_1") == "https://t.me/test_router_bot?start=order_1"
    assert referral_link(555) == "https://t.me/test_router_bot?start=ref_555"
