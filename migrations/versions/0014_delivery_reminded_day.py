"""Move the delivery reminder marker into its own migration.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-24

``reminded_day`` was initially appended to migration 0013 after that revision
had already been deployed.  Alembic does not rerun an applied revision, so
production remained on a schema without the column even though ``upgrade
head`` succeeded.  The guards also support databases that were freshly built
from the short-lived amended 0013.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE deliveries ADD COLUMN IF NOT EXISTS reminded_day INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE deliveries DROP COLUMN IF EXISTS reminded_day")
