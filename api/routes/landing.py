"""Витрина в корне домена: что это за роутер, сколько стоит и как купить.

Страница собирается на сервере, а не в браузере: её открывают с телефона
по ссылке из бота и находят в поиске, и пустой экран до загрузки скриптов
здесь дороже, чем удобство сборки.

Купить на самой витрине нельзя намеренно — каждая кнопка ведёт в бота
на карточку модели (`?start=buy_<id>`). Оформление, оплата и подписка уже
работают там, а второй путь развёл бы заказы по двум местам.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from core.config import settings
from core.services import landing

log = structlog.get_logger("api.landing")

router = APIRouter(include_in_schema=False)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Имена, под которыми знак кладут в статику. Порядок — очередь поиска:
# положенный руками `logo.png` побеждает нарисованный нами `logo.svg`,
# и заменить знак можно, просто закоммитив файл, — без настроек и правок.
LOGO_FILES = ("logo.png", "logo.webp", "logo.jpg", "logo.svg")
FAVICON_FILES = ("favicon.png", "favicon.webp", "favicon.ico", "favicon.svg")


def _static_url(names: tuple[str, ...], default: str) -> str:
    """Первый из файлов, который правда лежит в статике."""
    for name in names:
        if (STATIC_DIR / name).is_file():
            return f"/static/{name}"
    return default


def logo_fallback() -> str:
    return _static_url(LOGO_FILES, landing.DEFAULT_LOGO_URL)


def favicon_fallback() -> str:
    return _static_url(FAVICON_FILES, landing.DEFAULT_FAVICON_URL)

# Пути, которые обязаны отвечать JSON и на ошибках: их читают провайдер оплаты,
# мониторинг, бот и его админка. Страница с извинениями им не нужна — она
# сломает разбор ответа там, где никто не ждёт HTML.
SERVICE_PREFIXES: tuple[str, ...] = (
    "/api",
    "/webhooks",
    "/healthz",
    "/readyz",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/lists",
    "/firmware",
    "/media",
    "/panel",
    "/cgi-bin",
    "/luci",
    "/luci-static",
    "/ubus",
    "/static",
    # Только ручки приложения в Telegram, не сама страница: оболочка на /app —
    # обычная страница, и на 404 ей полагается наш экран с извинениями. А вот
    # её запросы за данными разбираются как JSON, и страница вместо ответа
    # ломала бы разбор с «Unexpected token '<'» вместо причины отказа.
    "/app/api",
)

ERROR_TITLES: dict[int, str] = {
    404: "Такой страницы нет",
    500: "Что-то сломалось у нас",
}


def is_page_request(path: str) -> bool:
    """Страница это или служебный путь. Всё, что не перечислено, — страница."""
    return not any(path == prefix or path.startswith(prefix + "/") for prefix in SERVICE_PREFIXES)


def error_page(request: Request, status_code: int) -> HTMLResponse:
    title = ERROR_TITLES.get(status_code, "Ошибка")
    return templates.TemplateResponse(
        request,
        "landing_error.html",
        {
            "brand": settings.app.brand,
            "code": status_code,
            "title": title,
            "favicon_url": favicon_fallback(),
        },
        status_code=status_code,
    )


@router.get("/", response_class=HTMLResponse)
async def landing_page(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    if not await landing.is_enabled(session):
        # Выключенная витрина — не ошибка сервера: домен просто ничего
        # не показывает, а ручки и панель роутера продолжают работать.
        return templates.TemplateResponse(
            request,
            "landing_off.html",
            {"brand": settings.app.brand, "favicon_url": favicon_fallback()},
            status_code=404,
        )

    content = await landing.page_content(
        session, logo_fallback=logo_fallback(), favicon_fallback=favicon_fallback()
    )
    return templates.TemplateResponse(request, "landing.html", content)


@router.get("/instruction", response_class=HTMLResponse)
async def instruction_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Что делать с приехавшим роутером — читается один раз, в самом начале.

    Адрес постоянный: на него ведёт кнопка в карточке заказа, пока посылка
    едет. Оператор может увести её на свою страницу настройкой
    `router.setup_url`.
    """
    return templates.TemplateResponse(
        request,
        "instruction.html",
        {
            "brand": settings.app.brand,
            "logo_url": await landing.logo_url(session, logo_fallback()),
            "favicon_url": await landing.favicon_url(session, favicon_fallback()),
            "steps": landing.INSTRUCTION_STEPS,
            "connection_types": landing.CONNECTION_TYPES,
            "bot_url": landing.bot_link(),
        },
    )


@router.get("/guide", response_class=HTMLResponse)
async def guide_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Постоянная инструкция: она нужна не в первый день, а потом.

    Отдельно от шагов подключения: те читают один раз, а сюда возвращаются
    с вопросами «где пароль от Wi-Fi», «как продлить», «пропал интернет».
    Кнопка на неё есть у клиента в «Моём роутере» всегда.
    """
    return templates.TemplateResponse(
        request,
        "guide.html",
        {
            "brand": settings.app.brand,
            "logo_url": await landing.logo_url(session, logo_fallback()),
            "favicon_url": await landing.favicon_url(session, favicon_fallback()),
            "sections": landing.GUIDE_SECTIONS,
            "bot_url": landing.bot_link(),
        },
    )
