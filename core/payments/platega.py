"""Провайдер PLATEGA.

Документация: https://docs.platega.io (проверено 03.08.2026).

Особенности, которые определили реализацию:
  * авторизация — заголовки X-MerchantId и X-Secret;
  * идемпотентности на стороне провайдера нет: своего externalId передать
    некуда, поэтому наш идентификатор кладём в свободное поле `payload`
    и требуем уникальности `payments.provider_payment_id` в БД;
  * колбэк не подписан HMAC — провайдер присылает обратно наши же
    X-MerchantId и X-Secret, сверяем их constant-time и дополнительно
    сверяем сумму с сохранённым платежом перед зачислением;
  * состав чека по 54-ФЗ API не принимает — фискализация выносится
    в отдельную интеграцию с облачной кассой (см. docs/decisions.md).
"""

from __future__ import annotations

import datetime as dt
import hmac
from decimal import Decimal
from typing import Any

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.config import settings
from core.enums import PaymentProviderName, PaymentStatus
from core.payments.base import (
    PaymentProvider,
    PaymentProviderError,
    PaymentRequest,
    PaymentResult,
    RefundResult,
    WebhookResult,
)

log = structlog.get_logger("payments.platega")

# Числовые коды способов оплаты (PaymentMethodInt из документации).
METHOD_SBP = 2
"""СБП (QR-код) + SberPay."""
METHOD_ERIP = 3
METHOD_CARD = 11
METHOD_INTERNATIONAL = 12
METHOD_CRYPTO = 13

SUPPORTED_METHODS = {
    "sbp": METHOD_SBP,
    "card": METHOD_CARD,
    "international": METHOD_INTERNATIONAL,
    "crypto": METHOD_CRYPTO,
}

_STATUS_MAP = {
    "PENDING": PaymentStatus.PENDING,
    "CONFIRMED": PaymentStatus.SUCCEEDED,
    "CANCELED": PaymentStatus.CANCELED,
    "CANCELLED": PaymentStatus.CANCELED,
    "CHARGEBACKED": PaymentStatus.REFUNDED,
}


def _parse_expires_in(value: str | None, *, now: dt.datetime | None = None) -> dt.datetime | None:
    """`"00:15:00"` -> абсолютный момент истечения ссылки."""
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3 or not all(part.strip().isdigit() for part in parts):
        return None
    hours, minutes, seconds = (int(part) for part in parts)
    base = now or dt.datetime.now(dt.UTC)
    return base + dt.timedelta(hours=hours, minutes=minutes, seconds=seconds)


