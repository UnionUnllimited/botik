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
    "/media",
    "/panel",
    "/cgi-bin",
    "/luci",
    "/luci-static",
    "/ubus",
    "/static",
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
        {"brand": settings.app.brand, "code": status_code, "title": title},
        status_code=status_code,
    )


@router.get("/", response_class=HTMLResponse)
async def landing_page(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    if not await landing.is_enabled(session):
        # Выключенная витрина — не ошибка сервера: домен просто ничего
        # не показывает, а ручки и панель роутера продолжают работать.
        return templates.TemplateResponse(
            request, "landing_off.html", {"brand": settings.app.brand}, status_code=404
        )

    content = await landing.page_content(session)
    return templates.TemplateResponse(request, "landing.html", content)


@router.get("/instruction", response_class=HTMLResponse)
async def instruction_page(request: Request) -> HTMLResponse:
    """Что делать с приехавшим роутером.

    Заглушка до настоящей инструкции: пять шагов, которые всё равно придётся
    написать. Адрес постоянный — на него ведёт кнопка в боте, и менять его
    вместе с текстом не придётся. Оператор может увести кнопку на свою
    страницу настройкой `router.instruction_url`.
    """
    return templates.TemplateResponse(
        request,
        "instruction.html",
        {"brand": settings.app.brand, "steps": landing.INSTRUCTION_STEPS, "bot_url": landing.bot_link()},
    )
