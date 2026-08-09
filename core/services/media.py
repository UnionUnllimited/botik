"""Картинки товаров: приём файла из админки и раздача с диска.

До сайта фото жило одним `photo_file_id` в Telegram — витрине оно бесполезно,
браузер такую ссылку не откроет. Файлы кладутся в том (`APP_MEDIA_DIR`)
и отдаются по `/media/...`, в товаре хранится этот путь.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import structlog

from core.config import settings

log = structlog.get_logger("services.media")

URL_PREFIX = "/media"

# Растровые форматы, которые понимает любой браузер. SVG намеренно нет:
# это документ со скриптами, а не картинка, и отдавать его со своего домена опасно.
ALLOWED_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class MediaError(RuntimeError):
    """Причина отказа с текстом для формы."""


def media_root() -> Path:
    return Path(settings.app.media_dir)


def save_image(data: bytes, content_type: str, *, prefix: str) -> str:
    """Сохраняет картинку и возвращает путь для `photo_url`.

    Имя придумываем сами и добавляем случайный хвост: имя из формы приходит
    от пользователя, и доверять ему в пути на диске нельзя. Хвост заодно
    обходит кеш браузера — иначе новая картинка не показывалась бы до Ctrl+F5.
    """
    if not data:
        raise MediaError("Файл пустой")
    if len(data) > settings.app.media_max_bytes:
        limit = settings.app.media_max_bytes // (1024 * 1024)
        raise MediaError(f"Файл больше {limit} МБ — сожмите картинку")

    extension = ALLOWED_TYPES.get(content_type.split(";")[0].strip().lower())
    if extension is None:
        raise MediaError("Только JPEG, PNG или WebP")

    root = media_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        name = f"{prefix}-{secrets.token_hex(4)}{extension}"
        (root / name).write_bytes(data)
    except OSError as exc:
        log.warning("media.save_failed", error=str(exc), root=str(root))
        raise MediaError("Не удалось сохранить файл на диск") from exc

    log.info("media.saved", name=name, bytes=len(data))
    return f"{URL_PREFIX}/{name}"


def delete_image(url: str | None) -> None:
    """Убирает старый файл. Молча: витрине важнее новая картинка, чем чистота диска."""
    if not url or not url.startswith(f"{URL_PREFIX}/"):
        return
    name = url.removeprefix(f"{URL_PREFIX}/")
    if "/" in name or "\\" in name or name.startswith("."):
        return
    try:
        (media_root() / name).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("media.delete_failed", name=name, error=str(exc))
