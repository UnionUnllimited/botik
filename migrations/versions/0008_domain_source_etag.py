"""Метка версии источника: частый круг без долбёжа чужого GitHub

Круг раз в час означал, что правка доезжает до роутера почти за два часа:
час до сборки и ещё до часа, пока роутер придёт за списком. Круг чаще упирался
бы в лимиты: 26 источников каждые несколько минут — это 429 и списки с дырами.

Поэтому у источника запоминается `ETag`. В следующий раз он уходит заголовком
`If-None-Match`, и неизменившийся файл отвечает `304` без тела: такой запрос
дёшев и для нас, и для отдающей стороны.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "domain_sources",
        sa.Column("etag", sa.String(length=200), nullable=False, server_default=""),
    )
    op.add_column(
        "domain_builds",
        sa.Column("skipped", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("domain_builds", "skipped")
    op.drop_column("domain_sources", "etag")
