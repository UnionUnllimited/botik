"""Видов списка два, оба через туннель

Решение заказчика от 31 августа, доведённое до конца: «мимо туннеля» больше
нет ни как списка, ни как вида. Остаются `proxy_domain` и `proxy_ip`.

Что делает миграция:

  * удаляет источники, свой список и его историю с видами `direct_domain`
    и `direct_ip` — в коде таких видов больше нет, и строка с неизвестным
    видом на странице списков выглядела бы как поломка;
  * заводит пустую строку своего списка для `proxy_ip`: `kind` уникален,
    и создавать её по требованию значило бы ловить гонку двух вкладок;
  * добавляет источник подсетей из нашего репозитория.

Адреса `direct-domains.lst` и `direct-ip.lst` перестают отвечать вместе
с видами. Ломать было нечего: парк на этот момент тянул списки со старого
сервера `vm171085` и на наши адреса не смотрел.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GONE = ("direct_domain", "direct_ip")
_PROXY_IP_URL = (
    "https://raw.githubusercontent.com/UnionUnllimited/domensrouter/main/build/proxy-ip.lst"
)


def upgrade() -> None:
    gone = ", ".join(f"'{kind}'" for kind in _GONE)
    op.execute(f"DELETE FROM domain_sources WHERE kind IN ({gone})")
    op.execute(f"DELETE FROM manual_list_revisions WHERE kind IN ({gone})")
    op.execute(f"DELETE FROM manual_lists WHERE kind IN ({gone})")

    op.execute(
        "INSERT INTO manual_lists (kind, body, updated_by, updated_at) "
        f"VALUES ('proxy_ip', '', '', now()) ON CONFLICT (kind) DO NOTHING"
    )

    op.execute(
        "INSERT INTO domain_sources (url, title, kind, is_enabled, sort_order) "
        f"VALUES ('{_PROXY_IP_URL}', 'Наши подсети', 'proxy_ip', true, 50) "
        f"ON CONFLICT (url) DO UPDATE SET kind = 'proxy_ip', is_enabled = true"
    )


def downgrade() -> None:
    # Удалённое не воскрешаем: адреса тех источников лежат в 0018 и 0019,
    # и восстановить их правильнее прогоном оттуда, чем копией здесь.
    op.execute(f"DELETE FROM domain_sources WHERE url = '{_PROXY_IP_URL}'")
    op.execute(f"DELETE FROM manual_lists WHERE kind = 'proxy_ip'")
