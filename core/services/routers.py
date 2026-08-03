"""Учёт роутеров: синхронизация с frps, разбор телеметрии, события.

Данные о состоянии приходят двумя путями и складываются в одну картину:
  * дашборд frps — кто держит туннель прямо сейчас;
  * ответ самого роутера — загрузка, память, температура, клиенты, трафик.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.dates import utcnow
from core.enums import DeviceServiceStatus, DeviceStatus
from core.models import Device, DeviceEvent, Heartbeat
from core.security import normalize_mac
from core.services.frp import FrpProxy, proxy_names_for

log = structlog.get_logger("services.routers")


@dataclass(slots=True)
class RouterStats:
    """Разобранный ответ роутера. Поля необязательные: прошивки разных версий."""

    board: str | None = None
    fw_version: str | None = None
    uptime_sec: int = 0
    load_avg: float | None = None
    cpu_pct: int | None = None
    ram_pct: int | None = None
    temp_c: float | None = None
    wan_ip: str | None = None
    rx_bytes: int = 0
    tx_bytes: int = 0
    clients_wifi: int = 0
    clients_dhcp: int = 0
    service_active: bool = False
    """Работает ли на роутере сервис доступа."""
    tunnel_running: bool = False
    raw: dict[str, Any] | None = None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def parse_stats(payload: dict[str, Any]) -> RouterStats:
    """Приводит ответ прошивки к нашей модели.

    Прошивка отдаёт часть полей в собственной терминологии и вложенными
    объектами; сюда стекается всё, что нам нужно, под нашими именами.
    """
    ram = payload.get("ram") if isinstance(payload.get("ram"), dict) else {}
    network = payload.get("network") if isinstance(payload.get("network"), dict) else {}
    clients = payload.get("clients") if isinstance(payload.get("clients"), dict) else {}

    service_active = payload.get("service_active")
    if service_active is None:
        # Прошивки прошлых версий называют этот флаг иначе — читаем оба варианта.
        service_active = payload.get("vpn_active", False)

    return RouterStats(
        board=str(payload.get("board") or "") or None,
        fw_version=str(payload.get("fw") or payload.get("fw_version") or "") or None,
        uptime_sec=_as_int(payload.get("uptime_sec")),
        load_avg=_as_float(payload.get("load")),
        cpu_pct=_as_int(payload.get("cpu_pct"), 0) if payload.get("cpu_pct") is not None else None,
        ram_pct=_as_int(ram.get("pct")) if ram.get("pct") is not None else None,
        temp_c=_as_float(payload.get("temp_c")),
        wan_ip=str(network.get("wan_ip") or "") or None,
        rx_bytes=_as_int(network.get("rx_bytes")),
        tx_bytes=_as_int(network.get("tx_bytes")),
        clients_wifi=_as_int(clients.get("wifi")),
        clients_dhcp=_as_int(clients.get("dhcp")),
        service_active=bool(service_active),
        tunnel_running=bool(payload.get("frpc_running", False)),
        raw=payload,
    )


def add_event(
    session: AsyncSession,
    *,
    device_id: int | None,
    mac: str | None,
    level: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> DeviceEvent:
    event = DeviceEvent(
        device_id=device_id,
        mac=mac,
        level=level,
        message=message,
        payload=payload or {},
    )
    session.add(event)
    return event


async def allocate_visitor_port(session: AsyncSession) -> int:
    """Следующий свободный порт visitor'а. Порты не переиспользуются в рамках жизни базы."""
    maximum = await session.scalar(select(func.max(Device.frp_visitor_port)))
    base = settings.frp.visitor_base_port
    return max(int(maximum or 0) + 1, base)


async def get_or_create_by_mac(session: AsyncSession, mac: str, *, model: str = "") -> tuple[Device, bool]:
    """Находит устройство по MAC или заводит новое — роутер мог прийти раньше заказа."""
    normalized = normalize_mac(mac)
    if not normalized:
        raise ValueError(f"Некорректный MAC: {mac!r}")

    device = await session.scalar(select(Device).where(Device.mac == normalized))
    if device is not None:
        return device, False

    device = Device(mac=normalized, model=model, status=DeviceStatus.NEW)
    session.add(device)
    await session.flush()
    log.info("router.discovered", mac=normalized, device_id=device.id)
    return device, True


