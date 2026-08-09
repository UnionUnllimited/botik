"""Вход на сайт по почте: tg_id перестаёт быть обязательным

Клиент больше не приходит только из Telegram, поэтому tg_id становится
nullable, а логином выступает почта с паролем Argon2id. У клиентов, заведённых
ботом, почты нет — она появится, когда они привяжут её разовым входом
через Telegram. NULL в уникальном индексе Postgres не конфликтуют,
поэтому таких записей может быть сколько угодно.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("users", "tg_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column("users", sa.Column("email", sa.String(length=254), nullable=True))
    op.add_column("users", sa.Column("password_hash", sa.String(length=255), nullable=True))
    op.create_unique_constraint(op.f("uq_users_email"), "users", ["email"])


def downgrade() -> None:
    op.drop_constraint(op.f("uq_users_email"), "users", type_="unique")
    op.drop_column("users", "password_hash")
    op.drop_column("users", "email")
    # Откат возможен, только пока нет клиентов, зарегистрированных на сайте:
    # у них tg_id пустой, и NOT NULL на такую таблицу не встанет.
    op.alter_column("users", "tg_id", existing_type=sa.BigInteger(), nullable=False)
