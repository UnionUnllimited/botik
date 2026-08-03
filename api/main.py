"""FastAPI-приложение: API устройств, вебхуки платежей, веб-админка.

На этапе 1 подняты только служебные маршруты (health/ready/metrics) —
на них завязаны healthcheck докера и мониторинг.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.routes import health
from core.config import settings
from core.db import check_database, dispose_engine
from core.logging import configure_logging
from core.metrics import api_request_seconds, api_requests_total
from core.redis_client import check_redis, close_redis
from core.sentry import init_sentry

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
    app.include_router(health.router)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": "invalid_request", "detail": exc.errors()})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
        log.exception("api.unhandled", error=str(exc))
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    return app


app = create_app()
