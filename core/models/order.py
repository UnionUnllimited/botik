"""Заказы, позиции заказа и доставка."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.enums import (
    DeliveryMethod,
    DeliverySpeed,
    DeliveryStatus,
    OrderItemType,
    OrderStatus,
    VatCode,
)
from core.models.base import MONEY, Base, IntPkMixin, TimestampMixin, enum_column

if TYPE_CHECKING:
    from core.models.payment import Payment
    from core.models.user import User


class Order(IntPkMixin, TimestampMixin, Base):
    __tablename__ = "orders"

    public_number: Mapped[str] = mapped_column(String(24), unique=True, nullable=False)
    """Человеческий номер вида R-260803-0142: его называет клиент в поддержке."""
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[OrderStatus] = enum_column(OrderStatus, nullable=False, default=OrderStatus.NEW)

    subtotal: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    discount_total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    delivery_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="RUB", nullable=False)

    promo_code_id: Mapped[int | None] = mapped_column(ForeignKey("promo_codes.id", ondelete="SET NULL"))
    is_cod: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """Оплата при получении: деньги забирает перевозчик, наш платёж не создаётся."""

    customer_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    customer_phone: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    customer_city: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    admin_note: Mapped[str | None] = mapped_column(Text)
    utm_source: Mapped[str | None] = mapped_column(String(64))

    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    shipped_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id"
    )
    delivery: Mapped[Delivery | None] = relationship(
        back_populates="order", cascade="all, delete-orphan", uselist=False
    )
    payments: Mapped[list[Payment]] = relationship(back_populates="order")

    __table_args__ = (
        Index("ix_orders_user_id", "user_id"),
        Index("ix_orders_status_created_at", "status", "created_at"),
    )


class OrderItem(IntPkMixin, Base):
    __tablename__ = "order_items"

    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    item_type: Mapped[OrderItemType] = enum_column(OrderItemType, nullable=False)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    """Снимок названия на момент заказа — цены и названия в каталоге меняются."""
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    vat_code: Mapped[VatCode] = enum_column(VatCode, length=8, nullable=False, default=VatCode.NONE)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")

    __table_args__ = (Index("ix_order_items_order_id", "order_id"),)


class Delivery(IntPkMixin, TimestampMixin, Base):
    __tablename__ = "deliveries"

    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    method: Mapped[DeliveryMethod] = enum_column(DeliveryMethod, nullable=False)
    """Кто повезёт. Выбирает оператор при отгрузке, а не клиент: перевозчик
    зависит от города, веса и действующего договора."""

    speed: Mapped[DeliverySpeed] = enum_column(
        DeliverySpeed, nullable=False, default=DeliverySpeed.FAST
    )
    """Что выбрал клиент: быстро и дороже или дешевле, но ждать отправки."""

    status: Mapped[DeliveryStatus] = enum_column(DeliveryStatus, nullable=False, default=DeliveryStatus.NEW)

    city: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    postal_code: Mapped[str | None] = mapped_column(String(12))
    pvz_code: Mapped[str | None] = mapped_column(String(64))
    pvz_address: Mapped[str | None] = mapped_column(Text)

    recipient_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    recipient_phone: Mapped[str] = mapped_column(String(20), default="", nullable=False)

    price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    quoted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    """Когда оператор назвал цену доставки.

    Отдельно от `price`: без этой отметки ноль читается как «бесплатно»,
    а на деле означает «ещё не считали». Заказ ждёт цены именно по ней."""

    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    """Когда клиент оплатил доставку вторым платежом.

    Полем, а не поиском успешного платежа по заказу: состояние доставки
    читают списки и сводка, а тянуть туда платежи ради одного флага дорого."""

    reminded_day: Mapped[int | None] = mapped_column(Integer)
    """На какой день после выставления счёта уже напомнили про оплату доставки.

    Защита от повторов: круг напоминаний ходит раз в сутки, но при перезапуске
    воркера он пойдёт снова, и без отметки клиент получил бы то же сообщение
    дважды."""

    tracking_number: Mapped[str | None] = mapped_column(String(64))
    tracking_url: Mapped[str | None] = mapped_column(String(512))
    shipped_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    """Ответ службы доставки, если заказ создавался через её API."""

    order: Mapped[Order] = relationship(back_populates="delivery")
