"""История правок своего списка доменов

Список правится текстом целиком, и «убрал лишнее» неотличимо от «стёр половину
и не заметил». Журнала действий у нас больше нет, а домен здесь открывает
доступ всему парку — вопрос «кто это добавил и когда» задают первым делом.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manual_list_revisions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("author", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("added", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("removed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_manual_list_revisions_kind_id", "manual_list_revisions", ["kind", "id"])


def downgrade() -> None:
    op.drop_index("ix_manual_list_revisions_kind_id", table_name="manual_list_revisions")
    op.drop_table("manual_list_revisions")
