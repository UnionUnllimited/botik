"""Просьбы клиентов о сайтах и отзывы о роутере

«Не открывается сайт» из бота и приложения теперь заканчивается не сообщением
в чате, которое пролистают, а записью: оператор видит её в админке и в топике
«Роутеры», а клиент получает ответ, когда сайт добавили.

Отзыв спрашивается раз на роутер, через неделю после включения.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("value", sa.String(255), nullable=False),
        sa.Column("covered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="new"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(120), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_site_requests_status_created", "site_requests", ["status", "created_at"])
    # Одна ждущая просьба на сайт от клиента — и при двойном нажатии.
    op.create_index(
        "uq_site_requests_pending",
        "site_requests",
        ["user_id", "value"],
        unique=True,
        postgresql_where=sa.text("status = 'new'"),
    )

    op.create_table(
        "router_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, unique=True
        ),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_router_reviews_rated", "router_reviews", ["rated_at"])


def downgrade() -> None:
    op.drop_index("ix_router_reviews_rated", table_name="router_reviews")
    op.drop_table("router_reviews")
    op.drop_index("uq_site_requests_pending", table_name="site_requests")
    op.drop_index("ix_site_requests_status_created", table_name="site_requests")
    op.drop_table("site_requests")
