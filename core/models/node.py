"""Пул серверов (узлов), группы и точечные назначения."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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

from core.enums import NodeProtocol, NodeStatus
from core.models.base import Base, IntPkMixin, TimestampMixin, enum_column


class NodeGroup(IntPkMixin, TimestampMixin, Base):
    """Набор узлов, который выдаётся тарифу или конкретному устройству."""

    __tablename__ = "node_groups"

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_nodes_per_device: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    """Сколько узлов максимум попадёт в выдачу подписки одному устройству."""

    assignments: Mapped[list[NodeAssignment]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class Node(IntPkMixin, TimestampMixin, Base):
    """Сервер доступа. Параметры подключения лежат в `config` как есть."""

    __tablename__ = "nodes"

    remarks: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    """Имя узла в выдаче подписки. Обязан начинаться с префикса Router_."""
    protocol: Mapped[NodeProtocol] = enum_column(
        NodeProtocol, nullable=False, default=NodeProtocol.VLESS_REALITY
    )
    location: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), default="", nullable=False)

    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    """uuid, flow, sni, public_key, short_id, fingerprint, path — всё, что нужно для ссылки."""

    status: Mapped[NodeStatus] = enum_column(NodeStatus, nullable=False, default=NodeStatus.ACTIVE)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    device_limit: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    """0 — без ограничения."""
    devices_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    """Денормализованный счётчик выданных устройств, пересчитывается воркером."""

    is_healthy: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_check_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    admin_note: Mapped[str | None] = mapped_column(Text)

    assignments: Mapped[list[NodeAssignment]] = relationship(
        back_populates="node", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("port > 0 AND port < 65536", name="port_range"),
        Index("ix_nodes_status_priority", "status", "priority"),
    )

    @property
    def is_available(self) -> bool:
        if self.status is not NodeStatus.ACTIVE or not self.is_healthy:
            return False
        return self.device_limit == 0 or self.devices_count < self.device_limit


class NodeAssignment(IntPkMixin, Base):
    """Связь узла с группой ИЛИ с конкретным устройством (ровно одно из двух)."""

    __tablename__ = "node_assignments"

    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("node_groups.id", ondelete="CASCADE"))
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    node: Mapped[Node] = relationship(back_populates="assignments")
    group: Mapped[NodeGroup | None] = relationship(back_populates="assignments")

    __table_args__ = (
        CheckConstraint(
            "(group_id IS NOT NULL AND device_id IS NULL) OR (group_id IS NULL AND device_id IS NOT NULL)",
            name="group_xor_device",
        ),
        UniqueConstraint("node_id", "group_id", name="uq_node_assignments_node_id_group_id"),
        UniqueConstraint("node_id", "device_id", name="uq_node_assignments_node_id_device_id"),
        Index("ix_node_assignments_group_id", "group_id"),
        Index("ix_node_assignments_device_id", "device_id"),
    )
