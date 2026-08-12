"""
Снимок живой SQLite в ZIP для бэкапа из админки.

Использует sqlite3.backup() — учитывает WAL и даёт согласованную копию без остановки сервиса.
Файлы .db-wal / .db-shm в архив не кладём: содержимое WAL включается в итоговый .db.

Архив поддерживает несколько БД одновременно (router_bot.db + devices/devices.db),
плюс опциональный .env.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import zipfile
from typing import Iterable, Optional


def _snapshot_to_temp(src_db: str, busy_timeout_sec: float) -> str:
    """Сделать WAL-safe снимок SQLite во временный файл, вернуть его путь."""
    fd, snap_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with sqlite3.connect(src_db, timeout=busy_timeout_sec) as src:
            dst = sqlite3.connect(snap_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        return snap_path
    except Exception:
        try:
            os.unlink(snap_path)
        except OSError:
            pass
        raise


def build_backup_zip(
    db_path: str,
    zip_path: str,
    *,
    env_path: Optional[str] = None,
    extra_db_paths: Optional[Iterable[str]] = None,
    busy_timeout_sec: float = 60.0,
) -> None:
    """
    Создать ZIP с одним или несколькими SQLite-снимками и опциональным .env.

    Args:
        db_path: путь к основной БД (например users.db / router_bot.db).
        zip_path: куда писать готовый архив.
        env_path: путь к .env. None или несуществующий файл — пропускается.
        extra_db_paths: дополнительные БД (например devices/devices.db).
            Несуществующие пути молча пропускаются.
        busy_timeout_sec: сколько ждать busy при подключении к SQLite.

    Имена файлов в архиве сохраняются как basename исходных, поэтому БД с одним
    и тем же именем класть нельзя (но в нашем случае разные имена: router_bot.db,
    devices.db). При коллизии будет ZIP с двумя одинаковыми именами — некритично,
    но лучше не доводить.
    """
    db_path = os.path.abspath(db_path)
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"Database file not found: {db_path}")

    # Снимки делаем заранее, потом одной операцией пакуем — так не держим
    # connection к SQLite в момент записи в zip (важно при большой БД).
    snapshots: list[tuple[str, str]] = []  # (snap_path, arcname)

    try:
        snapshots.append(
            (_snapshot_to_temp(db_path, busy_timeout_sec), os.path.basename(db_path))
        )

        for extra in extra_db_paths or ():
            if not extra:
                continue
            extra_abs = os.path.abspath(extra)
            if not os.path.isfile(extra_abs):
                # Пропускаем тихо: бэкап без отсутствующей БД лучше, чем ошибка.
                continue
            snapshots.append(
                (_snapshot_to_temp(extra_abs, busy_timeout_sec), os.path.basename(extra_abs))
            )

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for snap, arcname in snapshots:
                zipf.write(snap, arcname=arcname)
            if env_path and os.path.isfile(env_path):
                zipf.write(os.path.abspath(env_path), arcname=".env")
    finally:
        for snap, _ in snapshots:
            try:
                os.unlink(snap)
            except OSError:
                pass
