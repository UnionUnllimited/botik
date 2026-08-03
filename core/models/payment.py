"""Платежи. Одна строка = одна попытка оплаты у провайдера."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.enums import PaymentProviderName, PaymentPurpose, PaymentStatus
from core.models.base import MONEY, Base, IntPkMixin, TimestampMixin, enum_column

if TYPE_CHECKING:
    from core.models.order import Order
    from core.models.user import User


class Payment(IntPkMixin, TimestampMixin, Base):
    __tablename__ = "payments"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id", ondelete="SET NULL"))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))

    provider: Mapped[PaymentProviderName] = enum_column(PaymentProviderName, nullable=False)
    purpose: Mapped[PaymentPurpose] = enum_column(PaymentPurpose, nullable=False)
    status: Mapped[PaymentStatus] = enum_column(PaymentStatus, nullable=False, default=PaymentStatus.PENDING)

    provider_payment_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    """id платежа на стороне провайдера. UNIQUE — защита от двойного зачисления."""
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    refunded_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="RUB", nullable=False)

    confirmation_url: Mapped[str | None] = mapped_column(String(1024))
    description: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)

    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    """Срок жизни платёжной ссылки (у PLATEGA — 15 минут)."""
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    """Момент, когда успешный платёж уже отработан бизнес-логикой (заказ/подписка)."""

    receipt: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    """Состав чека по 54-ФЗ, как он ушёл провайдеру."""
    raw_request: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    raw_webhook: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    user: Mapped[User] = relationship(back_populates="payments")
    order: Mapped[Order | None] = relationship(back_populates="payments")

    __table_args__ = (
        Index("ix_payments_user_id", "user_id"),
        Index("ix_payments_order_id", "order_id"),
        Index("ix_payments_status_created_at", "status", "created_at"),
        Index("ix_payments_status_expires_at", "status", "expires_at"),
    )

    @property
    def is_final(self) -> bool:
        return self.status in {
            PaymentStatus.SUCCEEDED,
            PaymentStatus.CANCELED,
            PaymentStatus.FAILED,
            PaymentStatus.REFUNDED,
        }
