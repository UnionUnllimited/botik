"""FastAPI-приложение: админка, вебхуки платежей, парк роутеров.

Публичного сайта здесь больше нет: клиентская часть ушла в сторонний продукт
в `bot/` вместе с ботом. За нами осталось то, чего у него нет, — админка,
управление парком роутеров и туннели к ним.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from api import admin
from api.admin.routes import luci
from api.routes import catalog_api, fleet_api, health, webhooks
from core.config import settings
from core.db import check_database, dispose_engine
from core.logging import configure_logging
from core.metrics import api_request_seconds, api_requests_total
from core.notifications import close_bot
from core.payments import close_providers
from core.redis_client import check_redis, close_redis
from core.sentry import init_sentry
from core.services import media
from core.services.frp import close_dashboard
from core.services.remnawave import close_client as close_remnawave

log = structlog.get_logger("api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging("api")
    init_sentry("api")
    db_ok = await check_database()
    redis_ok = await check_redis()
    log.info("api.startup", db=db_ok, redis=redis_ok, env=settings.app.env)
    if not db_ok:
        # Не падаем: контейнер поднимется, healthcheck покажет проблему,
        # docker перезапустит зависимость. Но в лог пишем явно.
        log.error("api.startup.database_unavailable")
    try:
        yield
    finally:
        await close_providers()
        await close_bot()
        await close_dashboard()
        await close_remnawave()
        await close_redis()
        await dispose_engine()
        log.info("api.shutdown")


def _route_template(request: Request) -> str:
    """Шаблон маршрута вместо конкретного пути — иначе метрики взорвутся по кардинальности."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"


async def observability_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed = time.perf_counter() - started
        api_requests_total.labels(request.method, _route_template(request), "500").inc()
        api_request_seconds.labels(request.method, _route_template(request)).observe(elapsed)
        log.exception("api.request.failed", method=request.method, path=request.url.path)
        raise
    elapsed = time.perf_counter() - started
    template = _route_template(request)
    api_requests_total.labels(request.method, template, str(response.status_code)).inc()
    api_request_seconds.labels(request.method, template).observe(elapsed)
    response.headers["X-Request-Id"] = request_id
    if response.status_code >= 500 or elapsed > 1.0:
        log.warning(
            "api.request.slow_or_failed",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(elapsed * 1000),
        )
    return response


def _is_admin_page(request: Request) -> bool:
    """Страницам админки нужен HTML: голый JSON в браузере ничего не объясняет."""
    return request.url.path.startswith("/admin") and "text/html" in request.headers.get("accept", "")


def _admin_error_page(message: str, status_code: int) -> HTMLResponse:
    return HTMLResponse(
        f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Ошибка · {settings.app.brand}</title></head>
<body style="margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1115;color:#e6e8ee;font:15px/1.6 -apple-system,'Segoe UI',Roboto,sans-serif">
<div style="max-width:460px;padding:26px;background:#171a21;border:1px solid #2a2f3a;border-radius:12px">
<h1 style="font-size:19px;margin:0 0 10px">Ошибка {status_code}</h1>
<p style="color:#99a0ae;margin:0 0 18px">{message}</p>
<a href="/admin/" style="color:#4c8dff">← в админку</a>
</div></body></html>""",
        status_code=status_code,
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{settings.app.brand} API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.api.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.api.docs_enabled else None,
    )
    app.middleware("http")(observability_middleware)

    # Картинки товаров лежат на диске, а не в Telegram: витрине нужен обычный URL.
    # Каталог создаётся здесь же — StaticFiles на несуществующей папке падает при старте.
    media_root = media.media_root()
    try:
        media_root.mkdir(parents=True, exist_ok=True)
        app.mount(media.URL_PREFIX, StaticFiles(directory=str(media_root)), name="media")
    except OSError as exc:
        # Без картинок сайт работает, без запуска — нет.
        log.error("api.media_mount_failed", error=str(exc), path=str(media_root))

    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(fleet_api.router)
    app.include_router(catalog_api.router)
    app.include_router(admin.router)
    # Панель роутера отдаётся по корневым путям: LuCI строит абсолютные ссылки.
    app.include_router(luci.router)
    app.add_exception_handler(admin.LoginRequired, admin.login_redirect_handler)  # type: ignore[arg-type]

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        if _is_admin_page(request):
            return _admin_error_page(str(exc.detail), exc.status_code)
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": "invalid_request", "detail": exc.errors()})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
        log.exception("api.unhandled", error=str(exc), path=request.url.path)
        if _is_admin_page(request):
            return _admin_error_page("Внутренняя ошибка. Подробности в логах: docker compose logs api", 500)
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    return app


app = create_app()
