"""Шаблоны админки: окружение Jinja2 и фильтры отображения.

Сами фильтры общие с сайтом и лежат в `api/formatting.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from api.admin.auth import CSRF_FIELD, Principal
from api.formatting import (
    FILTERS,
    STATUS_LABELS,
    STATUS_TONES,
    bytes_human,
    date_ru,
    datetime_ru,
    days_until,
    money,
    phone_pretty,
    short_datetime,
    status_label,
    status_tone,
)
from core.config import settings

__all__ = [
    "STATUS_LABELS",
    "STATUS_TONES",
    "bytes_human",
    "date_ru",
    "datetime_ru",
    "days_until",
    "money",
    "phone_pretty",
    "render",
    "short_datetime",
    "status_label",
    "status_tone",
    "templates",
]

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Без ensure_ascii=False фильтр `tojson` экранирует кириллицу в escape-последовательности,
# и характеристики товара в форме становится невозможно прочитать, не то что править.
templates.env.policies["json.dumps_kwargs"] = {"sort_keys": True, "ensure_ascii": False}
templates.env.filters.update(FILTERS)
templates.env.globals.update(
    {
        "brand": settings.app.brand,
        "csrf_field": CSRF_FIELD,
    }
)


def render(
    request: Request,
    template: str,
    principal: Principal | None = None,
    **context: Any,
) -> HTMLResponse:
    payload: dict[str, Any] = {
        "request": request,
        "principal": principal,
        "csrf_token": principal.session.csrf if principal else "",
        "current_path": request.url.path,
    }
    payload.update(context)
    return templates.TemplateResponse(request, template, payload)
