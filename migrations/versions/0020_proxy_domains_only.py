"""Все домены идут через туннель, прежние источники выключены

Решение заказчика от 31 августа. Прежняя схема была обратной: через туннель
шёл короткий список, всё российское — мимо. Теперь домены собираются в один
список и целиком уходят в `proxy_domain`, то есть **через** туннель.

Источники берутся как есть, без вычитания:

  * `haritos90/allow-domains` — Russia/russia-all
  * `itdoginfo/allow-domains` — Russia/inside-raw
  * `1andrevich/Re-filter-lists` — community
  * `UnionUnllimited/domensrouter` — sources/only-ours-proxy

**Прежние источники выключены, а не удалены.** Их 38, они разложены по
категориям, и вернуть набор галочками проще, чем восстанавливать адреса
по памяти. Модель источников на это и рассчитана: выключенный остаётся
в таблице.

**Чего здесь нет: подсетей через туннель.** У PassWall три настройки —
`chnlist_url` (домены мимо), `chnroute_url` (сети мимо), `gfwlist_url`
(домены через). Списка «сети через туннель» у него нет вовсе, и
`build/proxy-ip.lst` класть некуда: в `direct_ip` он отправил бы эти сети
ровно в обратную сторону. Заведём, когда решим, чем его читать.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW: tuple[tuple[str, str, int], ...] = (
    (
        "https://raw.githubusercontent.com/haritos90/allow-domains/main/Russia/russia-all.lst",
        "Россия целиком (haritos90)",
        10,
    ),
    (
        "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Russia/inside-raw.lst",
        "Россия, исходный (itdoginfo)",
        20,
    ),
    (
        "https://raw.githubusercontent.com/1andrevich/Re-filter-lists/main/community.lst",
        "Re-filter, community",
        30,
    ),
    (
        "https://raw.githubusercontent.com/UnionUnllimited/domensrouter/main/sources/only-ours-proxy.lst",
        "Наши домены",
        40,
    ),
)


def upgrade() -> None:
    # Выключаем, а не удаляем: набор из 38 категорий собирался руками,
    # и вернуть его галочками проще, чем адресами по памяти.
    op.execute("UPDATE domain_sources SET is_enabled = false")

    for url, title, order in _NEW:
        op.execute(
            "INSERT INTO domain_sources (url, title, kind, is_enabled, sort_order) "
            f"VALUES ('{url}', '{title}', 'proxy_domain', true, {order}) "
            "ON CONFLICT (url) DO UPDATE SET "
            f"kind = 'proxy_domain', is_enabled = true, sort_order = {order}"
        )


def downgrade() -> None:
    for url, _title, _order in _NEW:
        op.execute(f"DELETE FROM domain_sources WHERE url = '{url}'")
    op.execute("UPDATE domain_sources SET is_enabled = true")
