"""Публичный сайт: витрина, регистрация, вход и личный кабинет клиента.

Маршруты живут в корне домена (`/`, `/login`, `/cabinet`), админка — под `/admin`.
Пересечься они не могут: панель роутера проксируется по своим корневым путям
(`/cgi-bin`, `/luci-static`, `/ubus`), витрина эти имена не занимает.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from api.site.auth import LoginRequired
from api.site.routes import account, catalog

router = APIRouter()
router.include_router(account.router)
router.include_router(catalog.router)


async def login_redirect_handler(_: Request, exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse(exc.headers.get("Location", "/login"), status_code=303)


__all__ = ["LoginRequired", "login_redirect_handler", "router"]
