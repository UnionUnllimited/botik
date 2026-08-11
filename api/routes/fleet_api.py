"""Парк роутеров наружу — для вкладки «Роутеры» в админке бота.

Бот и его админка живут отдельным процессом на хосте, со своим venv и без
драйвера Postgres, а наша база наружу не опубликована. Поэтому данные отдаются
по HTTP, а не запросом в базу: это единственный способ, которым тот процесс
вообще может нас спросить.

Действия — опрос, активация, продление, привязка клиента — тоже здесь, а не
у них: туннель к роутеру держит наш контейнер `frpc`, и дотянуться до него
может только процесс в нашей сети. Их админка эти ручки вызывает.

Консоль тоже здесь: у нас она разовыми командами, а не сессией, поэтому
переносится обычной ручкой. Не переехало только проксирование панели LuCI —
там переписывание ссылок и куки, это отдельный слой.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.deps import get_session, get_transaction
from api.service_auth import require_token
from core.config import settings
from core.dates import utcnow
from core.enums import DeviceStatus
from core.models import Device
from core.services import activation, panel_ticket, router_shell
from core.services import routers as routers_service
from core.services import subscriptions as subscription_service
from core.services.frp import RouterApi

log = structlog.get_logger("api.fleet")

router = APIRouter(prefix="/api/v1/fleet", tags=["fleet"], include_in_schema=False)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


@router.get("/routers", dependencies=[Depends(require_token)])
async def list_routers(session: AsyncSession = Depends(get_session)) -> dict:
    """Список роутеров с показаниями и владельцем."""
    now = utcnow()
    devices = list(
        await session.scalars(
            select(Device).options(selectinload(Device.user)).order_by(Device.id.desc())
        )
    )

    items = []
    for device in devices:
        online = device.frp_online or device.is_online(
            threshold_min=settings.subscription.heartbeat_offline_min, now=now
        )
        seen = (device.last_heartbeat_at, device.last_poll_at, device.frp_last_seen_at)
        subscription = (
            await subscription_service.get_current(session, device.user_id) if device.user_id else None
        )
        items.append(
            {
                "id": device.id,
                "mac": device.mac,
                "model": device.model or "",
                "status": str(device.status),
                "online": online,
                "last_seen": _iso(max((value for value in seen if value), default=None)),
                "activated_at": _iso(device.activated_at),
                "wan_ip": device.last_wan_ip or "",
                "clients": (device.clients_wifi or 0) + (device.clients_dhcp or 0),
                "cpu_pct": device.cpu_pct,
                "ram_pct": device.ram_pct,
                "rx_bytes": device.rx_bytes or 0,
                "tx_bytes": device.tx_bytes or 0,
                "client": device.user.display_name if device.user else "",
                "client_id": device.user_id,
                "subscription_status": str(subscription.status) if subscription else "",
                "subscription_until": _iso(subscription.expires_at) if subscription else None,
                "subscription_here": bool(subscription and subscription.device_id == device.id),
            }
        )

    online_total = sum(1 for item in items if item["online"])
    return {
        "generated_at": now.isoformat(),
        "total": len(items),
        "online": online_total,
        # Ссылка на нашу админку: действия с роутером живут там, и вкладке
        # в чужой админке нужно куда-то отправить человека.
        "admin_url": f"{settings.api.admin_base_url.rstrip('/')}/admin/fleet",
        "routers": items,
    }


def _device_payload(device, *, now):
    seen = (device.last_heartbeat_at, device.last_poll_at, device.frp_last_seen_at)
    return {
        "id": device.id,
        "mac": device.mac,
        "model": device.model or "",
        "fw_version": device.fw_version or "",
        "status": str(device.status),
        "online": device.frp_online
        or device.is_online(threshold_min=settings.subscription.heartbeat_offline_min, now=now),
        "last_seen": _iso(max((value for value in seen if value), default=None)),
        "activated_at": _iso(device.activated_at),
        "wan_ip": device.last_wan_ip or "",
        "uptime_sec": device.uptime_sec or 0,
        "clients": (device.clients_wifi or 0) + (device.clients_dhcp or 0),
        "cpu_pct": device.cpu_pct,
        "ram_pct": device.ram_pct,
        "rx_bytes": device.rx_bytes or 0,
        "tx_bytes": device.tx_bytes or 0,
        "visitor_port": device.frp_visitor_port,
    }


@router.get("/routers/{device_id}", dependencies=[Depends(require_token)])
async def router_card(device_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Карточка роутера: показания, владелец, подписка, журнал и срок в панели."""
    device = await session.scalar(
        select(Device).where(Device.id == device_id).options(selectinload(Device.user))
    )
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")

    now = utcnow()
    subscription = (
        await subscription_service.get_current(session, device.user_id) if device.user_id else None
    )
    events = await routers_service.recent_events(session, device_id=device_id, limit=30)

    # Срок ручной активации знает только панель. Предел по времени тот же, что был
    # в нашей карточке: страница не должна ждать чужой сервис.
    panel_expires_at = None
    if settings.remnawave.is_configured:
        try:
            account = await asyncio.wait_for(activation.panel_account_of(device), timeout=3)
            panel_expires_at = activation.panel_expiry_of(account)
        except TimeoutError:
            log.warning("fleet.panel_timeout", device_id=device_id)

    return {
        "router": _device_payload(device, now=now),
        "client": {
            "id": device.user_id,
            "name": device.user.display_name if device.user else "",
            "email": (device.user.email or "") if device.user else "",
            "phone": (device.user.phone or "") if device.user else "",
        },
        "subscription": {
            "status": str(subscription.status) if subscription else "",
            "until": _iso(subscription.expires_at) if subscription else None,
            "here": bool(subscription and subscription.device_id == device.id),
        },
        "panel": {
            "username": activation.manual_username_for(device.mac),
            "until": _iso(panel_expires_at),
            "active": bool(panel_expires_at and panel_expires_at > now),
        },
        "events": [
            {"at": _iso(event.created_at), "level": event.level, "message": event.message}
            for event in events
        ],
    }


