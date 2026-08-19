"""Отпечаток своего списка в записи сборки

Круг пропускался, если не изменился ни один источник, — а свой список в это
«изменилось» не входил вовсе. Оператор дописывал домен, и до роутеров он
не доезжал никогда: источники-то прежние.

Отпечаток сравнивается с прошлой сборкой, и правка своего списка теперь
такой же повод пересобрать, как новая версия источника.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "domain_builds",
        sa.Column("manual_hash", sa.String(length=64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("domain_builds", "manual_hash")
