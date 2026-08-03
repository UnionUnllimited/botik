"""Поддержка: тикеты и переписка через группу с топиками."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.enums import MediaType, MessageDirection, TicketStatus
from core.models.base import Base, IntPkMixin, TimestampMixin, enum_column

if TYPE_CHECKING:
    from core.models.user import User


class Ticket(IntPkMixin, TimestampMixin, Base):
    """Обращение клиента. Каждому соответствует топик в группе поддержки."""

    __tablename__ = "tickets"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[TicketStatus] = enum_column(TicketStatus, nullable=False, default=TicketStatus.OPEN)
    subject: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    topic_id: Mapped[int | None] = mapped_column(BigInteger)
    """message_thread_id топика в супергруппе поддержки."""

    assigned_admin_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_message_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="tickets")
    messages: Mapped[list[TicketMessage]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", order_by="TicketMessage.id"
    )

    __table_args__ = (
        Index("ix_tickets_user_id", "user_id"),
        Index("ix_tickets_status_last_message_at", "status", "last_message_at"),
        Index("ix_tickets_topic_id", "chat_id", "topic_id"),
    )


class TicketMessage(IntPkMixin, Base):
    __tablename__ = "ticket_messages"

    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False)
    direction: Mapped[MessageDirection] = enum_column(MessageDirection, nullable=False)
    media_type: Mapped[MediaType] = enum_column(MediaType, nullable=False, default=MediaType.TEXT)

    admin_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    admin_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    """Кто ответил из группы — админ может не иметь учётки в веб-админке."""

    text: Mapped[str | None] = mapped_column(Text)
    file_id: Mapped[str | None] = mapped_column(String(255))
    file_name: Mapped[str | None] = mapped_column(String(255))
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    delivered_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticket: Mapped[Ticket] = relationship(back_populates="messages")

    __table_args__ = (Index("ix_ticket_messages_ticket_id", "ticket_id"),)
