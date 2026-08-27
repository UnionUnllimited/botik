"""Номер сборки прошивки на роутере

Отдельная колонка, а не разбор `fw_version`: там `25.12.3` — версия базы,
она читается человеком и ни с чем не сравнивается. Номер сборки — целое,
то же самое, что `version` в манифесте обновлений, и только по нему видно,
обновился роутер или ещё нет.

NULL — прошивка номер не сообщает либо роутер ни разу не отвечал. Не ноль:
нулём пришлось бы считать все молчащие, и они попали бы в «отстают».

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS fw_build INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE devices DROP COLUMN IF EXISTS fw_build")
