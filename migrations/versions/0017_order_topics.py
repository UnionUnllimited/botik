"""Топики заказов в рабочем чате оператора

Оператор работает с телефона, а веб-админка на телефоне — это форма, в которую
надо попасть пальцем. Поэтому у каждого заказа заводится свой топик в чате:
там карточка с кнопками, туда же приходят изменения.

Очередь `notifications` служит и для этого: слать в Telegram мы не умеем —
токен только у бота, — а он и так забирает её раз в десять секунд. Добавляются
адрес чата, топик и название топика, если его ещё надо создать.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS tg_topic_id INTEGER")
    op.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS chat_id BIGINT")
    op.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS thread_id INTEGER")
    op.execute(
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS topic_title VARCHAR(128) "
        "NOT NULL DEFAULT ''"
    )
    op.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS order_id BIGINT")


def downgrade() -> None:
    op.execute("ALTER TABLE notifications DROP COLUMN IF EXISTS order_id")
    op.execute("ALTER TABLE notifications DROP COLUMN IF EXISTS topic_title")
    op.execute("ALTER TABLE notifications DROP COLUMN IF EXISTS thread_id")
    op.execute("ALTER TABLE notifications DROP COLUMN IF EXISTS chat_id")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS tg_topic_id")
