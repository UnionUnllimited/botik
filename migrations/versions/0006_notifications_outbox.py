"""Очередь сообщений клиенту: отправляет их бот, а не мы

Своего бота у нас больше нет — клиент разговаривает с ботом стороннего
продукта, и токен есть только у него. Поэтому напоминания об окончании
подписки, подтверждения оплаты и служебные алерты складываются в очередь,
а бот раз в несколько секунд забирает пачку и отправляет.

Индекс по (sent_at, id) — под единственный горячий запрос: «что ещё
не отправлено, по порядку».

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("buttons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index("ix_notifications_pending", "notifications", ["sent_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_notifications_pending", table_name="notifications")
    op.drop_table("notifications")
