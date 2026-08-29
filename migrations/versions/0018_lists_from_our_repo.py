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

from core.models import ListKind

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RAW = "https://raw.githubusercontent.com/UnionUnllimited/domensrouter/main/parts"

_FOLDERS = {
    ListKind.DIRECT_DOMAIN: "domains",
    ListKind.DIRECT_IP: "ip",
    ListKind.PROXY_DOMAIN: "proxy",
}

_SEED: tuple[tuple[str, str, str], ...] = (
    (ListKind.DIRECT_DOMAIN, "00-tld-zones", "Зоны верхнего уровня"),
    (ListKind.DIRECT_DOMAIN, "01-idn-zones", "Кириллические зоны"),
    (ListKind.DIRECT_DOMAIN, "10-yandex", "Яндекс"),
    (ListKind.DIRECT_DOMAIN, "11-vk-mail", "VK и Mail.ru"),
    (ListKind.DIRECT_DOMAIN, "12-sber", "Сбер"),
    (ListKind.DIRECT_DOMAIN, "20-banks-fintech", "Банки и финтех"),
    (ListKind.DIRECT_DOMAIN, "21-marketplace-retail", "Маркетплейсы и ритейл"),
    (ListKind.DIRECT_DOMAIN, "22-travel-delivery-taxi", "Поездки, такси, доставка"),
    (ListKind.DIRECT_DOMAIN, "23-gov-health-edu", "Госуслуги, медицина, образование"),
    (ListKind.DIRECT_DOMAIN, "24-maps", "Карты"),
    (ListKind.DIRECT_DOMAIN, "26-corporate", "Корпорации"),
    (ListKind.DIRECT_DOMAIN, "30-telecom", "Операторы связи"),
    (ListKind.DIRECT_DOMAIN, "31-hosting-cloud-cdn", "Хостинги и облака"),
    (ListKind.DIRECT_DOMAIN, "32-security-av", "Антивирусы и ИБ"),
    (ListKind.DIRECT_DOMAIN, "40-state-media", "Госмедиа"),
    (ListKind.DIRECT_DOMAIN, "41-media-streaming", "Кино, музыка, ТВ"),
    (ListKind.DIRECT_DOMAIN, "50-games", "Игры"),
    (ListKind.DIRECT_DOMAIN, "51-steam-valve", "Steam и Valve"),
    (ListKind.DIRECT_DOMAIN, "60-saas-martech", "Сервисы для бизнеса"),
    (ListKind.DIRECT_DOMAIN, "70-it-content", "IT-медиа и RuStore"),
    (ListKind.DIRECT_DOMAIN, "80-oss-updates", "Свободный софт и обновления"),
    (ListKind.DIRECT_DOMAIN, "90-ip-speed-check", "Проверка IP и скорости"),
    (ListKind.DIRECT_DOMAIN, "99-other", "Прочее"),
    (ListKind.DIRECT_IP, "00-steam-valve", "Сети Steam и Valve"),
    (ListKind.DIRECT_IP, "10-ru-operators", "Сети операторов связи"),
    (ListKind.DIRECT_IP, "20-ru-hosting-cloud", "Сети хостингов и облаков"),
    (ListKind.DIRECT_IP, "30-ru-bigtech", "Сети крупных сервисов"),
    (ListKind.DIRECT_IP, "40-cdn-antiddos", "Сети CDN и антиDDoS"),
    (ListKind.DIRECT_IP, "50-ru-unrouted", "Немаршрутизируемые сети"),
    (ListKind.DIRECT_IP, "90-ru-other", "Прочие сети"),
    (ListKind.PROXY_DOMAIN, "00-own-infra", "Наша инфраструктура"),
    (ListKind.PROXY_DOMAIN, "10-blocked-ru-hosted", "Заблокированное на российских хостингах"),
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
    op.execute(f"UPDATE manual_lists SET kind = '{ListKind.DIRECT_DOMAIN}' WHERE kind = 'domain'")
    op.execute(f"UPDATE manual_lists SET kind = '{ListKind.DIRECT_IP}' WHERE kind = 'ip'")
    op.execute(
        "INSERT INTO manual_lists (kind, body, updated_by, updated_at) "
        f"VALUES ('{ListKind.PROXY_DOMAIN}', '', '', now()) ON CONFLICT (kind) DO NOTHING"
    )
    op.execute(
        f"UPDATE manual_list_revisions SET kind = '{ListKind.DIRECT_DOMAIN}' WHERE kind = 'domain'"
    )
    op.execute(f"UPDATE manual_list_revisions SET kind = '{ListKind.DIRECT_IP}' WHERE kind = 'ip'")


def downgrade() -> None:
    op.execute(f"DELETE FROM manual_lists WHERE kind = '{ListKind.PROXY_DOMAIN}'")
    op.execute(f"UPDATE manual_lists SET kind = 'domain' WHERE kind = '{ListKind.DIRECT_DOMAIN}'")
    op.execute(f"UPDATE manual_lists SET kind = 'ip' WHERE kind = '{ListKind.DIRECT_IP}'")
    op.execute(
        f"UPDATE manual_list_revisions SET kind = 'domain' WHERE kind = '{ListKind.DIRECT_DOMAIN}'"
    )
    op.execute(f"UPDATE manual_list_revisions SET kind = 'ip' WHERE kind = '{ListKind.DIRECT_IP}'")
    op.execute("DELETE FROM domain_sources")