async def _device_or_404(session: AsyncSession, device_id: int) -> Device:
    device = await session.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return device


def _days(payload: dict) -> int:
    try:
        return max(min(int(payload.get("days", 30)), 3650), 1)
    except (TypeError, ValueError):
        return 30


@router.post("/routers/{device_id}/poll", dependencies=[Depends(require_token)])
async def poll_router(device_id: int, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Снять показания прямо сейчас. Туннель к роутеру есть только у нас."""
    device = await _device_or_404(session, device_id)
    if not device.frp_visitor_port:
        await routers_service.ensure_frp_binding(session, device)

    try:
        payload = await RouterApi(device.frp_visitor_port or 0).stats()
    except Exception as exc:  # noqa: BLE001 — причину показываем оператору как есть
        log.warning("fleet.poll_failed", device_id=device_id, error=str(exc))
        return {"ok": False, "error": "Роутер не ответил: " + str(exc)[:160]}

    stats = routers_service.parse_stats(payload)
    routers_service.apply_stats(device, stats)
    routers_service.record_metrics(session, device, stats)
    return {"ok": True, "router": _device_payload(device, now=utcnow())}


@router.post("/routers/{device_id}/activate", dependencies=[Depends(require_token)])
async def activate_router(
    device_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Ручная активация: учётка в панели по MAC и доставка ссылки роутеру."""
    device = await _device_or_404(session, device_id)
    try:
        until = await activation.activate_manually(session, device=device, days=_days(payload))
    except activation.ActivationError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "until": until.isoformat()}


@router.post("/routers/{device_id}/extend", dependencies=[Depends(require_token)])
async def extend_router(
    device_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    device = await _device_or_404(session, device_id)
    try:
        until = await activation.extend_manually(session, device=device, days=_days(payload))
    except activation.ActivationError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "until": until.isoformat()}


