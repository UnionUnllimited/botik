"""Помощь клиенту с роутером: сайт, Wi-Fi, устройства дома, отзыв.

Клиентские ручки — под каталогом, рядом с перезагрузкой и выбором узла,
и тем же порядком: роутер ищется среди принадлежащих клиенту
(`catalog_api._own_router`), к молчащему не ходим. Бот зовёт их со своим
токеном и `tg_id`, приложение — через `miniapp.py`, где `tg_id` берётся
только из подписи Telegram.

Ручки оператора — под парком: список просьб и отзывов для админки и решение
по просьбе, которое бот зовёт из кнопки в топике «Роутеры».
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session, get_transaction
from api.routes.catalog_api import _int, _own_router, _reachable
from api.service_auth import require_token
from core.errors import describe
from core.models import Device, SiteRequest, User
from core.redis_client import RateLimiter
from core.services import home_network, reviews, router_shell, router_wifi, site_help
from core.services import routers as routers_service

log = structlog.get_logger("api.care")

router = APIRouter(
    tags=["care"],
    include_in_schema=False,
    dependencies=[Depends(require_token)],
)
"""Один роутер на обе стороны, с полными путями: клиентские ручки живут
под каталогом, ручки оператора — под парком, а замок у них один и тот же."""

CLIENT = "/api/v1/catalog"
MANAGE = "/api/v1/fleet"


WIFI_CHANGES_PER_HOUR = 5
"""Каждая смена пароля отключает от Wi-Fi все устройства дома. Пять в час —
с запасом на опечатку и передумал, но не круг «сменил — не подключилось —
сменил ещё»."""

ROUTER_READS_PER_HOUR = 30
SITE_CHECKS_PER_HOUR = 30
SITE_REQUESTS_PER_DAY = 10
"""Проверка дешёвая, но ходит к роутеру за переключателем; просьба — это
сообщение живому оператору, и десять в день от одного клиента уже много."""


# ── Wi-Fi ────────────────────────────────────────────────────────────────────


def _networks(networks: list[router_wifi.Network]) -> list[dict]:
    return [network.as_dict() for network in networks]


async def _read_too_often(device: Device) -> bool:
    """Чтение с роутера — это SSH-сессия через туннель. Экран обновляют
    руками, и тридцати в час на роутер хватает с запасом, а параллельный
    залп запросов из приложения забил бы туннель самому же клиенту."""
    allowed, _ = await RateLimiter().hit(
        f"router_read:{device.id}", limit=ROUTER_READS_PER_HOUR, window_sec=3600
    )
    return not allowed


@router.get(CLIENT + "/my-router/wifi")
async def my_router_wifi(
    tg_id: int = Query(0),
    device_id: int = Query(0),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Сети роутера с паролями. Владелец видит свой пароль — ради этого
    и спрашивают: наклейку потеряли, гость просит подключиться."""
    device = await _own_router(session, tg_id, device_id)
    if await _read_too_often(device):
        return {"ok": False, "error": "too_often", "networks": []}
    if not _reachable(device):
        return {"ok": False, "error": "offline", "networks": []}
    try:
        networks = await router_wifi.read(device)
    except router_wifi.WifiError as exc:
        return {"ok": False, "error": "refused", "message": str(exc), "networks": []}
    except router_shell.ShellError as exc:
        log.info("care.wifi_unreachable", device_id=device.id, error=describe(exc))
        return {"ok": False, "error": "unreachable", "networks": []}
    return {"ok": True, "error": "", "networks": _networks(networks)}


