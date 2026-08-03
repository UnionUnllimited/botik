"""Срок жизни платёжной ссылки

Provider возвращает время жизни ссылки (у PLATEGA — 15 минут); храним
абсолютный момент истечения, чтобы гасить зависшие платежи по расписанию.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_payments_status_expires_at",
        "payments",
        ["status", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_payments_status_expires_at", table_name="payments")
    op.drop_column("payments", "expires_at")
