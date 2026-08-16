"""Списки доменов: источники, свой список и история сборок

Раньше набор источников был массивом в скрипте на сервере frps, а свой
список правился через веб-интерфейс GitHub. Отключить категорию значило
зайти на сервер; добавить домен — сделать коммит в чужой репозиторий.

Источники заводятся сразу — тем же набором, что был в скрипте: иначе
после выката сборка соберёт пустоту, и роутеры получат список короче
прежнего. Свой список создаётся пустым: его содержимое перенесёт оператор,
угадывать за него нечего.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DOMAINS = "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Categories/"
_SERVICES = "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Services/"
_SUBNETS = "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Subnets/IPv4/"

_SEED: tuple[tuple[str, str, str], ...] = (
    (f"{_DOMAINS}block.lst", "Заблокированное", "domain"),
    (f"{_DOMAINS}anime.lst", "Аниме", "domain"),
    (f"{_DOMAINS}geoblock.lst", "Геоблок", "domain"),
    (f"{_DOMAINS}hodca.lst", "Hodca", "domain"),
    (f"{_DOMAINS}news.lst", "Новости", "domain"),
    (f"{_DOMAINS}porn.lst", "Для взрослых", "domain"),
    (f"{_SERVICES}discord.lst", "Discord", "domain"),
    (f"{_SERVICES}google_ai.lst", "Google AI", "domain"),
    (f"{_SERVICES}google_meet.lst", "Google Meet", "domain"),
    (f"{_SERVICES}google_play.lst", "Google Play", "domain"),
    (f"{_SERVICES}hdrezka.lst", "HDrezka", "domain"),
    (f"{_SERVICES}hetzner.lst", "Hetzner", "domain"),
    (f"{_SERVICES}meta.lst", "Meta", "domain"),
    (f"{_SERVICES}roblox.lst", "Roblox", "domain"),
    (f"{_SERVICES}telegram.lst", "Telegram", "domain"),
    (f"{_SERVICES}tiktok.lst", "TikTok", "domain"),
    (f"{_SERVICES}twitter.lst", "Twitter", "domain"),
    (f"{_SUBNETS}telegram.lst", "Подсети Telegram", "ip"),
    (f"{_SUBNETS}Meta.lst", "Подсети Meta", "ip"),
    (f"{_SUBNETS}Discord.lst", "Подсети Discord", "ip"),
    (f"{_SUBNETS}cloudflare.lst", "Подсети Cloudflare", "ip"),
    (f"{_SUBNETS}cloudfront.lst", "Подсети CloudFront", "ip"),
    (f"{_SUBNETS}digitalocean.lst", "Подсети DigitalOcean", "ip"),
    (f"{_SUBNETS}ovh.lst", "Подсети OVH", "ip"),
    (f"{_SUBNETS}roblox.lst", "Подсети Roblox", "ip"),
    (f"{_SUBNETS}google_meet.lst", "Подсети Google Meet", "ip"),
)


def upgrade() -> None:
    sources = op.create_table(
        "domain_sources",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("url", sa.String(length=500), nullable=False, unique=True),
        sa.Column("title", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="domain"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("last_ok_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("last_lines", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_domain_sources_kind_enabled", "domain_sources", ["kind", "is_enabled"])

    manual = op.create_table(
        "manual_lists",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False, unique=True),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "domain_builds",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("domains", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ips", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_sources", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("uploaded", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.bulk_insert(
        sources,
        [
            {"url": url, "title": title, "kind": kind, "is_enabled": True, "sort_order": (i + 1) * 10}
            for i, (url, title, kind) in enumerate(_SEED)
        ],
    )
    # Свой список заводится пустыми строками, чтобы страница открывалась
    # до первой правки: `kind` уникален, и создавать их по требованию значило
    # бы ловить гонку двух вкладок.
    op.bulk_insert(manual, [{"kind": "domain", "body": ""}, {"kind": "ip", "body": ""}])


def downgrade() -> None:
    op.drop_table("domain_builds")
    op.drop_table("manual_lists")
    op.drop_index("ix_domain_sources_kind_enabled", table_name="domain_sources")
    op.drop_table("domain_sources")
