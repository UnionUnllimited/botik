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
from core.services import settings_service

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


NOT_MODIFIED = "not_modified"
"""Признак, что источник не менялся с прошлого круга."""


async def fetch(
    client: httpx.AsyncClient, url: str, *, etag: str = ""
) -> tuple[str, str, str]:
    """Скачивает источник. Возвращает (тело, метку версии, ошибка).

    С непустым `etag` уходит условный запрос: неизменившийся файл отвечает
    `304` без тела, и такой круг почти ничего не стоит ни нам, ни отдающей
    стороне. Ради этого опрос и может идти часто — безусловные запросы
    к 26 файлам каждые несколько минут упёрлись бы в 429, и списки начали бы
    собираться с дырами.

    Недоступный источник не роняет сборку: у поставщика периодически
    отваливаются отдельные файлы, а список без одной категории лучше,
    чем отсутствие обновления вовсе.
    """
    headers = {"If-None-Match": etag} if etag else {}
    try:
        response = await client.get(
            url, timeout=FETCH_TIMEOUT_SEC, follow_redirects=True, headers=headers
        )
    except httpx.HTTPError as exc:
        return "", etag, f"{type(exc).__name__}: {exc}"[:255]
    if response.status_code == 304:
        return "", etag, NOT_MODIFIED
    if response.status_code != 200:
        return "", etag, f"HTTP {response.status_code}"
    return response.text, (response.headers.get("ETag") or "")[:200], ""


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


