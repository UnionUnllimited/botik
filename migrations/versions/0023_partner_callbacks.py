"""Очередь чужих колбэков: платежи бота приходят к нам

Провайдер шлёт уведомления по одному адресу на мерчанта — нашему. Железо
продаём мы, подписку продаёт бот, и его платежи надо передавать ему.

Передавали HTTP-запросом на `127.0.0.1:8081`, и из контейнера это не работает
и работать не может: для процесса внутри `127.0.0.1` — это он сам, а бот
слушает петлю хоста. Публичного адреса у бота нет.

Теперь направление развёрнуто: колбэк ложится сюда, а бот забирает его сам —
тем же способом, каким уже забирает очередь сообщений. Это направление
работает всегда.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "partner_callbacks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("transaction_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "headers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    # Выборка всегда одна: недоставленные, по порядку. Индекс под неё —
    # очередь читается каждые несколько секунд, а растёт бесконечно.
    op.create_index("ix_partner_callbacks_pending", "partner_callbacks", ["delivered_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_partner_callbacks_pending", table_name="partner_callbacks")
    op.drop_table("partner_callbacks")
