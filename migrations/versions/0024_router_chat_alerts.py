"""Роутеры в рабочем чате: какая тревога ушла и когда

Заказы приходят в топики рабочего чата с кнопками. Роутеры теперь тоже:
«молчит у клиента» и «на связи, а подписка не включилась» — карточкой
в топике «Роутеры», а когда роутер вернулся — коротким сообщением.

Помнить надо две вещи: о чём уже сказали (иначе один молчащий роутер шлёт
карточку каждый круг) и с какого момента (чтобы написать «молчал 26 ч»).
Колонки у самого устройства, а не отдельная таблица: тревога у роутера одна
за раз, и сбрасывается она вместе с его возвращением.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("chat_alert_kind", sa.String(length=16), nullable=True))
    op.add_column("devices", sa.Column("chat_alert_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "chat_alert_at")
    op.drop_column("devices", "chat_alert_kind")