@router.post(CLIENT + "/my-router/wifi")
async def my_router_wifi_change(payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    """Смена пароля на всех закрытых сетях роутера.

    `check_only` — только проверить вид пароля: бот показывает «точно
    сменить?» лишь тому паролю, который роутер примет, а правило одно —
    здесь, а не копией в боте."""
    device = await _own_router(session, _int(payload.get("tg_id")), _int(payload.get("device_id")))
    password = str(payload.get("password") or "")
    error = router_wifi.check_password(password)
    if error:
        return {"ok": False, "error": "refused", "message": error}
    if payload.get("check_only"):
        return {"ok": True, "error": ""}

    allowed, _ = await RateLimiter().hit(
        f"router_wifi:{device.id}", limit=WIFI_CHANGES_PER_HOUR, window_sec=3600
    )
    if not allowed:
        return {"ok": False, "error": "too_often"}
    if not _reachable(device):
        return {"ok": False, "error": "offline"}

    try:
        networks = await router_wifi.change_password(device, password)
    except router_wifi.WifiError as exc:
        return {"ok": False, "error": "refused", "message": str(exc)}
    except router_shell.ShellError as exc:
        log.warning("care.wifi_change_failed", device_id=device.id, error=describe(exc))
        return {"ok": False, "error": "unreachable"}

    # Сам пароль в журнал не пишем: журнал читают операторы, а пароль
    # от домашней сети — не их дело.
    routers_service.add_event(
        session, device_id=device.id, mac=device.mac, level="info", message="Клиент сменил пароль Wi-Fi"
    )
    await session.commit()
    return {"ok": True, "error": "", "networks": _networks(networks)}


# ── Кто дома в сети ──────────────────────────────────────────────────────────


@router.get(CLIENT + "/my-router/devices")
async def my_router_devices(
    tg_id: int = Query(0),
    device_id: int = Query(0),
    session: AsyncSession = Depends(get_session),
) -> dict:
    device = await _own_router(session, tg_id, device_id)
    if await _read_too_often(device):
        return {"ok": False, "error": "too_often", "devices": []}
    if not _reachable(device):
        return {"ok": False, "error": "offline", "devices": []}
    try:
        found = await home_network.read(device)
    except router_shell.ShellError as exc:
        log.info("care.devices_unreachable", device_id=device.id, error=describe(exc))
        return {"ok": False, "error": "unreachable", "devices": []}
    return {
        "ok": True,
        "error": "",
        "devices": [item.as_dict() for item in found],
        "active": sum(1 for item in found if item.active),
    }


# ── Не открывается сайт ──────────────────────────────────────────────────────


@router.post(CLIENT + "/my-router/site-check")
async def my_router_site_check(payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    device = await _own_router(session, _int(payload.get("tg_id")), _int(payload.get("device_id")))
    allowed, _ = await RateLimiter().hit(
        f"site_check:{device.id}", limit=SITE_CHECKS_PER_HOUR, window_sec=3600
    )
    if not allowed:
        return {"ok": False, "error": "too_often"}
    try:
        return await site_help.check(session, device, str(payload.get("site") or ""))
    except site_help.BadSiteError as exc:
        return {"ok": False, "error": "bad_site", "message": str(exc)}


@router.post(CLIENT + "/my-router/site-request")
async def my_router_site_request(payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    """Просьба разобраться с сайтом — оператору в топик и в админку."""
    device = await _own_router(session, _int(payload.get("tg_id")), _int(payload.get("device_id")))
    user = await session.get(User, device.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    allowed, _ = await RateLimiter().hit(
        f"site_request:{user.id}", limit=SITE_REQUESTS_PER_DAY, window_sec=86400
    )
    if not allowed:
        return {"ok": False, "error": "too_often"}
    try:
        request, created = await site_help.create(
            session, user, device, str(payload.get("site") or ""), note=str(payload.get("note") or "")
        )
    except site_help.BadSiteError as exc:
        return {"ok": False, "error": "bad_site", "message": str(exc)}
    if created:
        routers_service.add_event(
            session,
            device_id=device.id,
            mac=device.mac,
            level="info",
            message=f"Клиент сообщил: не открывается {request.value}",
        )
    await session.commit()
    return {
        "ok": True,
        "error": "",
        "created": created,
        "site": site_help.shown(request.value),
    }


# ── Отзыв ────────────────────────────────────────────────────────────────────


@router.post(CLIENT + "/reviews/{review_id}/rate")
async def review_rate(
    review_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    review = await reviews.own_review(session, review_id, _int(payload.get("tg_id")))
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    rating = _int(payload.get("rating"))
    if rating < 1 or rating > 5:
        return {"ok": False, "error": "bad_rating"}
    await reviews.rate(session, review, rating)
    return {"ok": True, "error": "", "rating": rating, "low": rating <= reviews.LOW}


@router.post(CLIENT + "/reviews/{review_id}/text")
async def review_text(
    review_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    review = await reviews.own_review(session, review_id, _int(payload.get("tg_id")))
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    await reviews.add_text(session, review, str(payload.get("text") or ""))
    return {"ok": True, "error": ""}


# ── Оператору ────────────────────────────────────────────────────────────────


@router.get(MANAGE + "/site-requests")
async def site_requests(
    status_filter: str = Query("", alias="status", max_length=16),
    session: AsyncSession = Depends(get_session),
) -> dict:
    counts = dict(
        (await session.execute(select(SiteRequest.status, func.count()).group_by(SiteRequest.status))).all()
    )
    return {
        "items": await site_help.recent(session, status=status_filter),
        "counts": {key: int(value) for key, value in counts.items()},
    }


@router.post(MANAGE + "/site-requests/{request_id}/resolve")
async def site_request_resolve(
    request_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Решение по просьбе — из кнопки в топике или со страницы админки.

    В ответе — текст и кнопки сообщения после решения: бот перерисовывает
    им то же сообщение в топике, а не шлёт второе."""
    request = await session.get(SiteRequest, request_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    error = await site_help.resolve(
        session, request, str(payload.get("action") or ""), author=str(payload.get("author") or "")
    )
    user = await session.get(User, request.user_id)
    device = await session.get(Device, request.device_id) if request.device_id else None
    note = site_help.resolution_note(request)
    text = site_help.operator_text(request, user, device)
    return {
        "ok": not error,
        "error": error,
        "status": request.status,
        "text": f"{text}\n\n{note}" if note else text,
        "buttons": site_help.operator_buttons(request),
    }


@router.get(MANAGE + "/reviews")
async def reviews_list(session: AsyncSession = Depends(get_session)) -> dict:
    return {"items": await reviews.recent(session), "summary": await reviews.summary(session)}
