"""Устройства (роутеры), команды им, телеметрия и лог обращений за подпиской."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.enums import CommandStatus, CommandType, DeviceServiceStatus, DeviceStatus
from core.models.base import (
    Base,
    BigIntPkMixin,
    IntPkMixin,
    TimestampMixin,
    enum_column,
)

if TYPE_CHECKING:
    from core.models.subscription import Subscription
    from core.models.user import User


class Device(IntPkMixin, TimestampMixin, Base):
    """Роутер клиента. Идентифицируется MAC-адресом с корпуса."""

    __tablename__ = "devices"

    mac: Mapped[str] = mapped_column(String(17), unique=True, nullable=False)
    """Канонический формат AA:BB:CC:DD:EE:FF."""
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    status: Mapped[DeviceStatus] = enum_column(DeviceStatus, nullable=False, default=DeviceStatus.NEW)

    model: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    serial: Mapped[str | None] = mapped_column(String(64))
    fw_version: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    panel_version: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    secret_enc: Mapped[str | None] = mapped_column(String(512))
    """AES-GCM(device_secret). Сервер обязан знать значение, чтобы проверить HMAC запроса."""
    secret_rotated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    sub_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    """SHA-256 действующего токена подписки. Сам токен в БД не хранится."""
    prev_sub_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    prev_sub_token_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    """Старый токен ещё работает N часов после ротации, чтобы не оборвать роутер."""
    sub_token_rotated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    node_group_id: Mapped[int | None] = mapped_column(ForeignKey("node_groups.id", ondelete="SET NULL"))
    config_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    activated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_sub_fetch_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_wan_ip: Mapped[str | None] = mapped_column(String(45))
    wan_proto: Mapped[str | None] = mapped_column(String(16))
    service_status: Mapped[DeviceServiceStatus] = enum_column(
        DeviceServiceStatus, nullable=False, default=DeviceServiceStatus.UNKNOWN
    )
    uptime_sec: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    rx_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tx_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    load_avg: Mapped[float | None] = mapped_column(Float)
    active_nodes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    admin_note: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User | None] = relationship(back_populates="devices")
    commands: Mapped[list[DeviceCommand]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )
    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="device")

    __table_args__ = (
        Index("ix_devices_user_id", "user_id"),
        Index("ix_devices_order_id", "order_id"),
        Index("ix_devices_last_heartbeat_at", "last_heartbeat_at"),
        Index("ix_devices_status", "status"),
    )

    def is_online(self, *, threshold_min: int, now: dt.datetime | None = None) -> bool:
        if self.last_heartbeat_at is None:
            return False
        current = now or dt.datetime.now(dt.UTC)
        last = self.last_heartbeat_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.UTC)
        return (current - last) <= dt.timedelta(minutes=threshold_min)


class DeviceCommand(IntPkMixin, Base):
    """Команда устройству. Роутер забирает её сам в ответе на heartbeat."""

    __tablename__ = "device_commands"

    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    command: Mapped[CommandType] = enum_column(CommandType, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    status: Mapped[CommandStatus] = enum_column(CommandStatus, nullable=False, default=CommandStatus.PENDING)

    created_by_admin_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    device: Mapped[Device] = relationship(back_populates="commands")

    __table_args__ = (Index("ix_device_commands_device_id_status", "device_id", "status"),)


class Heartbeat(BigIntPkMixin, Base):
    """Сырая телеметрия. Чистится по расписанию (SUBSCRIPTION_HEARTBEAT_RETENTION_DAYS)."""

    __tablename__ = "heartbeats"

    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    uptime_sec: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    fw_version: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    panel_version: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    wan_ip: Mapped[str | None] = mapped_column(String(45))
    wan_proto: Mapped[str | None] = mapped_column(String(16))
    service_status: Mapped[DeviceServiceStatus] = enum_column(
        DeviceServiceStatus, nullable=False, default=DeviceServiceStatus.UNKNOWN
    )
    active_nodes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rx_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tx_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    load_avg: Mapped[float | None] = mapped_column(Float)
    remote_ip: Mapped[str | None] = mapped_column(String(45))

    __table_args__ = (Index("ix_heartbeats_device_id_created_at", "device_id", "created_at"),)


class SubscriptionAccessLog(BigIntPkMixin, Base):
    """Кто и откуда скачивал подписку — основа антифрод-проверки по числу IP."""

    __tablename__ = "subscription_access_log"

    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    remote_ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    nodes_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), default="ok", nullable=False)
    """ok / empty_inactive / unknown_token / revoked."""
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_subscription_access_log_device_id_created_at", "device_id", "created_at"),)
