"""Промокоды и коды активации с коробок."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.enums import ActivationCodeStatus, PromoDiscountType
from core.models.base import MONEY, Base, IntPkMixin, TimestampMixin, enum_column


class PromoCode(IntPkMixin, TimestampMixin, Base):
    __tablename__ = "promo_codes"

    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    """Хранится в верхнем регистре, сравнение — по нормализованному значению."""
    description: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    discount_type: Mapped[PromoDiscountType] = enum_column(PromoDiscountType, nullable=False)
    value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    """Проценты (0..100) или рубли — в зависимости от discount_type."""

    max_uses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    """0 — без ограничения."""
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    per_user_limit: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    min_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)

    valid_from: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"))
    new_clients_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    usages: Mapped[list[PromoUsage]] = relationship(back_populates="promo_code", cascade="all, delete-orphan")


class PromoUsage(IntPkMixin, Base):
    __tablename__ = "promo_usages"

    promo_code_id: Mapped[int] = mapped_column(
        ForeignKey("promo_codes.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    amount_discounted: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    promo_code: Mapped[PromoCode] = relationship(back_populates="usages")

    __table_args__ = (
        UniqueConstraint("promo_code_id", "order_id", name="uq_promo_usages_promo_code_id_order_id"),
        Index("ix_promo_usages_user_id", "user_id"),
    )


class ActivationCodeBatch(IntPkMixin, TimestampMixin, Base):
    """Партия кодов: сгенерировали N штук, выгрузили CSV, напечатали на коробки."""

    __tablename__ = "activation_code_batches"

    title: Mapped[str] = mapped_column(String(120), nullable=False)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))
    months: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    extra_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_admin_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    comment: Mapped[str | None] = mapped_column(Text)

    codes: Mapped[list[ActivationCode]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class ActivationCode(IntPkMixin, Base):
    """Одноразовый код XXXX-XXXX-XXXX.

    Хранится в открытом виде: партию нужно уметь перевыпустить и напечатать,
    а ценность одного кода ограничена сроком подписки и одноразовостью.
    """

    __tablename__ = "activation_codes"

    code: Mapped[str] = mapped_column(String(14), unique=True, nullable=False)
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("activation_code_batches.id", ondelete="SET NULL")
    )
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))
    months: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    extra_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    status: Mapped[ActivationCodeStatus] = enum_column(
        ActivationCodeStatus, nullable=False, default=ActivationCodeStatus.NEW
    )
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    used_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    used_device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    batch: Mapped[ActivationCodeBatch | None] = relationship(back_populates="codes")

    __table_args__ = (
        Index("ix_activation_codes_batch_id", "batch_id"),
        Index("ix_activation_codes_status", "status"),
    )
