"""Шесть новых категорий доменов мимо туннеля

В `parts/domains/` репозитория списков появились файлы, которых не было
в посеве 0018. Пока источник не заведён, его строки не попадают в сборку
вовсе — на странице списков это видно как «0 из N».

Что добавляется и зачем каждая:

  * `05-own-infra` — наша инфраструктура. Мимо туннеля она нужна затем же,
    зачем врачу не лечиться у себя: роутер ходит к нам за подпиской,
    прошивкой и списками, и заворачивать этот путь в туннель, который сам
    настраивается из ответа, — верный способ получить круг.
    Не путать с `00-own-infra` в `proxy`: там то, что должно идти **через**
    туннель, и это другой набор имён.
  * `81-device-ota` — обновления телефонов, Apple, Windows Update. Через
    зарубежный выход они либо не качаются, либо тянут гигабайты по чужому
    каналу.
  * `82-dev-registries` — npm, crates, Maven, Apache.
  * `83-asia` — Taobao, Alipay, Bilibili, Baidu: из-за рубежа отвечают хуже,
    чем напрямую, а часть просто закрыта не для Азии.
  * `84-hardware-vendors` — AMD, ASUS, Lenovo, HP.
  * `85-desktop-software` — браузеры, Blender, OBS, FFmpeg.

Порядок сортировки подобран так, чтобы категории встали на свои места
между соседями по номеру, а не в хвост за списками сетей: `05` идёт
после кириллических зон, `81`–`85` — сразу за `80-oss-updates`.
Перенумеровывать существующие ради этого не нужно.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from core.models import ListKind

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RAW = "https://raw.githubusercontent.com/UnionUnllimited/domensrouter/main/parts"

_NEW: tuple[tuple[str, str, int], ...] = (
    ("05-own-infra", "Наша инфраструктура", 25),
    ("81-device-ota", "Обновления устройств", 211),
    ("82-dev-registries", "Репозитории разработчика", 212),
    ("83-asia", "Азиатские сервисы", 213),
    ("84-hardware-vendors", "Производители железа", 214),
    ("85-desktop-software", "Настольный софт", 215),
)


def _url(stem: str) -> str:
    return f"{_RAW}/domains/{stem}.lst"


def upgrade() -> None:
    # `ON CONFLICT` по адресу: миграцию могут прогнать на базе, где источник
    # уже завели руками на странице списков. Тогда его настройки — включён он
    # или выключен, как назван — остаются оператора, а не наши.
    for stem, title, order in _NEW:
        op.execute(
            "INSERT INTO domain_sources (url, title, kind, is_enabled, sort_order) "
            f"VALUES ('{_url(stem)}', '{title}', '{ListKind.DIRECT_DOMAIN}', true, {order}) "
            "ON CONFLICT (url) DO NOTHING"
        )


def downgrade() -> None:
    for stem, _title, _order in _NEW:
        op.execute(f"DELETE FROM domain_sources WHERE url = '{_url(stem)}'")
