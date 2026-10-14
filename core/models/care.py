"""Забота о клиенте после покупки: просьбы о сайтах и отзывы о роутере.

Обе таблицы — память о разговоре, который начался в боте и закончился
у оператора. Без записи просьба «добавьте сайт» жила бы только сообщением
в рабочем чате: пролистали — забыли, а клиент ждёт. Отзыв без записи
спрашивался бы у одного человека дважды.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base, IntPkMixin, TimestampMixin


class SiteRequest(IntPkMixin, TimestampMixin, Base):
    """Клиент сообщил, что сайт не открывается, и попросил разобраться."""

    __tablename__ = "site_requests"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    """`proxy_domain` или `proxy_ip` — в какой список добавлять."""

    value: Mapped[str] = mapped_column(String(255), nullable=False)
    """Уже очищенное: `youtube.com`, а не ссылка с путём, как её прислали."""

    covered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """Был ли сайт в списках, когда клиент пожаловался. Да — дело не
    в списках, и оператору нужна другая кнопка: не «добавить», а «написать»."""

    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="new", nullable=False)
    """new — ждёт оператора; added — добавлен в свой список; declined — не
    добавляем; done — разобрались иначе."""

    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    __table_args__ = (
        Index("ix_site_requests_status_created", "status", "created_at"),
        # Одна ждущая просьба на сайт от клиента — и при двойном нажатии,
        # пришедшем двумя запросами разом.
        Index(
            "uq_site_requests_pending",
            "user_id",
            "value",
            unique=True,
            postgresql_where=text("status = 'new'"),
            sqlite_where=text("status = 'new'"),
        ),
    )


class RouterReview(IntPkMixin, TimestampMixin, Base):
    """Оценка роутера клиентом через неделю после включения.

    Одна на роутер: спросили — запись есть, и второй раз не спросим, даже
    если клиент не ответил. Напоминать о просьбе оценить — навязчивость,
    которая испортит ровно ту оценку, ради которой спрашивали."""

    __tablename__ = "router_reviews"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    rating: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    asked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    rated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_router_reviews_rated", "rated_at"),)