class PlategaProvider(PaymentProvider):
    name = PaymentProviderName.PLATEGA

    def __init__(self) -> None:
        self._config = settings.platega
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self._config.merchant_id and self._config.secret.get_secret_value())

    # ------------------------------------------------------------------ http

    def _headers(self) -> dict[str, str]:
        return {
            "X-MerchantId": self._config.merchant_id,
            "X-Secret": self._config.secret.get_secret_value(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url.rstrip("/"),
                timeout=httpx.Timeout(self._config.timeout_sec),
                headers=self._headers(),
            )
        return self._client

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        client = await self._get_client()
        try:
            response = await client.request(method, path, **kwargs)
        except (httpx.TransportError, httpx.TimeoutException):
            raise
        except httpx.HTTPError as exc:  # pragma: no cover — нештатная ошибка клиента
            raise PaymentProviderError(f"PLATEGA: ошибка запроса {exc}") from exc

        if response.status_code >= 400:
            log.warning(
                "platega.http_error",
                method=method,
                path=path,
                status=response.status_code,
                body=response.text[:500],
            )
            raise PaymentProviderError(
                f"PLATEGA вернул {response.status_code}",
                status_code=response.status_code,
                raw=response.text[:2000],
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PaymentProviderError("PLATEGA: ответ не является JSON", raw=response.text[:500]) from exc
        if not isinstance(payload, dict):
            raise PaymentProviderError("PLATEGA: неожиданный формат ответа", raw=payload)
        return payload

    # --------------------------------------------------------------- платежи

    async def create_payment(self, request: PaymentRequest) -> PaymentResult:
        body: dict[str, Any] = {
            "paymentDetails": {
                "amount": float(request.amount),
                "currency": request.currency,
            },
            "description": request.description[:255],
            "return": request.return_url,
            "failedUrl": request.fail_url,
            "payload": request.payload,
        }
        if request.user_tg_id:
            body["metadata"] = {
                "userId": str(request.user_tg_id),
                "userName": request.user_name or "",
            }

        method_code = self._resolve_method(request.method)
        if method_code is not None:
            body["paymentMethod"] = method_code
            path = self._config.create_path_with_method
        else:
            path = self._config.create_path

        data = await self._request("POST", path, json=body)

        provider_id = str(data.get("transactionId") or data.get("id") or "")
        if not provider_id:
            raise PaymentProviderError("PLATEGA: в ответе нет transactionId", raw=data)

        url = data.get("url") or data.get("redirect")
        log.info(
            "platega.payment_created",
            payment_id=request.payment_id,
            provider_payment_id=provider_id,
            amount=str(request.amount),
        )
        return PaymentResult(
            provider_payment_id=provider_id,
            status=_STATUS_MAP.get(str(data.get("status", "")).upper(), PaymentStatus.PENDING),
            confirmation_url=str(url) if url else None,
            expires_at=_parse_expires_in(data.get("expiresIn")),
            amount=request.amount,
            currency=request.currency,
            raw=data,
        )

    def _resolve_method(self, requested: str | None) -> int | None:
        code = requested or self._config.default_method
        if not code or code == "any":
            return None
        if code.isdigit():
            return int(code)
        return SUPPORTED_METHODS.get(code)

    async def check_status(self, provider_payment_id: str) -> PaymentResult:
        data = await self._request("GET", f"{self._config.status_path}/{provider_payment_id}")
        details = data.get("paymentDetails") or {}
        amount = details.get("amount") if isinstance(details, dict) else None
        return PaymentResult(
            provider_payment_id=str(data.get("id") or provider_payment_id),
            status=_STATUS_MAP.get(str(data.get("status", "")).upper(), PaymentStatus.PENDING),
            confirmation_url=data.get("payformSuccessUrl"),
            amount=Decimal(str(amount)) if amount is not None else None,
            currency=details.get("currency") if isinstance(details, dict) else None,
            raw=data,
        )

    async def refund(self, provider_payment_id: str, amount: Decimal | None = None) -> RefundResult:
        """У PLATEGA возврат — это отмена транзакции целиком; частичный не поддержан."""
        data = await self._request("POST", f"{self._config.status_path}/{provider_payment_id}/cancel")
        return RefundResult(
            accepted=bool(data.get("accepted")),
            manual_required=bool(data.get("manualControlRequired")),
            message=str(data.get("message") or ""),
            raw=data,
        )

    # --------------------------------------------------------------- колбэки

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        """PLATEGA присылает обратно наши же реквизиты — сверяем их constant-time.

        HMAC-подписи у провайдера нет, поэтому это единственный доступный
        механизм. Дополнительно вызывающий код обязан сверить сумму платежа.
        """
        normalized = {key.lower(): value for key, value in headers.items()}
        merchant = normalized.get("x-merchantid", "")
        secret = normalized.get("x-secret", "")
        merchant_ok = hmac.compare_digest(merchant, self._config.merchant_id)
        secret_ok = hmac.compare_digest(secret, self._config.secret.get_secret_value())
        return merchant_ok and secret_ok

    def parse_webhook(self, data: dict[str, Any]) -> WebhookResult:
        provider_id = str(data.get("id") or "")
        if not provider_id:
            raise PaymentProviderError("PLATEGA: в колбэке нет id транзакции", raw=data)
        amount = data.get("amount")
        return WebhookResult(
            provider_payment_id=provider_id,
            status=_STATUS_MAP.get(str(data.get("status", "")).upper(), PaymentStatus.PENDING),
            amount=Decimal(str(amount)) if amount is not None else None,
            currency=data.get("currency"),
            payload=data.get("payload"),
            raw=data,
        )

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
