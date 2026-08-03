"""Подписки и история их изменений."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.enums import SubscriptionEventType, SubscriptionStatus
from core.models.base import Base, IntPkMixin, TimestampMixin, enum_column

if TYPE_CHECKING:
    from core.models.catalog import Plan
    from core.models.device import Device
    from core.models.user import User


class Subscription(IntPkMixin, TimestampMixin, Base):
    """Подписка на сервис доступа.

    Создаётся оплаченной в статусе `pending`: отсчёт срока начинается не с оплаты,
    а с активации роутера — клиент не теряет дни, пока посылка едет.
    """

    __tablename__ = "subscriptions"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))

    status: Mapped[SubscriptionStatus] = enum_column(
        SubscriptionStatus, nullable=False, default=SubscriptionStatus.PENDING
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    grace_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    pending_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    """До какой даты оплаченная подписка ждёт активации."""
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_renew_token: Mapped[str | None] = mapped_column(String(255))
    """Идентификатор сохранённого способа оплаты у провайдера (рекуррент)."""

    last_reminder_day: Mapped[int | None] = mapped_column(Integer)
    """Какое напоминание уже отправлено (7/3/1/0/-1/-3) — защита от дублей."""
    source: Mapped[str] = mapped_column(String(32), default="order", nullable=False)
    """order / activation_code / manual / bonus."""
    comment: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="subscriptions")
    device: Mapped[Device | None] = relationship(back_populates="subscriptions")
    plan: Mapped[Plan | None] = relationship()
    events: Mapped[list[SubscriptionEvent]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan", order_by="SubscriptionEvent.id"
    )

    __table_args__ = (
        Index("ix_subscriptions_user_id", "user_id"),
        Index("ix_subscriptions_device_id", "device_id"),
        Index("ix_subscriptions_expires_at", "expires_at"),
        Index("ix_subscriptions_status_expires_at", "status", "expires_at"),
    )

    def is_serving(self, *, now: dt.datetime | None = None) -> bool:
        """Отдавать ли устройству рабочий список узлов (учитывая grace-период)."""
        if self.status not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE):
            return False
        current = now or dt.datetime.now(dt.UTC)
        deadline = self.grace_until or self.expires_at
        if deadline is None:
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=dt.UTC)
        return current <= deadline


class SubscriptionEvent(IntPkMixin, Base):
    """Аудит подписки: любое изменение срока фиксируется строкой."""

    __tablename__ = "subscription_events"

    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    event: Mapped[SubscriptionEventType] = enum_column(SubscriptionEventType, nullable=False)
    days_delta: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    old_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    new_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"))
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    subscription: Mapped[Subscription] = relationship(back_populates="events")

    __table_args__ = (Index("ix_subscription_events_subscription_id", "subscription_id"),)
