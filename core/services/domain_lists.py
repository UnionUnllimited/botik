"""Сборка списков доменов и подсетей для роутеров.

Переехало со скрипта `sync_local.sh` на сервере frps. Правила чистки те же —
списки у поставщиков лежат в разном виде, и разойтись с прежним поведением
значит поменять то, что попадёт в туннель, у всего парка сразу.

Собранное отдаётся с нашего домена и, если настроено, кладётся копией
в объектное хранилище: списки тянет весь парк разом, и выкат нашего сервера
не должен оставлять роутеры без обновления.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.dates import utcnow
from core.models import DomainBuild, DomainSource, ManualList

log = structlog.get_logger(__name__)

FETCH_TIMEOUT_SEC = 20
"""Столько же, сколько давал `curl -m 20` в скрипте."""

MAX_PARALLEL = 6
"""Источников два десятка, и качать их по очереди — минута на сборку.
Больше шести одновременно к одному GitHub — верный способ получить 429."""

_COMMENT = re.compile(r"\s*#.*$")
_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
_DOMAIN_OK = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")
_IP_OK = re.compile(r"^(\d{1,3}\.){3}\d{1,3}(/\d{1,2})?$")


def clean_domains(raw: str) -> list[str]:
    """Приводит кусок списка к голым доменам.

    Порядок шагов повторяет скрипт: снять комментарий, отбросить пустое,
    привести к нижнему регистру, снять схему и путь, выбросить строки
    с пробелами и всё, что не похоже на домен.
    """
    out: list[str] = []
    for line in raw.replace("\r", "").splitlines():
        value = _COMMENT.sub("", line).strip().lower()
        if not value:
            continue
        value = _SCHEME.sub("", value).split("/", 1)[0]
        if not value or " " in value or "\t" in value:
            continue
        if _DOMAIN_OK.match(value):
            out.append(value)
    return out


def clean_ips(raw: str) -> list[str]:
    """То же для подсетей: годится только IPv4, с маской или без."""
    out: list[str] = []
    for line in raw.replace("\r", "").splitlines():
        value = _COMMENT.sub("", line).strip().lower()
        if not value or " " in value or "\t" in value:
            continue
        if _IP_OK.match(value):
            out.append(value)
    return out


CLEANERS = {"domain": clean_domains, "ip": clean_ips}


async def fetch(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Скачивает источник. Возвращает (тело, ошибка) — одно из двух пустое.

    Недоступный источник не роняет сборку: у поставщика периодически
    отваливаются отдельные файлы, а список без одной категории лучше,
    чем отсутствие обновления вовсе. Ошибка запоминается у источника,
    чтобы её было видно на странице, а не только в журнале.
    """
    try:
        response = await client.get(url, timeout=FETCH_TIMEOUT_SEC, follow_redirects=True)
    except httpx.HTTPError as exc:
        return "", f"{type(exc).__name__}: {exc}"[:255]
    if response.status_code != 200:
        return "", f"HTTP {response.status_code}"
    return response.text, ""


def merge(parts: list[list[str]], manual: str, kind: str) -> list[str]:
    """Склеивает куски со своим списком: уникальные значения по алфавиту.

    Свой список проходит ту же чистку, что и скачанное. Оператор вставляет
    в поле что придётся — с `https://`, с комментарием, с пустой строкой,
    — и молча пропустить такую строку хуже, чем причесать её тем же способом.
    """
    cleaner = CLEANERS[kind]
    values: set[str] = set()
    for part in parts:
        values.update(part)
    values.update(cleaner(manual))
    return sorted(values)


# ── Сборка ────────────────────────────────────────────────────────────────────

FILE_NAMES = {"domain": "domains.lst", "ip": "ip.lst"}
"""Имена файлов, по которым роутер их забирает. Прежние `domenchik.lst`
и `ipchik.lst` не переносим: адрес в прошивке всё равно меняется, а имя,
по которому не догадаться о содержимом, стоило заменить сразу."""


def lists_dir() -> Path:
    """Каталог с собранными списками — в том же томе, что картинки товаров."""
    return Path(settings.app.media_dir) / "lists"


def read_list(kind: str) -> str:
    """Отдаёт собранный список. Пусто — значит сборки ещё не было."""
    path = lists_dir() / FILE_NAMES[kind]
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_list(kind: str, values: list[str]) -> None:
    """Пишет список атомарно: через временный файл и переименование.

    Роутеры тянут файл в произвольный момент, и запись «на месте» отдала бы
    кому-то половину списка — то есть половину доступа.
    """
    directory = lists_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / FILE_NAMES[kind]
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text("\n".join(values) + ("\n" if values else ""), encoding="utf-8")
    tmp.replace(target)


async def build(session: AsyncSession) -> DomainBuild:
    """Пересобирает оба списка: качает включённые источники, чистит, склеивает.

    Одна запись `DomainBuild` на сборку — по ней видно, когда собирали, сколько
    вышло и сколько источников не ответило. Недоступный источник не роняет
    сборку целиком: список без одной категории лучше, чем отсутствие
    обновления вовсе.
    """
    record = DomainBuild()
    session.add(record)
    await session.flush()

    sources = (
        await session.scalars(
            select(DomainSource).where(DomainSource.is_enabled.is_(True)).order_by(DomainSource.sort_order)
        )
    ).all()
    manual = {row.kind: row.body for row in (await session.scalars(select(ManualList))).all()}

    parts: dict[str, list[list[str]]] = {"domain": [], "ip": []}
    failed = 0
    gate = asyncio.Semaphore(MAX_PARALLEL)

    async with httpx.AsyncClient() as client:

        async def one(source: DomainSource) -> None:
            nonlocal failed
            async with gate:
                body, error = await fetch(client, source.url)
            if error:
                failed += 1
                source.last_error = error
                source.last_lines = 0
                log.warning("domain_lists.source_failed", url=source.url, error=error)
                return
            values = CLEANERS[source.kind](body)
            parts[source.kind].append(values)
            source.last_error = ""
            source.last_lines = len(values)
            source.last_ok_at = utcnow()

        await asyncio.gather(*(one(source) for source in sources))

    counts: dict[str, int] = {}
    for kind in ("domain", "ip"):
        values = merge(parts[kind], manual.get(kind, ""), kind)
        write_list(kind, values)
        counts[kind] = len(values)

    record.domains = counts["domain"]
    record.ips = counts["ip"]
    record.failed_sources = failed
    record.finished_at = utcnow()
    log.info("domain_lists.built", domains=record.domains, ips=record.ips, failed=failed)
    return record
