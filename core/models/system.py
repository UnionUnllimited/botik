"""Системные таблицы: настройки и аудит действий."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
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


class Notification(BigIntPkMixin, Base):
    """Сообщение клиенту, которое ждёт отправки ботом.

    Своего бота у нас больше нет: клиент разговаривает с ботом стороннего
    продукта, и токен есть только у него. Слать напрямую мы не можем и не должны —
    сообщение от неизвестного бота человек в лучшем случае не узнает.

    Поэтому очередь: мы кладём готовый текст, бот раз в несколько секунд
    забирает пачку, отправляет и отчитывается. Заодно это переживает и его
    перезапуск, и обрыв связи — напоминание об окончании подписки не должно
    теряться из-за того, что бота в этот момент обновляли.
    """

    __tablename__ = "notifications"

    tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    buttons: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    """Клиенту — только ссылки: [{"text": "...", "url": "..."}]. Callback
    обрабатывает бот, а он сменный — кнопка с уехавшим обработчиком молча
    перестала бы работать, и человек остался бы с мёртвым экраном.

    В рабочий чат оператора (`chat_id` задан) уходят и callback-кнопки:
    [{"text": "...", "data": "ord:12:track"}]. Там это допустимо — чат наш,
    обработчик наш, и сломанную кнопку видит тот, кто её и чинит."""

    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    """Куда слать, если не клиенту: рабочий чат с топиками по заказам.
    Пусто — обычное сообщение клиенту по `tg_id`, как было."""

    thread_id: Mapped[int | None] = mapped_column(Integer)
    """Топик в этом чате. Пусто вместе с `topic_title` — сообщение уйдёт
    в общую ленту чата."""

    topic_title: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    """Непусто — топик надо сначала создать, и это его название. Создать может
    только бот: право на топики есть у него, а у нас нет даже токена."""

    order_id: Mapped[int | None] = mapped_column(BigInteger)
    """Чей это топик. По нему отчёт бота записывает номер созданного топика
    в заказ — иначе следующее сообщение завело бы второй топик тому же заказу.

    Без внешнего ключа намеренно: очередь переживает удаление заказа, и
    сообщение, ушедшее по удалённому, не должно мешать удалению."""
    kind: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    """Зачем отправлено: reminder, payment, order, admin. Нужно для разбора жалоб."""

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_notifications_pending", "sent_at", "id"),)
