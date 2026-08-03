"""Оплата при получении.

Онлайн-платежа нет: деньги забирает перевозчик. Провайдер нужен, чтобы
заказ с наложенным платежом проходил ровно тот же путь, что и обычный,
и попадал в те же отчёты.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from core.enums import PaymentProviderName, PaymentStatus
from core.payments.base import (
    PaymentNotSupportedError,
    PaymentProvider,
    PaymentRequest,
    PaymentResult,
    RefundResult,
    WebhookResult,
)


class CashOnDeliveryProvider(PaymentProvider):
    name = PaymentProviderName.COD

    @property
    def is_configured(self) -> bool:
        return True

    async def create_payment(self, request: PaymentRequest) -> PaymentResult:
        """Платёж сразу «ожидает» — подтвердит админ после получения денег."""
        return PaymentResult(
            provider_payment_id=f"cod-{request.payment_id}",
            status=PaymentStatus.PENDING,
            confirmation_url=None,
            amount=request.amount,
            currency=request.currency,
            raw={"note": "оплата при получении"},
        )

    async def check_status(self, provider_payment_id: str) -> PaymentResult:
        return PaymentResult(provider_payment_id=provider_payment_id, status=PaymentStatus.PENDING)

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        return False

    def parse_webhook(self, data: dict[str, Any]) -> WebhookResult:
        raise PaymentNotSupportedError("Оплата при получении не присылает уведомлений")

    async def refund(self, provider_payment_id: str, amount: Decimal | None = None) -> RefundResult:
        raise PaymentNotSupportedError("Возврат наложенного платежа оформляется вне системы")
