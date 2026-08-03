"""Опрос роутеров через frp.

Раз в минуту спрашиваем у frps, кто на связи, и для каждого снимаем показания
через его же туннель. Роутер при этом остаётся недоступен снаружи: наружу
он ходит сам.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import structlog
from sqlalchemy import delete, select

from core.config import settings
from core.db import session_scope
from core.models import Device, Heartbeat
from core.services import routers as router_service
from core.services.frp import FrpError, RouterApi, dashboard

log = structlog.get_logger("worker.routers")

CONCURRENCY = 8


async def _poll_one(device_id: int, port: int) -> tuple[int, dict | None, str | None]:
    """Опрашивает один роутер. Ошибка не должна ронять весь цикл."""
    try:
        payload = await RouterApi(port).stats()
    except (FrpError, Exception) as exc:  # noqa: BLE001 — один роутер не мешает остальным
        return device_id, None, str(exc)[:200]
    return device_id, payload, None


async def sync_routers() -> int:
    """Синхронизирует статусы с frps и собирает телеметрию."""
    if not settings.frp.is_configured:
        log.info("routers.frp_not_configured", missing=settings.frp.missing_keys)
        return 0

    try:
        online = await dashboard().online_routers()
    except Exception as exc:  # noqa: BLE001 — frps недоступен, попробуем в следующий раз
        log.warning("routers.frps_unavailable", error=str(exc))
        return 0

    polled = 0
    async with session_scope() as session:
        # 1. Отмечаем тех, кто на связи, и заводим незнакомых.
        targets: list[tuple[int, int]] = []
        for mac, proxy in online.items():
            device, created = await router_service.get_or_create_by_mac(session, mac)
            if created:
                router_service.add_event(
                    session,
                    device_id=device.id,
                    mac=device.mac,
                    level="info",
                    message="Роутер подключился впервые",
                    payload={"proxy": proxy.name},
                )
            await router_service.ensure_frp_binding(session, device)
            await router_service.mark_online(session, device, proxy)
            if device.frp_visitor_port:
                targets.append((device.id, device.frp_visitor_port))

        # 2. Кто пропал из списка — офлайн.
        known = list(await session.scalars(select(Device).where(Device.frp_online.is_(True))))
        for device in known:
            if device.mac not in online:
                await router_service.mark_offline(session, device)

        await session.flush()

        # 3. Снимаем показания параллельно, но не заваливая туннели.
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def guarded(device_id: int, port: int):
            async with semaphore:
                return await _poll_one(device_id, port)

        results = await asyncio.gather(*(guarded(*target) for target in targets))

        for device_id, payload, error in results:
            device = await session.get(Device, device_id)
            if device is None:
                continue
            if payload is None:
                log.debug("routers.poll_failed", device_id=device_id, error=error)
                continue
            stats = router_service.parse_stats(payload)
            router_service.apply_stats(device, stats)
            router_service.record_metrics(session, device, stats)
            polled += 1

    log.info("routers.synced", online=len(online), polled=polled)
    return polled


async def cleanup_router_metrics() -> int:
    """История метрик нужна для графиков за пару недель, не дольше."""
    threshold = dt.datetime.now(dt.UTC) - dt.timedelta(days=settings.frp.metrics_retention_days)
    async with session_scope() as session:
        result = await session.execute(
            delete(Heartbeat).where(Heartbeat.created_at < threshold, Heartbeat.source == "poll")
        )
    count = result.rowcount or 0
    if count:
        log.info("routers.metrics_cleaned", count=count)
    return count
