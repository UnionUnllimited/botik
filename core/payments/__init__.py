"""Реестр платёжных провайдеров."""

from __future__ import annotations

import structlog

from core.enums import PaymentProviderName
from core.payments.base import (
    PaymentNotSupportedError,
    PaymentProvider,
    PaymentProviderError,
    PaymentRequest,
    PaymentResult,
    RefundResult,
    WebhookResult,
)
from core.payments.cod import CashOnDeliveryProvider
from core.payments.platega import PlategaProvider

log = structlog.get_logger("payments")

_registry: dict[PaymentProviderName, PaymentProvider] = {}


def get_provider(name: PaymentProviderName | str) -> PaymentProvider:
    """Возвращает провайдера (по одному экземпляру на процесс — они держат http-клиент)."""
    key = PaymentProviderName(name)
    provider = _registry.get(key)
    if provider is None:
        provider = _build(key)
        _registry[key] = provider
    return provider


def _build(name: PaymentProviderName) -> PaymentProvider:
    match name:
        case PaymentProviderName.PLATEGA:
            return PlategaProvider()
        case PaymentProviderName.COD:
            return CashOnDeliveryProvider()
        case _:
            raise PaymentNotSupportedError(f"Провайдер {name} не подключён")


def online_provider() -> PaymentProvider | None:
    """Провайдер онлайн-оплаты, если он настроен в .env."""
    provider = get_provider(PaymentProviderName.PLATEGA)
    return provider if provider.is_configured else None


async def close_providers() -> None:
    for provider in _registry.values():
        await provider.aclose()
    _registry.clear()


__all__ = [
    "CashOnDeliveryProvider",
    "PaymentNotSupportedError",
    "PaymentProvider",
    "PaymentProviderError",
    "PaymentRequest",
    "PaymentResult",
    "PlategaProvider",
    "RefundResult",
    "WebhookResult",
    "close_providers",
    "get_provider",
    "online_provider",
]
