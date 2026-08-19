"""Модель устройства заполняется из имени платы

Телеметрия роутера приезжает с полем `board` и складывалась в одноимённую
колонку, а все экраны показывают `model` — и его не заполнял никто. В парке
на месте модели стояло «—» у каждого устройства.

Дальше это делает опрос, но у тех, что уже опрошены, `board` заполнен,
а `model` пуст — переносим один раз. Заполненный `model` не трогаем:
оператор мог назвать устройство по-своему.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE devices SET model = board "
        "WHERE (model IS NULL OR model = '') AND board IS NOT NULL AND board <> ''"
    )


def downgrade() -> None:
    # Снимаем ровно те, что совпадают с именем платы: это и есть перенесённые.
    # Если оператор вписал руками то же самое — значение от этого не меняется.
    op.execute("UPDATE devices SET model = '' WHERE board IS NOT NULL AND model = board")
