"""Раздел «Роутеры»: парк устройств, телеметрия, события, доступ к панели.

Данные берутся из двух источников: дашборд frps знает, кто держит туннель,
а сам роутер отдаёт показания по своему HTTP-API через этот туннель.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.admin import audit
from api.admin.auth import (
    Principal,
    form_value,
    load_session,
    require_section,
    update_session,
    verify_csrf,
)
from api.admin.templating import render
from api.deps import get_session, get_transaction
from core.config import settings
from core.dates import utcnow
from core.enums import DeviceStatus
from core.models import Device, DeviceEvent, Heartbeat, Plan
from core.security import normalize_mac
from core.services import activation
from core.services import routers as router_service
from core.services import subscriptions as subscription_service
from core.services.frp import FrpError, RouterApi, dashboard

router = APIRouter(prefix="/fleet")
log = structlog.get_logger("admin.fleet")

PAGE_SIZE = 40

CHART_POINTS = 96
"""Столбиков на графике за сутки. Роутер опрашивается раз в пять минут, то есть
за сутки набегает под три сотни точек — в колонку такой ряд не влезает, и график
либо распирал страницу, либо ужимался в неразличимые полоски."""


def _thin_out(rows: list[Heartbeat], limit: int = CHART_POINTS) -> list[Heartbeat]:
    """Прореживает ряд равномерно, сохраняя последнее измерение.

    Считаем от конца: правый край графика — это «сейчас», и он должен совпадать
    с показаниями в карточке. Потеря лишней точки в начале суток не значит ничего.
    """
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    picked = [rows[min(int(index * step), len(rows) - 1)] for index in range(limit)]
    picked[-1] = rows[-1]
    return picked


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def fleet_list(
    request: Request,
    principal: Principal = Depends(require_section("devices")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    query_text = request.query_params.get("q", "").strip()
    only = request.query_params.get("only", "").strip()
    page = max(int(request.query_params.get("page", "1") or 1), 1)

    query = select(Device).options(selectinload(Device.user))
    counter = select(func.count()).select_from(Device)

    if only == "online":
        query = query.where(Device.frp_online.is_(True))
        counter = counter.where(Device.frp_online.is_(True))
    elif only == "offline":
        query = query.where(Device.frp_online.is_(False))
        counter = counter.where(Device.frp_online.is_(False))
    elif only == "problem":
        # Туннель есть, а сервис доступа на роутере не работает.
        condition = Device.frp_online.is_(True) & (Device.service_status != "running")
        query = query.where(condition)
        counter = counter.where(condition)

    if query_text:
        mac = normalize_mac(query_text)
        pattern = f"%{query_text}%"
        condition = or_(
            Device.mac == mac if mac else Device.mac.ilike(pattern),
            Device.model.ilike(pattern),
            Device.board.ilike(pattern),
            Device.last_wan_ip.ilike(pattern),
        )
        query = query.where(condition)
        counter = counter.where(condition)

    total = await session.scalar(counter) or 0
    devices = list(
        await session.scalars(
            query.order_by(Device.frp_online.desc(), Device.id.desc())
            .limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)
        )
    )

    online_total = (
        await session.scalar(select(func.count()).select_from(Device).where(Device.frp_online.is_(True))) or 0
    )
    events = await router_service.recent_events(session, limit=15)

    server_info: dict = {}
    frps_error: str | None = None
    if settings.frp.is_configured:
        try:
            # Жёсткий предел: страница не должна ждать внешний сервис.
            # Статусы роутеров всё равно берутся из базы, их собирает воркер.
            server_info = await asyncio.wait_for(dashboard().server_info(), timeout=3)
        except TimeoutError:
            frps_error = "дашборд не ответил за 3 секунды"
        except Exception as exc:  # noqa: BLE001 — страница обязана открыться и без frps
            frps_error = str(exc)[:200]

    return render(
        request,
        "fleet.html",
        principal,
        devices=devices,
        events=events,
        total=total,
        online_total=online_total,
        query_text=query_text,
        only=only,
        page=page,
        pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        frp_configured=settings.frp.is_configured,
        frp_missing=settings.frp.missing_keys,
        frps_error=frps_error,
        server_info=server_info,
        poll_interval=settings.frp.poll_interval_sec,
    )


@router.get("/{device_id}", response_class=HTMLResponse, include_in_schema=False, response_model=None)
async def fleet_card(
    device_id: int,
    request: Request,
    principal: Principal = Depends(require_section("devices")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    device = await session.scalar(
        select(Device).where(Device.id == device_id).options(selectinload(Device.user))
    )
    if device is None:
        return RedirectResponse("/admin/fleet?err=Роутер+не+найден", status_code=303)

    history = _thin_out(await router_service.metrics_history(session, device_id, hours=24))
    events = await router_service.recent_events(session, device_id=device_id, limit=40)

    # Подписка владельца прямо в карточке роутера: первый вопрос поддержки
    # об умолкшем устройстве — оплачен ли доступ, и ради него не должно
    # приходиться уходить в карточку клиента.
    subscription = None
    plan = None
    if device.user_id is not None:
        subscription = await subscription_service.get_current(session, device.user_id)
        if subscription is not None and subscription.plan_id:
            plan = await session.get(Plan, subscription.plan_id)

    # Учётка ручной активации: без её срока кнопка продления — стрельба вслепую.
    # Жёсткий предел, как и у дашборда frps: страница не должна ждать чужой сервис.
    panel_account = None
    if settings.remnawave.is_configured:
        try:
            panel_account = await asyncio.wait_for(activation.panel_account_of(device), timeout=3)
        except TimeoutError:
            log.warning("fleet.panel_lookup_timeout", device_id=device_id)

    series = {
        "cpu": [(item.created_at, item.cpu_pct or 0) for item in history],
        "ram": [(item.created_at, item.ram_pct or 0) for item in history],
        "clients": [
            (item.created_at, (item.clients_wifi or 0) + (item.clients_dhcp or 0)) for item in history
        ],
    }

    return render(
        request,
        "fleet_card.html",
        principal,
        device=device,
        events=events,
        series=series,
        subscription=subscription,
        plan=plan,
        panel_account=panel_account,
        panel_username=activation.manual_username_for(device.mac),
        panel_expires_at=activation.panel_expiry_of(panel_account),
        has_history=bool(history),
        frp_configured=settings.frp.is_configured,
    )


@router.post("/{device_id}/poll", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def poll_now(
    device_id: int,
    request: Request,
    principal: Principal = Depends(require_section("devices")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    """Снять показания прямо сейчас, не дожидаясь планового опроса."""
    device = await session.get(Device, device_id)
    if device is None:
        return RedirectResponse("/admin/fleet?err=Роутер+не+найден", status_code=303)
    if not device.frp_visitor_port:
        await router_service.ensure_frp_binding(session, device)

    try:
        payload = await RouterApi(device.frp_visitor_port or 0).stats()
    except (FrpError, Exception) as exc:  # noqa: BLE001 — показываем причину оператору
        log.warning("fleet.poll_failed", device_id=device_id, error=str(exc))
        return RedirectResponse(
            f"/admin/fleet/{device_id}?err=Роутер+не+ответил:+{str(exc)[:80]}", status_code=303
        )

    stats = router_service.parse_stats(payload)
    router_service.apply_stats(device, stats)
    router_service.record_metrics(session, device, stats)
    router_service.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="info",
        message="Показания сняты вручную",
        payload={"by": principal.admin.login},
    )
    return RedirectResponse(f"/admin/fleet/{device_id}?ok=Показания+обновлены", status_code=303)


@router.post("/{device_id}/open", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def open_panel(
    device_id: int,
    request: Request,
    principal: Principal = Depends(require_section("console")),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Запоминает роутер в сессии и открывает его веб-панель."""
    device = await session.get(Device, device_id)
    if device is None or not device.frp_visitor_port:
        return RedirectResponse("/admin/fleet?err=Туннель+не+выделен", status_code=303)

    loaded = await load_session(request)
    if loaded is None:
        return RedirectResponse("/admin/login", status_code=303)
    session_id, data = loaded
    data.router_port = device.frp_visitor_port
    data.router_mac = device.mac
    await update_session(session_id, data)

    audit.record(
        session,
        admin_id=principal.admin.id,
        action="fleet.panel_opened",
        entity_type="device",
        entity_id=device.id,
        new={"mac": device.mac},
        request=request,
    )
    await session.commit()
    log.info("fleet.panel_opened", device_id=device.id, admin=principal.admin.login)
    return RedirectResponse("/cgi-bin/luci/", status_code=303)


