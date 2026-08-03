"""Веб-админка.

Собрана на FastAPI + Jinja2 без клиентских библиотек: панель открывают
с телефона, в том числе на плохой связи, и она не должна зависеть от
доступности чужих CDN. Обоснование выбора против SQLAdmin — в docs/decisions.md.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from api.admin.auth import LoginRequired
from api.admin.routes import (
    auth,
    catalog,
    clients,
    dashboard,
    fleet,
    infra,
    orders,
    promo,
    subscriptions,
    system,
)

router = APIRouter(prefix="/admin", include_in_schema=False)

# Порядок важен: auth не требует сессии, остальные — требуют.
router.include_router(auth.router)
router.include_router(dashboard.router)
router.include_router(orders.router)
router.include_router(clients.router)
router.include_router(subscriptions.router)
router.include_router(fleet.router)
router.include_router(infra.router)
router.include_router(catalog.router)
router.include_router(promo.router)
router.include_router(system.router)


async def login_redirect_handler(request: Request, exc: LoginRequired) -> RedirectResponse:
    """Не авторизован — отправляем на форму входа, а не отдаём голый 403."""
    location = exc.headers.get("Location", "/admin/login") if exc.headers else "/admin/login"
    return RedirectResponse(location, status_code=303)


__all__ = ["LoginRequired", "login_redirect_handler", "router"]
