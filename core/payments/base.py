"""Абстракция платёжного провайдера.

Бизнес-логика работает только с этими типами и не знает, кто именно проводит
платёж. Добавление провайдера = новая реализация PaymentProvider + запись
в реестр, без правок в заказах, подписках и хендлерах.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from core.enums import PaymentProviderName, PaymentStatus


class PaymentProviderError(RuntimeError):
    """Провайдер вернул ошибку или недоступен."""

    def __init__(self, message: str, *, status_code: int | None = None, raw: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.raw = raw


class PaymentNotSupportedError(PaymentProviderError):
    """Операция не поддерживается этим провайдером (например, возврат)."""


@dataclass(frozen=True, slots=True)
class PaymentRequest:
    """Всё, что нужно провайдеру для создания платежа."""

    payment_id: int
    """Наш внутренний id — уходит провайдеру и возвращается в колбэке."""
    amount: Decimal
    currency: str
    description: str
    return_url: str
    fail_url: str
    payload: str
    """Строка-маркер, по которой находим платёж при разборе колбэка."""
    user_tg_id: int | None = None
    user_name: str | None = None
    method: str | None = None
    """Код способа оплаты в терминах провайдера; None — выбирает клиент."""
    receipt_items: list[dict[str, Any]] = field(default_factory=list)
    """Состав чека по 54-ФЗ. Сохраняется всегда, даже если провайдер его не принимает."""


@dataclass(frozen=True, slots=True)
class PaymentResult:
    provider_payment_id: str
    status: PaymentStatus
    confirmation_url: str | None = None
    expires_at: dt.datetime | None = None
    amount: Decimal | None = None
    currency: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WebhookResult:
    """Разобранное уведомление провайдера."""

    provider_payment_id: str
    status: PaymentStatus
    amount: Decimal | None = None
    currency: str | None = None
    payload: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RefundResult:
    accepted: bool
    manual_required: bool = False
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentProvider(ABC):
    name: PaymentProviderName

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """Заданы ли реквизиты в .env — иначе провайдер не показывается клиенту."""

    @abstractmethod
    async def create_payment(self, request: PaymentRequest) -> PaymentResult: ...

    @abstractmethod
    async def check_status(self, provider_payment_id: str) -> PaymentResult: ...

    @abstractmethod
    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        """Проверка подлинности уведомления. Сравнения — только constant-time."""

    @abstractmethod
    def parse_webhook(self, data: dict[str, Any]) -> WebhookResult: ...

    async def refund(self, provider_payment_id: str, amount: Decimal | None = None) -> RefundResult:
        raise PaymentNotSupportedError(f"{self.name}: возврат через API не поддерживается")

    async def aclose(self) -> None:
        """Закрыть http-клиент при остановке процесса. По умолчанию — нечего."""
        return None
