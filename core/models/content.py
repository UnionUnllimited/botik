"""Контент: статьи FAQ/инструкций и рассылки."""

from __future__ import annotations

import datetime as dt
from typing import Any

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

from core.enums import BroadcastStatus, BroadcastTargetStatus, MediaType
from core.models.base import Base, BigIntPkMixin, IntPkMixin, TimestampMixin, enum_column


class Article(IntPkMixin, TimestampMixin, Base):
    """Статья инструкций/FAQ. Дерево строится через parent_id."""

    __tablename__ = "articles"

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    media_type: Mapped[MediaType] = enum_column(MediaType, nullable=False, default=MediaType.TEXT)
    file_id: Mapped[str | None] = mapped_column(String(255))
    attachments: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    show_support_button: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    children: Mapped[list[Article]] = relationship(back_populates="parent", order_by="Article.sort_order")
    parent: Mapped[Article | None] = relationship(back_populates="children", remote_side="Article.id")

    __table_args__ = (Index("ix_articles_parent_id_sort_order", "parent_id", "sort_order"),)


class Broadcast(IntPkMixin, TimestampMixin, Base):
    __tablename__ = "broadcasts"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    media_type: Mapped[MediaType] = enum_column(MediaType, nullable=False, default=MediaType.TEXT)
    file_id: Mapped[str | None] = mapped_column(String(255))
    buttons: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    """Инлайн-кнопки: {"rows": [[{"text": "...", "url": "..."}]]}."""
    segment: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    """Фильтр аудитории: {"kind": "expiring", "days": 7} и т.п."""

    status: Mapped[BroadcastStatus] = enum_column(
        BroadcastStatus, nullable=False, default=BroadcastStatus.DRAFT
    )
    scheduled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    total_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocked_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_by_admin_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))

    targets: Mapped[list[BroadcastTarget]] = relationship(
        back_populates="broadcast", cascade="all, delete-orphan"
    )


class BroadcastTarget(BigIntPkMixin, Base):
    """Получатель рассылки. UNIQUE не даёт отправить одно сообщение дважды."""

    __tablename__ = "broadcast_targets"

    broadcast_id: Mapped[int] = mapped_column(ForeignKey("broadcasts.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[BroadcastTargetStatus] = enum_column(
        BroadcastTargetStatus, nullable=False, default=BroadcastTargetStatus.PENDING
    )
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    broadcast: Mapped[Broadcast] = relationship(back_populates="targets")

    __table_args__ = (
        UniqueConstraint("broadcast_id", "user_id", name="uq_broadcast_targets_broadcast_id_user_id"),
        Index("ix_broadcast_targets_broadcast_id_status", "broadcast_id", "status"),
    )
