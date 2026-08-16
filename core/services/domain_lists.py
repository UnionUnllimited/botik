"""Сборка списков доменов и подсетей для роутеров.

Переехало со скрипта `sync_local.sh` на сервере frps. Правила чистки те же —
списки у поставщиков лежат в разном виде, и разойтись с прежним поведением
значит поменять то, что попадёт в туннель, у всего парка сразу.

Собранное отдаётся с нашего домена и, если настроено, кладётся копией
в объектное хранилище: списки тянет весь парк разом, и выкат нашего сервера
не должен оставлять роутеры без обновления.
"""

from __future__ import annotations

import re

import httpx
import structlog

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
