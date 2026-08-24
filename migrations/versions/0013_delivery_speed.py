"""Доставка выбирается скоростью, а цена называется после заказа

Тарифные зоны прожили неделю и оказались не тем, что нужно: цену по ним всё
равно приходилось перебивать руками, а незнакомый город останавливал оформление
у живого клиента. Решение заказчика от 21 августа 2026 — считать доставку
вручную по каждому заказу.

Клиент теперь выбирает не перевозчика и не зону, а скорость: быстро и дороже
или дешевле, но ждать ближайшего понедельника. Перевозчик остаётся заботой
оператора — он зависит от города, веса и действующего договора.

`quoted_at` отдельно от `price`: без этой отметки ноль читается как
«доставка бесплатна», а на деле означает «ещё не считали», и заказ ушёл бы
в сборку неоплаченным.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deliveries",
        sa.Column("speed", sa.String(length=32), nullable=False, server_default="fast"),
    )
    op.add_column("deliveries", sa.Column("quoted_at", sa.DateTime(timezone=True)))
    op.add_column("deliveries", sa.Column("paid_at", sa.DateTime(timezone=True)))

    # Заказы, оформленные при зонах, доставку уже оплатили вместе с товаром:
    # цена у них посчитана и деньги получены, второй раз просить нельзя.
    op.execute("UPDATE deliveries SET quoted_at = created_at, paid_at = created_at WHERE price > 0")

    op.drop_column("deliveries", "zone")

    op.drop_table("delivery_unknown_cities")
    op.drop_index("ix_delivery_zone_prices_zone_method", table_name="delivery_zone_prices")
    op.drop_table("delivery_zone_prices")
    op.drop_table("delivery_zones")


def downgrade() -> None:
    op.add_column(
        "deliveries",
        sa.Column("zone", sa.String(length=32), nullable=False, server_default=""),
    )
    op.drop_column("deliveries", "paid_at")
    op.drop_column("deliveries", "quoted_at")
    op.drop_column("deliveries", "speed")

    # Зоны заводятся пустыми: их содержимое было в миграции 0012, и
    # восстанавливать его здесь значило бы держать две копии одного списка.
    zones = op.create_table(
        "delivery_zones",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("code", sa.String(length=32), nullable=False, unique=True),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("cities", sa.Text(), nullable=False, server_default=""),
        sa.Column("days", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "delivery_zone_prices",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "zone_id",
            sa.BigInteger(),
            sa.ForeignKey("delivery_zones.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("method", sa.String(length=32), nullable=False),
        sa.Column("pvz_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("courier_price", sa.Numeric(12, 2), nullable=False),
    )
    op.create_index(
        "ix_delivery_zone_prices_zone_method",
        "delivery_zone_prices",
        ["zone_id", "method"],
        unique=True,
    )
    op.create_table(
        "delivery_unknown_cities",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("city", sa.String(length=120), nullable=False),
        sa.Column("normalized", sa.String(length=120), nullable=False, unique=True),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("tg_id", sa.BigInteger()),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    del zones
