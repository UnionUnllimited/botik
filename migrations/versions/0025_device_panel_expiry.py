"""Срок учётки роутера в панели — у самого роутера, сверкой раз в 10 минут

Парк писал «подписка: нет» у роутеров, которые работают: их включали вручную
до того, как ручная активация стала заводить нашу подписку, или они вообще
без клиента — «на складе», а стоят у кого-то дома. Срок доступа у них есть,
но знает его только панель.

Спрашивать панель на каждую строку списка нельзя — это десятки запросов на
страницу. Поэтому срок сверяется одним запросом на весь парк и лежит у
роутера; список и карточка показывают его, когда своей подписки нет.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("panel_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("devices", sa.Column("panel_synced_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "panel_synced_at")
    op.drop_column("devices", "panel_expires_at")