def _days_from(form: Any, default: int = 30) -> int:
    raw = form_value(form, "days") or str(default)
    try:
        return max(min(int(raw), 3650), 1)
    except ValueError:
        return default


@router.post("/{device_id}/activate", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def manual_activate(
    device_id: int,
    request: Request,
    principal: Principal = Depends(require_section("devices")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    """Выдать роутеру доступ руками: учётка в панели по MAC и доставка ссылки."""
    device = await session.get(Device, device_id)
    if device is None:
        return RedirectResponse("/admin/fleet?err=Роутер+не+найден", status_code=303)

    days = _days_from(await request.form())
    try:
        until = await activation.activate_manually(session, device=device, days=days)
    except activation.ActivationError as exc:
        return RedirectResponse(f"/admin/fleet/{device_id}?err={quote(str(exc)[:160])}", status_code=303)

    audit.record(
        session,
        admin_id=principal.admin.id,
        action="fleet.manual_activate",
        entity_type="device",
        entity_id=device.id,
        new={"days": days, "until": until.isoformat()},
        request=request,
    )
    return RedirectResponse(
        f"/admin/fleet/{device_id}?ok={quote(f'Активирован до {until:%d.%m.%Y}')}", status_code=303
    )


@router.post("/{device_id}/extend", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def manual_extend(
    device_id: int,
    request: Request,
    principal: Principal = Depends(require_section("devices")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    """Продлить срок учётки, заведённой ручной активацией."""
    device = await session.get(Device, device_id)
    if device is None:
        return RedirectResponse("/admin/fleet?err=Роутер+не+найден", status_code=303)

    days = _days_from(await request.form())
    try:
        until = await activation.extend_manually(session, device=device, days=days)
    except activation.ActivationError as exc:
        return RedirectResponse(f"/admin/fleet/{device_id}?err={quote(str(exc)[:160])}", status_code=303)

    audit.record(
        session,
        admin_id=principal.admin.id,
        action="fleet.manual_extend",
        entity_type="device",
        entity_id=device.id,
        new={"days": days, "until": until.isoformat()},
        request=request,
    )
    return RedirectResponse(
        f"/admin/fleet/{device_id}?ok={quote(f'Продлён до {until:%d.%m.%Y}')}", status_code=303
    )


@router.post("/{device_id}/bind", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def bind_client(
    device_id: int,
    request: Request,
    principal: Principal = Depends(require_section("devices")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    """Привязка роутера к клиенту вручную — до самостоятельной активации в боте."""
    device = await session.get(Device, device_id)
    if device is None:
        return RedirectResponse("/admin/fleet?err=Роутер+не+найден", status_code=303)

    form = await request.form()
    query = form_value(form, "client")
    user = await router_service.find_user(session, query)
    if user is None:
        return RedirectResponse(
            f"/admin/fleet/{device_id}?err=Клиент+не+найден:+{query[:40]}", status_code=303
        )

    previous = device.user_id
    if previous and previous != user.id:
        # Один MAC — один аккаунт: молча переклеивать устройство нельзя.
        router_service.add_event(
            session,
            device_id=device.id,
            mac=device.mac,
            level="warning",
            message=f"Устройство переписано с клиента #{previous} на #{user.id}",
            payload={"by": principal.admin.login},
        )

    device.user_id = user.id
    if device.status in (DeviceStatus.NEW, DeviceStatus.REVOKED):
        # REVOKED тоже: отвязанный роутер, привязанный заново, оставался отвязанным.
        # В админке связь появлялась, а клиент видел «роутер не привязан» — кабинет
        # такие устройства прячет. Заблокированный не трогаем: снятие блокировки —
        # отдельное решение, а не побочный эффект привязки.
        device.status = DeviceStatus.ASSIGNED

    router_service.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="info",
        message=f"Привязан клиент {user.display_name}",
        payload={"by": principal.admin.login, "user_id": user.id},
    )
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="fleet.client_bound",
        entity_type="device",
        entity_id=device.id,
        old={"user_id": previous},
        new={"user_id": user.id, "mac": device.mac},
        request=request,
    )
    log.info("fleet.client_bound", device_id=device.id, user_id=user.id)
    return RedirectResponse(
        f"/admin/fleet/{device_id}?ok=Клиент+{user.display_name}+привязан", status_code=303
    )


@router.post("/{device_id}/unbind", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def unbind_client(
    device_id: int,
    request: Request,
    principal: Principal = Depends(require_section("devices")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    device = await session.get(Device, device_id)
    if device is None:
        return RedirectResponse("/admin/fleet?err=Роутер+не+найден", status_code=303)

    previous = device.user_id
    device.user_id = None
    device.status = DeviceStatus.NEW if device.activated_at is None else DeviceStatus.REVOKED
    router_service.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="warning",
        message="Клиент отвязан",
        payload={"by": principal.admin.login},
    )
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="fleet.client_unbound",
        entity_type="device",
        entity_id=device.id,
        old={"user_id": previous},
        new={"user_id": None},
        request=request,
    )
    return RedirectResponse(f"/admin/fleet/{device_id}?ok=Клиент+отвязан", status_code=303)


@router.post("/{device_id}/note", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def save_note(
    device_id: int,
    request: Request,
    principal: Principal = Depends(require_section("devices")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    device = await session.get(Device, device_id)
    if device is None:
        return RedirectResponse("/admin/fleet?err=Роутер+не+найден", status_code=303)
    form = await request.form()
    device.admin_note = form_value(form, "admin_note") or None
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="fleet.note_saved",
        entity_type="device",
        entity_id=device.id,
        request=request,
    )
    return RedirectResponse(f"/admin/fleet/{device_id}?ok=Заметка+сохранена", status_code=303)


@router.get("/events/all", response_class=HTMLResponse, include_in_schema=False)
async def events_page(
    request: Request,
    principal: Principal = Depends(require_section("devices")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    level = request.query_params.get("level", "").strip()
    query = select(DeviceEvent).order_by(DeviceEvent.id.desc()).limit(200)
    if level:
        query = query.where(DeviceEvent.level == level)
    events = list(await session.scalars(query))
    return render(
        request,
        "fleet_events.html",
        principal,
        events=events,
        level=level,
        now=utcnow(),
    )
