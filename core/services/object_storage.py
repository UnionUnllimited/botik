"""Объектное хранилище: один клиент на всё, что мы туда кладём.

Кладём две вещи, и обе тянет парк железа разом: списки доменов и образы
прошивки. Разница между ними в цене ошибки. Список весит пару мегабайт, и
не доехавшая копия стоит одного круга ожидания. Образ весит полсотни, его
качают триста устройств сразу, и промахнуться адресом значит остановить
обновление у всего парка молча — роутер о себе не сообщает ничего.

Поэтому здесь только операции, а решение «что считать успехом» принимает
зовущий: списки выкладываются мягко, прошивка перед публикацией сверяется.

Провайдеры S3-совместимы и различаются одним адресом:
  Yandex — https://storage.yandexcloud.net
  VK     — https://hb.vkcs.cloud
Клиент один, выбор — значение `endpoint_url` в настройках.

Имена настроек начинаются на `lists_` с тех пор, когда в хранилище уезжали
только списки. Переименовать их значило бы заставить оператора вводить ключи
заново — а это ровно тот случай, когда честное имя стоит дороже, чем стоит.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.services import settings_service

log = structlog.get_logger("services.object_storage")

KEYS = (
    "lists_s3_bucket",
    "lists_s3_endpoint",
    "lists_s3_region",
    "lists_s3_prefix",
    "lists_s3_public_url",
    "lists_s3_access_key",
    "lists_s3_secret_key",
)
"""Настройки хранилища. Живут в базе, а не только в окружении: оператор
меняет их из панели, и требовать ради смены бакета правки `.env`
с перезапуском — перебор."""

SECRET_KEYS = frozenset({"lists_s3_access_key", "lists_s3_secret_key"})
"""Наружу отдаются не значением, а признаком «задано»: страница открыта
оператору, а ключ от хранилища ему смотреть незачем."""


@dataclass(frozen=True, slots=True)
class Storage:
    """Куда и чем класть. Пустой bucket выключает выкладку целиком."""

    bucket: str = ""
    endpoint: str = ""
    region: str = "ru-central1"
    access_key: str = ""
    secret_key: str = ""
    public_url: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.bucket and self.endpoint and self.access_key and self.secret_key)

    def url_of(self, key: str) -> str:
        """Адрес, по которому файл заберёт роутер.

        По умолчанию собирается из адреса хранилища и имени бакета — так
        Yandex отдаёт публичные объекты. `public_url` перебивает его: перед
        бакетом обычно стоит CDN, и полсотни мегабайт на триста устройств
        разумнее отдавать через него, а не из хранилища напрямую.
        """
        base = self.public_url.rstrip("/") or f"{self.endpoint.rstrip('/')}/{self.bucket}"
        return f"{base}/{key.lstrip('/')}"


def from_mapping(conf: dict[str, str] | None) -> Storage:
    conf = conf or {}
    return Storage(
        bucket=(conf.get("lists_s3_bucket") or "").strip(),
        endpoint=(conf.get("lists_s3_endpoint") or "").strip(),
        region=(conf.get("lists_s3_region") or "").strip() or "ru-central1",
        access_key=(conf.get("lists_s3_access_key") or "").strip(),
        secret_key=(conf.get("lists_s3_secret_key") or "").strip(),
        public_url=(conf.get("lists_s3_public_url") or "").strip(),
    )


def defaults() -> dict[str, str]:
    """Значения из окружения. Ими закрываются ключи, по которым в базе пусто."""
    env = settings.lists
    return {
        "lists_s3_bucket": env.s3_bucket,
        "lists_s3_endpoint": env.s3_endpoint,
        "lists_s3_region": env.s3_region,
        "lists_s3_prefix": env.s3_prefix,
        "lists_s3_public_url": env.s3_public_url,
        "lists_s3_access_key": env.s3_access_key.get_secret_value(),
        "lists_s3_secret_key": env.s3_secret_key.get_secret_value(),
    }


async def config(session: AsyncSession) -> dict[str, str]:
    """Настройки хранилища: из базы, с падением на окружение."""
    fallback = defaults()
    out: dict[str, str] = {}
    for key in KEYS:
        stored = await settings_service.get_setting(session, key)
        out[key] = str(stored) if stored not in (None, "") else fallback[key]
    return out


async def current(session: AsyncSession) -> Storage:
    return from_mapping(await config(session))


# ── Операции ─────────────────────────────────────────────────────────────────
#
# `boto3` синхронный, поэтому каждая уходит в поток: держать event loop
# закачкой образа в полсотни мегабайт незачем. Импорт внутри — библиотека
# нужна только здесь, а тянуть её при каждом старте API не за чем.


def _client(storage: Storage):
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=storage.endpoint,
        region_name=storage.region,
        aws_access_key_id=storage.access_key,
        aws_secret_access_key=storage.secret_key,
    )


async def put_bytes(storage: Storage, key: str, body: bytes, content_type: str) -> str:
    """Кладёт небольшой объект. Возвращает причину неудачи или пустую строку."""
    if not storage.configured:
        return "хранилище не настроено"

    def _put() -> None:
        _client(storage).put_object(
            Bucket=storage.bucket, Key=key, Body=body, ContentType=content_type
        )

    return await _run(_put, key=key, bucket=storage.bucket)


async def put_file(storage: Storage, key: str, path: Path, content_type: str) -> str:
    """Кладёт файл с диска, не читая его в память целиком.

    `upload_file` бьёт его на части и шлёт их потоком — образ прошивки весит
    до 54 МБ, и `read()` целиком означал бы столько же памяти на каждую
    загрузку.
    """
    if not storage.configured:
        return "хранилище не настроено"

    def _put() -> None:
        _client(storage).upload_file(
            str(path), storage.bucket, key, ExtraArgs={"ContentType": content_type}
        )

    return await _run(_put, key=key, bucket=storage.bucket)


async def size_of(storage: Storage, key: str) -> int | None:
    """Размер объекта в хранилище. `None` — его там нет или спросить не вышло.

    Отличать «нет» от «не спросилось» зовущему не нужно: и то, и другое
    означает, что ссылаться на этот объект нельзя.
    """
    if not storage.configured:
        return None

    def _head() -> int:
        return int(_client(storage).head_object(Bucket=storage.bucket, Key=key)["ContentLength"])

    try:
        return await asyncio.to_thread(_head)
    except Exception as exc:  # noqa: BLE001 — причин у чужого хранилища много
        log.info("storage.head_failed", key=key, error=str(exc))
        return None


async def remove(storage: Storage, key: str) -> None:
    """Убирает объект. Молча: запись в базе уже снята, а лишний файл
    в хранилище не мешает никому, кроме нас."""
    if not storage.configured:
        return

    def _delete() -> None:
        _client(storage).delete_object(Bucket=storage.bucket, Key=key)

    await _run(_delete, key=key, bucket=storage.bucket)


async def _run(job, *, key: str, bucket: str) -> str:
    try:
        await asyncio.to_thread(job)
    except Exception as exc:  # noqa: BLE001 — отказ чужого хранилища не наша ошибка
        log.warning("storage.failed", key=key, bucket=bucket, error=str(exc))
        return f"{exc.__class__.__name__}: {exc}"[:200]
    log.info("storage.put", key=key, bucket=bucket)
    return ""
