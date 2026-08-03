"""Платежи: разбор ответов PLATEGA, проверка колбэка, идемпотентность."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from core.enums import PaymentStatus
from core.payments.platega import (
    METHOD_CARD,
    METHOD_SBP,
    PlategaProvider,
    _parse_expires_in,
)
from core.services.payments import build_payload, parse_payload


@pytest.fixture
def provider(monkeypatch):
    from pydantic import SecretStr

    from core.config import settings

    monkeypatch.setattr(settings.platega, "merchant_id", "merchant-uuid", raising=False)
    monkeypatch.setattr(settings.platega, "secret", SecretStr("super-secret"), raising=False)
    return PlategaProvider()


class TestPayload:
    """Своего externalId у провайдера нет — наш id ездит в поле payload."""

    def test_roundtrip(self):
        assert parse_payload(build_payload(42)) == 42

    @pytest.mark.parametrize("bad", [None, "", "42", "other:42", "rs:", "rs:abc", "garbage"])
    def test_invalid_payload(self, bad):
        assert parse_payload(bad) is None


class TestExpiresIn:
    def test_parses_duration(self):
        now = dt.datetime(2026, 8, 3, 12, tzinfo=dt.UTC)
        assert _parse_expires_in("00:15:00", now=now) == dt.datetime(2026, 8, 3, 12, 15, tzinfo=dt.UTC)

    def test_parses_hours(self):
        now = dt.datetime(2026, 8, 3, 12, tzinfo=dt.UTC)
        assert _parse_expires_in("01:30:00", now=now) == dt.datetime(2026, 8, 3, 13, 30, tzinfo=dt.UTC)

    @pytest.mark.parametrize("bad", [None, "", "15m", "00:15", "aa:bb:cc"])
    def test_invalid_duration(self, bad):
        assert _parse_expires_in(bad) is None


class TestWebhookVerification:
    """Подписи у провайдера нет: подлинность — по возвращённым реквизитам."""

    def test_valid_headers_pass(self, provider):
        headers = {"X-MerchantId": "merchant-uuid", "X-Secret": "super-secret"}
        assert provider.verify_webhook(headers, b"{}") is True

    def test_header_case_does_not_matter(self, provider):
        headers = {"x-merchantid": "merchant-uuid", "x-secret": "super-secret"}
        assert provider.verify_webhook(headers, b"{}") is True

    def test_wrong_secret_rejected(self, provider):
        headers = {"X-MerchantId": "merchant-uuid", "X-Secret": "wrong"}
        assert provider.verify_webhook(headers, b"{}") is False

    def test_wrong_merchant_rejected(self, provider):
        headers = {"X-MerchantId": "someone-else", "X-Secret": "super-secret"}
        assert provider.verify_webhook(headers, b"{}") is False

    def test_missing_headers_rejected(self, provider):
        assert provider.verify_webhook({}, b"{}") is False


class TestWebhookParsing:
    def test_confirmed_maps_to_succeeded(self, provider):
        result = provider.parse_webhook(
            {
                "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "amount": 6900,
                "currency": "RUB",
                "status": "CONFIRMED",
                "paymentMethod": METHOD_SBP,
                "payload": "rs:17",
            }
        )
        assert result.status is PaymentStatus.SUCCEEDED
        assert result.amount == Decimal("6900")
        assert result.payload == "rs:17"
        assert parse_payload(result.payload) == 17

    def test_canceled_maps_to_canceled(self, provider):
        result = provider.parse_webhook({"id": "x", "status": "CANCELED", "amount": 100})
        assert result.status is PaymentStatus.CANCELED

    def test_chargeback_maps_to_refunded(self, provider):
        result = provider.parse_webhook({"id": "x", "status": "CHARGEBACKED", "amount": 100})
        assert result.status is PaymentStatus.REFUNDED

    def test_unknown_status_stays_pending(self, provider):
        result = provider.parse_webhook({"id": "x", "status": "SOMETHING_NEW"})
        assert result.status is PaymentStatus.PENDING

    def test_missing_id_raises(self, provider):
        from core.payments.base import PaymentProviderError

        with pytest.raises(PaymentProviderError):
            provider.parse_webhook({"status": "CONFIRMED"})

    def test_amount_is_decimal_not_float(self, provider):
        result = provider.parse_webhook({"id": "x", "status": "CONFIRMED", "amount": 1990.5})
        assert isinstance(result.amount, Decimal)
        assert result.amount == Decimal("1990.5")


class TestMethodResolution:
    def test_named_method(self, provider):
        assert provider._resolve_method("sbp") == METHOD_SBP
        assert provider._resolve_method("card") == METHOD_CARD

    def test_numeric_method_passes_through(self, provider):
        assert provider._resolve_method("11") == METHOD_CARD

    def test_any_means_client_chooses(self, provider):
        assert provider._resolve_method("any") is None
        assert provider._resolve_method(None) is None


class TestConfiguration:
    def test_requires_credentials(self, provider):
        assert provider.is_configured is True

    def test_not_configured_without_secret(self, monkeypatch):
        from pydantic import SecretStr

        from core.config import settings

        monkeypatch.setattr(settings.platega, "merchant_id", "", raising=False)
        monkeypatch.setattr(settings.platega, "secret", SecretStr(""), raising=False)
        assert PlategaProvider().is_configured is False
