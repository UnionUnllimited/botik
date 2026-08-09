"""Шаблоны сайта: своё окружение Jinja2, общие с админкой фильтры.

Окружение отдельное, потому что каталоги шаблонов разные и наследование
`{% extends "base.html" %}` иначе бы разъехалось: у админки свой base.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from api.formatting import FILTERS
from api.site.auth import CSRF_FIELD, HONEYPOT_FIELD, Client
from core.config import settings

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters.update(FILTERS)
templates.env.globals.update(
    {
        "brand": settings.app.brand,
        "csrf_field": CSRF_FIELD,
        "honeypot_field": HONEYPOT_FIELD,
    }
)


def render(
    request: Request,
    template: str,
    client: Client | None = None,
    *,
    status_code: int = 200,
    **context: Any,
) -> HTMLResponse:
    payload: dict[str, Any] = {
        "request": request,
        "client": client,
        "csrf_token": client.session.csrf if client else "",
        "current_path": request.url.path,
    }
    payload.update(context)
    return templates.TemplateResponse(request, template, payload, status_code=status_code)
