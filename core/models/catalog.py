"""Каталог: роутеры (товар) и тарифы подписки."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.dates import add_period
from core.enums import VatCode
from core.models.base import MONEY, Base, IntPkMixin, TimestampMixin, enum_column


class Product(IntPkMixin, TimestampMixin, Base):
    """Физический роутер. Цены и тексты правятся в админке без деплоя."""

    __tablename__ = "products"

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    subtitle: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model_code: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    """Внутренний код модели — им же маркируются устройства при активации."""

    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    old_price: Mapped[Decimal | None] = mapped_column(MONEY)
    vat_code: Mapped[VatCode] = enum_column(VatCode, length=8, nullable=False, default=VatCode.NONE)

    stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    allow_preorder: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    photo_file_id: Mapped[str | None] = mapped_column(String(255))
    """file_id фото в Telegram — переиспользуется, чтобы не грузить картинку каждый раз."""
    photo_url: Mapped[str | None] = mapped_column(String(512))
    specs: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    """Характеристики человеческим языком: {"Порты": "3 LAN", "Wi-Fi": "..."}."""

    @property
    def in_stock(self) -> bool:
        return self.stock > 0 or self.allow_preorder


class Plan(IntPkMixin, TimestampMixin, Base):
    """Тариф подписки на сервис доступа."""

    __tablename__ = "plans"

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    months: Mapped[int] = mapped_column(Integer, nullable=False)
    extra_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    """Бонусные дни сверх месяцев (акции «12 мес + 30 дней»)."""

    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    old_price: Mapped[Decimal | None] = mapped_column(MONEY)
    discount_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0.00"), nullable=False)
    vat_code: Mapped[VatCode] = enum_column(VatCode, length=8, nullable=False, default=VatCode.NONE)

    node_group_id: Mapped[int | None] = mapped_column(ForeignKey("node_groups.id", ondelete="SET NULL"))
    """Какой набор узлов получает клиент на этом тарифе."""

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    def apply_to(self, start: dt.datetime) -> dt.datetime:
        """Дата окончания при старте `start`: календарные месяцы + бонусные дни.

        `or 0` не декоративный: у ещё не сохранённого объекта серверные
        значения по умолчанию не проставлены и поле равно None.
        """
        return add_period(start, months=self.months or 0, days=self.extra_days or 0)

    @property
    def price_per_month(self) -> Decimal:
        if self.months <= 0:
            return self.price
        return (self.price / self.months).quantize(Decimal("0.01"))
