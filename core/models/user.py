"""Клиенты бота, администраторы и рефералы."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.enums import AdminRole, ReferralStatus
from core.models.base import Base, IntPkMixin, TimestampMixin, enum_column

if TYPE_CHECKING:
    from core.models.device import Device
    from core.models.order import Order
    from core.models.payment import Payment
    from core.models.subscription import Subscription
    from core.models.support import Ticket


class User(IntPkMixin, TimestampMixin, Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str] = mapped_column(String(8), default="ru", nullable=False)

    # Контактные данные покупателя — заполняются при первом заказе, потом переиспользуются.
    full_name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(20))
    city: Mapped[str | None] = mapped_column(String(120))

    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """Забанен нами (фрод/абьюз)."""
    block_reason: Mapped[str | None] = mapped_column(Text)
    bot_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """Пользователь заблокировал бота — рассылки ему не шлём."""
    bot_blocked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    referrer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    utm_source: Mapped[str | None] = mapped_column(String(64))
    start_payload: Mapped[str | None] = mapped_column(String(128))
    """Сырой payload первого /start — для разбора источников трафика."""

    bonus_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    """Начисленные, но ещё не использованные бонусные дни подписки."""
    admin_note: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    referrer: Mapped[User | None] = relationship(remote_side="User.id", back_populates="referrals")
    referrals: Mapped[list[User]] = relationship(back_populates="referrer")
    orders: Mapped[list[Order]] = relationship(back_populates="user")
    devices: Mapped[list[Device]] = relationship(back_populates="user")
    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="user")
    payments: Mapped[list[Payment]] = relationship(back_populates="user")
    tickets: Mapped[list[Ticket]] = relationship(back_populates="user")

    __table_args__ = (
        Index("ix_users_phone", "phone"),
        Index("ix_users_referrer_id", "referrer_id"),
        Index("ix_users_username", "username"),
    )

    @property
    def display_name(self) -> str:
        if self.full_name:
            return self.full_name
        parts = [self.first_name or "", self.last_name or ""]
        name = " ".join(p for p in parts if p).strip()
        return name or (f"@{self.username}" if self.username else f"id{self.tg_id}")


class AdminUser(IntPkMixin, TimestampMixin, Base):
    __tablename__ = "admin_users"

    login: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    tg_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    role: Mapped[AdminRole] = enum_column(AdminRole, nullable=False, default=AdminRole.SUPPORT)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    totp_secret_enc: Mapped[str | None] = mapped_column(String(255))
    """AES-GCM(TOTP secret). Плейнтекст показывается один раз при подключении 2FA."""
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_ip: Mapped[str | None] = mapped_column(String(64))
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Referral(IntPkMixin, Base):
    __tablename__ = "referrals"

    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    referred_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    status: Mapped[ReferralStatus] = enum_column(
        ReferralStatus, nullable=False, default=ReferralStatus.PENDING
    )
    reward_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    """Заказ приглашённого, который «раскрыл» вознаграждение."""
    rewarded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_referrals_referrer_id", "referrer_id"),)
