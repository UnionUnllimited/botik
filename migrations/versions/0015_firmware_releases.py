"""Выпуски прошивки: манифест для роутеров и образы по моделям

Роутеры обновляются сами: раз в сутки берут один JSON по постоянному адресу
и действуют по нему. От панели нужны номер версии, доля раскатки и набор
образов — больше в манифест ничего не уходит, и больше здесь ничего нет.

`sha256` и `size_bytes` пишет только сервер, при загрузке файла: ошибка
в одном знаке тихо отменяет обновление у всего парка.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "firmware_releases",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, unique=True),
        sa.Column("notes", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("rollout", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rollout_max", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.String(length=120), nullable=False, server_default=""),
    )
    op.create_index("ix_firmware_releases_published_at", "firmware_releases", ["published_at"])

    op.create_table(
        "firmware_images",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "release_id",
            sa.BigInteger(),
            sa.ForeignKey("firmware_releases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model_key", sa.String(length=64), nullable=False),
        sa.Column("file_name", sa.String(length=200), nullable=False),
        sa.Column("url_path", sa.String(length=300), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "release_id", "model_key", name="uq_firmware_images_release_id_model_key"
        ),
    )


def downgrade() -> None:
    op.drop_table("firmware_images")
    op.drop_index("ix_firmware_releases_published_at", table_name="firmware_releases")
    op.drop_table("firmware_releases")
