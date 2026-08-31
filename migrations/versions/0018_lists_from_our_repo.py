"""Списки собираются из нашего репозитория и делятся на три

PassWall на роутере читает три файла, и назначение у них разное:

  * `direct_domain` → `chnlist_url` при `chn_list 'direct'` — домены мимо
    туннеля: российские сайты, банки, госуслуги. Через туннель они либо
    не открываются, либо отвечают как из-за рубежа.
  * `direct_ip` → `chnroute_url` — сети мимо туннеля. Ими же держатся
    напрямую все `*.ru`: голые зоны из списка доменов PassWall отбрасывает.
  * `proxy_domain` → `gfwlist_url` при `gfwlist_update '1'` — домены через
    туннель. Короткий список: наша инфраструктура и заблокированное
    у российских хостеров.

**Смысл списков стал обратным прежнему.** До этой ревизии собирались
`allow-domains` от itdoginfo — домены, которые надо вести *через* туннель.
Теперь наоборот: через туннель идёт короткий список, всё российское — мимо.
Поэтому прежние источники не переименовываются, а заменяются целиком:
оставить их значило бы пустить банки в туннель.

Источник один — `github.com/UnionUnllimited/domensrouter`, папка `parts/`,
где те же записи разложены по категориям. Каждая категория заведена
отдельным источником: их включают и выключают галочками, и «выключить
госмедиа» должно быть одним движением, а не правкой файла.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RAW = "https://raw.githubusercontent.com/UnionUnllimited/domensrouter/main/parts"

_FOLDERS = {
    "direct_domain": "domains",
    "direct_ip": "ip",
    "proxy_domain": "proxy",
}

_SEED: tuple[tuple[str, str, str], ...] = (
    ("direct_domain", "00-tld-zones", "Зоны верхнего уровня"),
    ("direct_domain", "01-idn-zones", "Кириллические зоны"),
    ("direct_domain", "10-yandex", "Яндекс"),
    ("direct_domain", "11-vk-mail", "VK и Mail.ru"),
    ("direct_domain", "12-sber", "Сбер"),
    ("direct_domain", "20-banks-fintech", "Банки и финтех"),
    ("direct_domain", "21-marketplace-retail", "Маркетплейсы и ритейл"),
    ("direct_domain", "22-travel-delivery-taxi", "Поездки, такси, доставка"),
    ("direct_domain", "23-gov-health-edu", "Госуслуги, медицина, образование"),
    ("direct_domain", "24-maps", "Карты"),
    ("direct_domain", "26-corporate", "Корпорации"),
    ("direct_domain", "30-telecom", "Операторы связи"),
    ("direct_domain", "31-hosting-cloud-cdn", "Хостинги и облака"),
    ("direct_domain", "32-security-av", "Антивирусы и ИБ"),
    ("direct_domain", "40-state-media", "Госмедиа"),
    ("direct_domain", "41-media-streaming", "Кино, музыка, ТВ"),
    ("direct_domain", "50-games", "Игры"),
    ("direct_domain", "51-steam-valve", "Steam и Valve"),
    ("direct_domain", "60-saas-martech", "Сервисы для бизнеса"),
    ("direct_domain", "70-it-content", "IT-медиа и RuStore"),
    ("direct_domain", "80-oss-updates", "Свободный софт и обновления"),
    ("direct_domain", "90-ip-speed-check", "Проверка IP и скорости"),
    ("direct_domain", "99-other", "Прочее"),
    ("direct_ip", "00-steam-valve", "Сети Steam и Valve"),
    ("direct_ip", "10-ru-operators", "Сети операторов связи"),
    ("direct_ip", "20-ru-hosting-cloud", "Сети хостингов и облаков"),
    ("direct_ip", "30-ru-bigtech", "Сети крупных сервисов"),
    ("direct_ip", "40-cdn-antiddos", "Сети CDN и антиDDoS"),
    ("direct_ip", "50-ru-unrouted", "Немаршрутизируемые сети"),
    ("direct_ip", "90-ru-other", "Прочие сети"),
    ("proxy_domain", "00-own-infra", "Наша инфраструктура"),
    ("proxy_domain", "10-blocked-ru-hosted", "Заблокированное на российских хостингах"),
)


def upgrade() -> None:
    # Старые источники убираются целиком, а не правятся: они вели
    # в противоположную сторону — «через туннель» вместо «мимо».
    op.execute("DELETE FROM domain_sources")

    sources = sa.table(
        "domain_sources",
        sa.column("url", sa.String),
        sa.column("title", sa.String),
        sa.column("kind", sa.String),
        sa.column("is_enabled", sa.Boolean),
        sa.column("sort_order", sa.Integer),
    )
    op.bulk_insert(
        sources,
        [
            {
                "url": f"{_RAW}/{_FOLDERS[kind]}/{stem}.lst",
                "title": title,
                "kind": kind,
                "is_enabled": True,
                "sort_order": (index + 1) * 10,
            }
            for index, (kind, stem, title) in enumerate(_SEED)
        ],
    )

    # Свой список оператора: прежние два переезжают на новые имена видов,
    # третий заводится пустым. Пустая строка обязана существовать заранее —
    # `kind` уникален, и создавать её по требованию значило бы ловить гонку
    # двух вкладок.
    op.execute(f"UPDATE manual_lists SET kind = 'direct_domain' WHERE kind = 'domain'")
    op.execute(f"UPDATE manual_lists SET kind = 'direct_ip' WHERE kind = 'ip'")
    op.execute(
        "INSERT INTO manual_lists (kind, body, updated_by, updated_at) "
        f"VALUES ('proxy_domain', '', '', now()) ON CONFLICT (kind) DO NOTHING"
    )
    op.execute(
        f"UPDATE manual_list_revisions SET kind = 'direct_domain' WHERE kind = 'domain'"
    )
    op.execute(f"UPDATE manual_list_revisions SET kind = 'direct_ip' WHERE kind = 'ip'")


def downgrade() -> None:
    op.execute(f"DELETE FROM manual_lists WHERE kind = 'proxy_domain'")
    op.execute(f"UPDATE manual_lists SET kind = 'domain' WHERE kind = 'direct_domain'")
    op.execute(f"UPDATE manual_lists SET kind = 'ip' WHERE kind = 'direct_ip'")
    op.execute(
        f"UPDATE manual_list_revisions SET kind = 'domain' WHERE kind = 'direct_domain'"
    )
    op.execute(f"UPDATE manual_list_revisions SET kind = 'ip' WHERE kind = 'direct_ip'")
    op.execute("DELETE FROM domain_sources")
