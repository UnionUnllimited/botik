"""Системные таблицы: настройки и аудит действий."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.enums import ActorType
from core.models.base import Base, BigIntPkMixin, enum_column


class Setting(Base):
    """Настройки, которые меняются без деплоя (цены доставки, тексты, лимиты)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    """Всегда объект вида {"value": ...} — так в jsonb ложатся и числа, и списки."""
    description: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    updated_by_admin_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditLog(BigIntPkMixin, Base):
    """Кто, что и когда изменил. Пишется на все действия с деньгами и подписками."""

    __tablename__ = "audit_log"

    actor_type: Mapped[ActorType] = enum_column(ActorType, nullable=False, default=ActorType.ADMIN)
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    actor_tg_id: Mapped[int | None] = mapped_column(BigInteger)

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    old_value: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    new_value: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)

    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_audit_log_admin_id_created_at", "admin_id", "created_at"),
        Index("ix_audit_log_action_created_at", "action", "created_at"),
    )
