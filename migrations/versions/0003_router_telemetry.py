"""Телеметрия роутеров и доступ через frp

Роутеры держат обратный туннель к frps; админка снимает с них показания
и ведёт журнал подключений. Поля названы по нашей терминологии, даже если
прошивка отдаёт их иначе.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("board", sa.String(length=64), nullable=True))
    op.add_column("devices", sa.Column("cpu_pct", sa.Integer(), nullable=True))
    op.add_column("devices", sa.Column("ram_pct", sa.Integer(), nullable=True))
    op.add_column("devices", sa.Column("temp_c", sa.Float(), nullable=True))
    op.add_column(
        "devices",
        sa.Column("clients_wifi", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "devices",
        sa.Column("clients_dhcp", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "devices",
        sa.Column("tunnel_running", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("devices", sa.Column("frp_luci_name", sa.String(length=64), nullable=True))
    op.add_column("devices", sa.Column("frp_ssh_name", sa.String(length=64), nullable=True))
    op.add_column("devices", sa.Column("frp_visitor_port", sa.Integer(), nullable=True))
    op.add_column(
        "devices",
        sa.Column("frp_online", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "devices", sa.Column("frp_last_seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("devices", sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint(
        "uq_devices_frp_visitor_port", "devices", ["frp_visitor_port"]
    )

    op.add_column("heartbeats", sa.Column("cpu_pct", sa.Integer(), nullable=True))
    op.add_column("heartbeats", sa.Column("ram_pct", sa.Integer(), nullable=True))
    op.add_column("heartbeats", sa.Column("temp_c", sa.Float(), nullable=True))
    op.add_column(
        "heartbeats",
        sa.Column("clients_wifi", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "heartbeats",
        sa.Column("clients_dhcp", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "heartbeats",
        sa.Column("source", sa.String(length=16), server_default="device", nullable=False),
    )

    op.create_table(
        "device_events",
        sa.Column("device_id", sa.Integer(), nullable=True),
        sa.Column("mac", sa.String(length=17), nullable=True),
        sa.Column("level", sa.String(length=16), server_default="info", nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_device_events_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_events")),
    )
    op.create_index(
        "ix_device_events_created_at", "device_events", ["created_at"], unique=False
    )
    op.create_index(
        "ix_device_events_device_id_created_at",
        "device_events",
        ["device_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_device_events_device_id_created_at", table_name="device_events")
    op.drop_index("ix_device_events_created_at", table_name="device_events")
    op.drop_table("device_events")

    for column in ("source", "clients_dhcp", "clients_wifi", "temp_c", "ram_pct", "cpu_pct"):
        op.drop_column("heartbeats", column)

    op.drop_constraint("uq_devices_frp_visitor_port", "devices", type_="unique")
    for column in (
        "last_poll_at",
        "frp_last_seen_at",
        "frp_online",
        "frp_visitor_port",
        "frp_ssh_name",
        "frp_luci_name",
        "tunnel_running",
        "clients_dhcp",
        "clients_wifi",
        "temp_c",
        "ram_pct",
        "cpu_pct",
        "board",
    ):
        op.drop_column("devices", column)