async def build(session: AsyncSession, *, force: bool = False) -> DomainBuild:
    """Пересобирает списки, если хоть один источник изменился.

    Круг идёт часто, а меняются источники редко, поэтому первым проходом
    спрашиваем условно: с `If-None-Match` неизменившийся файл отвечает `304`
    без тела. Если так ответили все — пересобирать нечего, круг закрывается
    записью `skipped`, и роутеры получают тот же файл, что и раньше.

    Если изменился хоть один, остальные нужно докачать целиком: их содержимое
    мы не храним, а склеить список из половины кусков нельзя. Это редкий
    случай, и он стоит ровно одного лишнего круга запросов.

    `force=True` — кнопка «Собрать сейчас»: оператор нажимает её как раз тогда,
    когда хочет увидеть результат своей правки, а её `ETag` источников не знает.
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

    bodies: dict[int, str] = {}
    failed = 0
    changed = force
    gate = asyncio.Semaphore(MAX_PARALLEL)

    async with httpx.AsyncClient() as client:

        async def probe(source: DomainSource) -> None:
            nonlocal failed, changed
            async with gate:
                body, etag, error = await fetch(
                    client, source.url, etag="" if force else source.etag
                )
            if error == NOT_MODIFIED:
                source.last_error = ""
                source.last_ok_at = utcnow()
                return
            if error:
                failed += 1
                source.last_error = error
                log.warning("domain_lists.source_failed", url=source.url, error=error)
                return
            changed = True
            bodies[source.id] = body
            source.etag = etag

        await asyncio.gather(*(probe(source) for source in sources))

        if not changed:
            record.skipped = True
            record.finished_at = utcnow()
            log.info("domain_lists.unchanged", sources=len(sources))
            return record

        # Докачиваем то, что ответило «не менялось»: содержимое мы не храним,
        # а склеить список из половины кусков нельзя.
        async def refetch(source: DomainSource) -> None:
            nonlocal failed
            async with gate:
                body, etag, error = await fetch(client, source.url)
            if error:
                failed += 1
                source.last_error = error
                return
            bodies[source.id] = body
            source.etag = etag

        await asyncio.gather(
            *(refetch(s) for s in sources if s.id not in bodies and not s.last_error)
        )

    parts: dict[str, list[list[str]]] = {"domain": [], "ip": []}
    for source in sources:
        body = bodies.get(source.id)
        if body is None:
            continue
        values = CLEANERS[source.kind](body)
        parts[source.kind].append(values)
        source.last_lines = len(values)
        source.last_error = ""
        source.last_ok_at = utcnow()

    counts: dict[str, int] = {}
    built: dict[str, list[str]] = {}
    for kind in ("domain", "ip"):
        values = merge(parts[kind], manual.get(kind, ""), kind)
        write_list(kind, values)
        counts[kind] = len(values)
        built[kind] = values

    record.uploaded = await upload(built, await config(session))
    record.domains = counts["domain"]
    record.ips = counts["ip"]
    record.failed_sources = failed
    record.finished_at = utcnow()
    log.info("domain_lists.built", domains=record.domains, ips=record.ips, failed=failed)
    return record


# ── Копия в объектном хранилище ───────────────────────────────────────────────


def _s3_client(conf: dict[str, str]):
    """Клиент к S3-совместимому хранилищу. Один на оба провайдера.

    Yandex и VK различаются только адресом, поэтому выбор — это `endpoint_url`
    из настроек, а не два разных клиента. Импорт внутри: `boto3` нужен только
    тут, а тянуть его при каждом старте API незачем.
    """
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=conf["lists_s3_endpoint"],
        region_name=conf.get("lists_s3_region") or "ru-central1",
        aws_access_key_id=conf["lists_s3_access_key"],
        aws_secret_access_key=conf["lists_s3_secret_key"],
    )


async def upload(values_by_kind: dict[str, list[str]], conf: dict[str, str] | None = None) -> bool:
    """Кладёт копию списков в хранилище. Возвращает, получилось ли.

    Выкладка мягкая: список уже собран и отдаётся с нашего домена, и падать
    из-за недоступного хранилища нельзя — пропущенный круг повторит следующий.
    Тот же размен, что с синхронизацией срока в панели.

    Загрузка блокирующая (`boto3` синхронный), поэтому уходит в поток: держать
    ею event loop на паре мегабайт незачем.
    """
    conf = conf or {}
    if not (conf.get("lists_s3_bucket") and conf.get("lists_s3_endpoint")
            and conf.get("lists_s3_access_key") and conf.get("lists_s3_secret_key")):
        return False

    def _put() -> None:
        client = _s3_client(conf)
        for kind, values in values_by_kind.items():
            body = ("\n".join(values) + ("\n" if values else "")).encode()
            client.put_object(
                Bucket=conf["lists_s3_bucket"],
                Key=f"{(conf.get('lists_s3_prefix') or 'lists/').lstrip('/')}{FILE_NAMES[kind]}",
                Body=body,
                ContentType="text/plain; charset=utf-8",
            )

    try:
        await asyncio.to_thread(_put)
    except Exception as exc:  # noqa: BLE001 — причин у чужого хранилища много, все одинаково нефатальны
        log.warning("domain_lists.upload_failed", error=str(exc))
        return False
    log.info("domain_lists.uploaded", bucket=conf["lists_s3_bucket"])
    return True

# ── Настройки, правимые на странице ───────────────────────────────────────────

SETTING_KEYS = (
    "lists_poll_interval_min",
    "lists_s3_bucket",
    "lists_s3_endpoint",
    "lists_s3_region",
    "lists_s3_prefix",
    "lists_s3_access_key",
    "lists_s3_secret_key",
)
"""Живут в базе, а не только в окружении: оператор меняет их из панели,
и требовать ради смены интервала правки `.env` с перезапуском — перебор.
Значение из `.env` остаётся значением по умолчанию, пока в базе пусто."""

SECRET_KEYS = frozenset({"lists_s3_access_key", "lists_s3_secret_key"})
"""Наружу отдаются не значением, а признаком «задано»: страница открыта
оператору, а ключ от хранилища ему смотреть незачем."""


async def config(session: AsyncSession) -> dict[str, str]:
    """Настройки списков: из базы, с падением на окружение."""
    env = settings.lists
    fallback = {
        "lists_poll_interval_min": str(env.poll_interval_min),
        "lists_s3_bucket": env.s3_bucket,
        "lists_s3_endpoint": env.s3_endpoint,
        "lists_s3_region": env.s3_region,
        "lists_s3_prefix": env.s3_prefix,
        "lists_s3_access_key": env.s3_access_key.get_secret_value(),
        "lists_s3_secret_key": env.s3_secret_key.get_secret_value(),
    }
    out: dict[str, str] = {}
    for key in SETTING_KEYS:
        stored = await settings_service.get_setting(session, key)
        out[key] = str(stored) if stored not in (None, "") else fallback[key]
    return out


async def save_config(session: AsyncSession, values: dict[str, str]) -> None:
    """Сохраняет настройки. Пустой секрет не затирает прежний.

    Страница не показывает ключи, поэтому пустое поле означает «не менял»,
    а не «сотри»: иначе любое сохранение интервала обнуляло бы доступ
    к хранилищу.
    """
    for key in SETTING_KEYS:
        if key not in values:
            continue
        value = str(values[key]).strip()
        if key in SECRET_KEYS and not value:
            continue
        if key == "lists_poll_interval_min":
            try:
                value = str(max(1, min(int(value or 10), 1440)))
            except ValueError:
                continue
        await settings_service.set_setting(session, key, value)

