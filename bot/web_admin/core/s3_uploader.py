"""
Загрузка готового файла бэкапа в S3-совместимое хранилище.

Поддерживаются любые провайдеры с S3 API:
  - AWS S3 (endpoint пустой)
  - Yandex Object Storage   (https://storage.yandexcloud.net)
  - Selectel S3             (https://s3.storage.selcloud.ru)
  - Backblaze B2 (S3 API)   (https://s3.us-west-XXX.backblazeb2.com)
  - MinIO / self-hosted     (свой endpoint)

boto3 импортируется лениво: если пакет не установлен — функция кинет понятную
ошибку, чтобы админ увидел её во flash, а не в виде 500.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class S3NotConfigured(RuntimeError):
    """boto3 не установлен или критические параметры пусты."""


class S3UploadError(RuntimeError):
    """Любая ошибка взаимодействия с S3 (auth/network/permissions/etc.)."""


def _import_boto3():
    try:
        import boto3  # type: ignore
        from botocore.config import Config as BotoConfig  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
        return boto3, BotoConfig, BotoCoreError, ClientError
    except ImportError as e:
        raise S3NotConfigured(
            "boto3 не установлен. Установите: pip install boto3"
        ) from e


def _build_client(
    *,
    endpoint_url: Optional[str],
    region_name: Optional[str],
    access_key: str,
    secret_key: str,
):
    boto3, BotoConfig, _BotoCoreError, _ClientError = _import_boto3()
    cfg = BotoConfig(
        signature_version="s3v4",
        retries={"max_attempts": 3, "mode": "standard"},
        connect_timeout=10,
        read_timeout=60,
    )
    return boto3.client(
        "s3",
        endpoint_url=(endpoint_url or None),
        region_name=(region_name or None),
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=cfg,
    )


def _normalize_key(prefix: str, filename: str) -> str:
    """Собрать object key вида '<prefix>/<filename>' c аккуратной обработкой слешей."""
    p = (prefix or "").strip().strip("/")
    name = (filename or "").lstrip("/")
    return f"{p}/{name}" if p else name


def upload_file_sync(
    file_path: str,
    *,
    endpoint_url: Optional[str],
    region_name: Optional[str],
    bucket: str,
    access_key: str,
    secret_key: str,
    object_key: str,
    content_type: str = "application/zip",
) -> str:
    """
    Синхронная загрузка файла в S3. Возвращает финальный object_key.
    Бросает S3NotConfigured / S3UploadError при ошибках.
    """
    if not bucket:
        raise S3NotConfigured("Не указан S3 bucket")
    if not access_key or not secret_key:
        raise S3NotConfigured("Не указаны S3 access_key / secret_key")
    if not os.path.isfile(file_path):
        raise S3UploadError(f"Файл не найден: {file_path}")

    _boto3, _BotoConfig, BotoCoreError, ClientError = _import_boto3()
    client = _build_client(
        endpoint_url=endpoint_url,
        region_name=region_name,
        access_key=access_key,
        secret_key=secret_key,
    )
    try:
        client.upload_file(
            Filename=file_path,
            Bucket=bucket,
            Key=object_key,
            ExtraArgs={"ContentType": content_type},
        )
        return object_key
    except (BotoCoreError, ClientError) as e:
        # Маскируем секреты на всякий случай: текст ошибок boto уже не содержит
        # ключи, но не даём всплыть голому stacktrace в UI.
        raise S3UploadError(f"S3 upload failed: {e}") from e
    except Exception as e:  # noqa: BLE001 — нужно перехватить и сетевые
        raise S3UploadError(f"S3 upload failed: {e}") from e


async def upload_file_async(
    file_path: str,
    *,
    endpoint_url: Optional[str],
    region_name: Optional[str],
    bucket: str,
    access_key: str,
    secret_key: str,
    prefix: str,
    filename: str,
) -> str:
    """
    Асинхронный wrapper: blocking-вызов boto3 переносится в executor.
    Возвращает финальный object_key, под которым лежит файл.
    """
    object_key = _normalize_key(prefix, filename)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: upload_file_sync(
            file_path,
            endpoint_url=endpoint_url,
            region_name=region_name,
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
            object_key=object_key,
        ),
    )


__all__ = [
    "S3NotConfigured",
    "S3UploadError",
    "upload_file_sync",
    "upload_file_async",
]