async def ensure_frp_binding(session: AsyncSession, device: Device) -> Device:
    """Проставляет имена прокси и выделяет порт visitor'а."""
    luci_name, ssh_name = proxy_names_for(device.mac)
    device.frp_luci_name = luci_name
    device.frp_ssh_name = ssh_name
    if device.frp_visitor_port is None:
        device.frp_visitor_port = await allocate_visitor_port(session)
        await session.flush()
    return device


def apply_stats(device: Device, stats: RouterStats, *, now: dt.datetime | None = None) -> Device:
    """Переносит снимок в карточку устройства."""
    moment = now or utcnow()
    if stats.board:
        device.board = stats.board[:64]
    if stats.fw_version:
        device.fw_version = stats.fw_version[:32]
    device.uptime_sec = stats.uptime_sec or device.uptime_sec
    device.load_avg = stats.load_avg if stats.load_avg is not None else device.load_avg
    device.cpu_pct = stats.cpu_pct if stats.cpu_pct is not None else device.cpu_pct
    device.ram_pct = stats.ram_pct if stats.ram_pct is not None else device.ram_pct
    device.temp_c = stats.temp_c if stats.temp_c is not None else device.temp_c
    device.clients_wifi = stats.clients_wifi
    device.clients_dhcp = stats.clients_dhcp
    device.tunnel_running = stats.tunnel_running
    if stats.wan_ip:
        device.last_wan_ip = stats.wan_ip[:45]
    if stats.rx_bytes:
        device.rx_bytes = stats.rx_bytes
    if stats.tx_bytes:
        device.tx_bytes = stats.tx_bytes
    device.service_status = (
        DeviceServiceStatus.RUNNING if stats.service_active else DeviceServiceStatus.STOPPED
    )
    device.last_poll_at = moment
    device.last_heartbeat_at = moment
    return device


def record_metrics(session: AsyncSession, device: Device, stats: RouterStats) -> Heartbeat:
    """Точка в историю метрик — из неё строятся графики в админке."""
    entry = Heartbeat(
        device_id=device.id,
        uptime_sec=stats.uptime_sec,
        fw_version=(stats.fw_version or "")[:32],
        panel_version=device.panel_version,
        wan_ip=stats.wan_ip,
        service_status=device.service_status,
        rx_bytes=stats.rx_bytes,
        tx_bytes=stats.tx_bytes,
        load_avg=stats.load_avg,
        cpu_pct=stats.cpu_pct,
        ram_pct=stats.ram_pct,
        temp_c=stats.temp_c,
        clients_wifi=stats.clients_wifi,
        clients_dhcp=stats.clients_dhcp,
        source="poll",
    )
    session.add(entry)
    return entry


async def mark_online(
    session: AsyncSession, device: Device, proxy: FrpProxy, *, now: dt.datetime | None = None
) -> bool:
    """Отмечает, что туннель поднят. True — если устройство только что вернулось."""
    moment = now or utcnow()
    came_back = not device.frp_online
    device.frp_online = True
    device.frp_last_seen_at = moment
    if came_back:
        add_event(
            session,
            device_id=device.id,
            mac=device.mac,
            level="success",
            message="Роутер на связи",
            payload={"proxy": proxy.name},
        )
        log.info("router.online", mac=device.mac)
    return came_back


async def mark_offline(session: AsyncSession, device: Device) -> bool:
    """Туннеля больше нет. True — если это изменение состояния."""
    if not device.frp_online:
        return False
    device.frp_online = False
    device.service_status = DeviceServiceStatus.UNKNOWN
    add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="error",
        message="Роутер отключился",
    )
    log.info("router.offline", mac=device.mac)
    return True


async def recent_events(
    session: AsyncSession, *, device_id: int | None = None, limit: int = 50
) -> list[DeviceEvent]:
    query = select(DeviceEvent).order_by(DeviceEvent.id.desc()).limit(limit)
    if device_id is not None:
        query = query.where(DeviceEvent.device_id == device_id)
    return list(await session.scalars(query))


async def metrics_history(
    session: AsyncSession, device_id: int, *, hours: int = 24, limit: int = 300
) -> list[Heartbeat]:
    since = utcnow() - dt.timedelta(hours=hours)
    rows = await session.scalars(
        select(Heartbeat)
        .where(Heartbeat.device_id == device_id, Heartbeat.created_at >= since)
        .order_by(Heartbeat.created_at.desc())
        .limit(limit)
    )
    return list(reversed(list(rows)))
