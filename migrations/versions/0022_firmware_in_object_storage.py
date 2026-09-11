"""Образ прошивки может раздаваться из объектного хранилища

Образ весит 27–54 МБ, и за ним приходит весь парк разом. Отдавать его со
своего сервера значит выкладывать этот трафик на тот же канал, по которому
работают витрина, панель и туннели к роутерам.

Колонка хранит готовый адрес, а не признак «уехал»: бакет и CDN оператор
может сменить, а роутеры в этот момент качают по ссылкам, которые уже
прочитали из манифеста.

Пусто — образ раздаётся с нашего домена, как и раньше. Поэтому у выпусков,
заведённых до этой миграции, ничего не меняется.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "firmware_images",
        sa.Column("remote_url", sa.String(length=500), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("firmware_images", "remote_url")
