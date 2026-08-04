"""Индивидуальный пароль SSH роутера

По умолчанию пароль root выводится из MAC с солью — так его назначает
прошивка. Колонка нужна для исключений: роутер с нестандартным паролем.
Значение хранится зашифрованным AES-GCM.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("ssh_password_enc", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "ssh_password_enc")
