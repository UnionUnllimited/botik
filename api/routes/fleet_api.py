"""Парк роутеров наружу — для вкладки «Роутеры» в админке бота.

Бот и его админка живут отдельным процессом на хосте, со своим venv и без
драйвера Postgres, а наша база наружу не опубликована. Поэтому данные отдаются
по HTTP, а не запросом в базу: это единственный способ, которым тот процесс
вообще может нас спросить.

Ручка только на чтение. Всё, что трогает роутеры — опрос, консоль, панель LuCI —
остаётся в нашей админке: туннели держит наш контейнер `frpc`, и снаружи
их всё равно не достать.
"""

from __future__ import annotations

import datetime as dt
import secrets

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.deps import get_session
from core.config import settings
from core.dates import utcnow
from core.models import Device
from core.services import subscriptions as subscription_service

log = structlog.get_logger("api.fleet")

router = APIRouter(prefix="/api/v1/fleet", tags=["fleet"], include_in_schema=False)


async def require_token(authorization: str = Header(default="")) -> None:
    """Общий секрет вместо сессии: на том конце процесс, а не человек в браузере."""
    expected = settings.api.fleet_token.get_secret_value()
    if not expected:
        # Токен не задан — ручки как будто нет. Иначе выключенная возможность
        # молча раздавала бы список устройств всем.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")

    presented = authorization.removeprefix("Bearer ").strip()
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad_token")


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