@router.post("/routers/{device_id}/bind", dependencies=[Depends(require_token)])
async def bind_router(
    device_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Привязка клиента по почте, телефону, @username или id."""
    device = await _device_or_404(session, device_id)
    query = str(payload.get("client", "")).strip()
    user = await routers_service.find_user(session, query) if query else None
    if user is None:
        return {"ok": False, "error": "Клиент не найден: " + query[:60]}

    device.user_id = user.id
    if device.status in (DeviceStatus.NEW, DeviceStatus.REVOKED):
        # REVOKED тоже: иначе заново привязанный роутер остаётся отвязанным
        # и кабинет клиента его прячет.
        device.status = DeviceStatus.ASSIGNED
    routers_service.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="info",
        message="Привязан клиент " + user.display_name,
    )
    return {"ok": True, "client": user.display_name}


@router.post("/routers/{device_id}/unbind", dependencies=[Depends(require_token)])
async def unbind_router(device_id: int, session: AsyncSession = Depends(get_transaction)) -> dict:
    device = await _device_or_404(session, device_id)
    device.user_id = None
    device.status = DeviceStatus.NEW if device.activated_at is None else DeviceStatus.REVOKED
    routers_service.add_event(
        session, device_id=device.id, mac=device.mac, level="warning", message="Клиент отвязан"
    )
    return {"ok": True}


@router.post("/routers/{device_id}/panel-ticket", dependencies=[Depends(require_token)])
async def panel_ticket_for(
    device_id: int, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Разовая ссылка на веб-панель роутера.

    Саму панель отдаём мы: туннель держит наш контейнер `frpc`, и проксировать
    её из их процесса физически нечем. Переносится не прокси, а вход — по этой
    ссылке оператор попадает на панель без входа в нашу админку.
    """
    device = await _device_or_404(session, device_id)
    if not device.frp_visitor_port:
        await routers_service.ensure_frp_binding(session, device)
    if not device.frp_visitor_port:
        return {"ok": False, "error": "Туннель к роутеру не выделен."}

    ticket = await panel_ticket.issue(
        device_id=device.id, port=device.frp_visitor_port, mac=device.mac
    )
    routers_service.add_event(
        session, device_id=device.id, mac=device.mac, level="info", message="Открыта веб-панель"
    )
    base = settings.api.admin_base_url.rstrip("/")
    return {"ok": True, "url": f"{base}/panel/open?ticket={ticket}"}


FORBIDDEN_COMMANDS = ("mkfs", "firstboot", "rm -rf /", "> /dev/mtd", "dd if=", "sysupgrade")
"""Перепрошивка и форматирование из веб-консоли — верный способ получить кирпич
у клиента на другом конце страны. Список тот же, что был в нашей админке."""


@router.post("/routers/{device_id}/console", dependencies=[Depends(require_token)])
async def console(
    device_id: int, payload: dict, session: AsyncSession = Depends(get_session)
) -> dict:
    """Разовая команда на роутере по SSH через туннель.

    Именно разовая, а не сессия: интерактивной консоли у нас никогда не было,
    и делать её ради переезда незачем. Читать журналы можно тем же способом —
    `logread`, `dmesg`.
    """
    device = await _device_or_404(session, device_id)
    command = str(payload.get("command", "")).strip()
    if not command:
        return {"ok": False, "error": "Пустая команда."}
    if any(bad in command.lower() for bad in FORBIDDEN_COMMANDS):
        log.warning("fleet.console_forbidden", device_id=device_id, command=command[:120])
        return {"ok": False, "error": "Эта команда запрещена из веб-консоли."}

    try:
        result = await router_shell.run(device, command)
    except router_shell.ShellError as exc:
        return {"ok": False, "error": str(exc)}

    routers_service.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="info",
        message="Консоль: " + command[:120],
    )
    await session.commit()
    return {"ok": result.ok, "output": result.output[:20000], "command": command}
