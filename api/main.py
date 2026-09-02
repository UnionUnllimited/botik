"""FastAPI-приложение: витрина, ручки каталога и парка, вебхуки, панель роутера.

Своей админки здесь нет — интерфейс оператора один, в стороннем продукте
в `bot/`. За нами данные и всё, что физически может делать только процесс
в нашей сети: туннели к роутерам, активация по SSH, проксирование панели
и приём оплаты.

Витрина в корне — не второй кабинет: она рассказывает про товар и уводит
в бота. Кабинет, регистрация и заказы остаются в одном месте.
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
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.routes import (
    catalog_api,
    firmware_api,
    fleet_api,
    health,
    landing,
    lists_api,
    miniapp,
    panel_proxy,
    terminal,
    webhooks,
)
from core.config import settings
from core.db import check_database, dispose_engine
from core.logging import configure_logging
from core.metrics import api_request_seconds, api_requests_total
from core.notifications import close_bot
from core.payments import close_providers
from core.redis_client import check_redis, close_redis
from core.sentry import init_sentry
from core.services import firmware, media
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

    # Образы прошивки. Свой каталог и свой префикс: в `/media` лежат картинки
    # товаров, а тут файлы по 27–54 МБ, за которыми приходит парк железа.
    # Отдаёт их StaticFiles, а не наш обработчик: он умеет Range и `304`,
    # а на такие размеры это разница между дозагрузкой и повторной закачкой.
    images_root = firmware.images_root()
    try:
        images_root.mkdir(parents=True, exist_ok=True)
        app.mount(
            firmware.IMAGES_PREFIX, StaticFiles(directory=str(images_root)), name="firmware"
        )
    except OSError as exc:
        log.error("api.firmware_mount_failed", error=str(exc), path=str(images_root))

    # Стили витрины. Отдельным каталогом, а не вместе с картинками товаров:
    # media — том с загруженными файлами, static едет в образе.
    try:
        app.mount("/static", StaticFiles(directory=str(landing.STATIC_DIR)), name="static")
    except (OSError, RuntimeError) as exc:
        log.error("api.static_mount_failed", error=str(exc), path=str(landing.STATIC_DIR))

    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(fleet_api.router)
    app.include_router(catalog_api.router)
    app.include_router(lists_api.router)
    app.include_router(firmware_api.router)
    # Живой терминал — до панели: у той корневые пути, и она перехватила бы
    # `/terminal/*` вместе со всем остальным.
    app.include_router(terminal.router)
    # Приложение в Telegram — по той же причине до панели: `/app/*`.
    app.include_router(miniapp.router)
    # Панель роутера отдаётся по корневым путям: LuCI строит абсолютные ссылки.
    app.include_router(panel_proxy.router)
    # Витрина последней: у неё корень, и перехватывать чужие пути ей нечем.
    app.include_router(landing.router)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        # Человеку в браузере — страница, машине — JSON. Разбирается по пути,
        # а не по заголовку Accept: его подделывает кто угодно, а список
        # служебных путей у нас известен точно.
        if landing.is_page_request(request.url.path) and request.method in ("GET", "HEAD"):
            return landing.error_page(request, exc.status_code)
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": "invalid_request", "detail": exc.errors()})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
        log.exception("api.unhandled", error=str(exc), path=request.url.path)
        if landing.is_page_request(request.url.path) and request.method in ("GET", "HEAD"):
            return landing.error_page(request, 500)
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    return app


app = create_app()
