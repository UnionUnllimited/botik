"""Опрос роутеров через frp.

Роутер наружу недоступен: он сам держит связь с frps, а мы ходим к нему
обратно через туннель. Отсюда два разных круга, и путать их дорого.

**Присутствие** — один запрос к дашборду frps: кто сейчас зарегистрирован.
До роутеров не доходит вовсе, стоит копейки, поэтому идёт часто. Отсюда же
«на связи» в админке и автоактивация отгруженного заказа: роутер вышел на
связь — подписка включилась, и ждать этого полчаса нельзя.

**Показания** — CPU, память, аптайм, трафик. Каждое означает настоящее
соединение до роутера через туннель, и раз в минуту по всему парку это
постоянный поток к каждому клиенту домой ради чисел, которые никто не
смотрит чаще, чем раз в полчаса. Поэтому реже и отдельным кругом.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import structlog
from sqlalchemy import delete, select

from core.config import settings
from core.db import session_scope
from core.models import Device, DeviceEvent, Heartbeat
from core.services import activation
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
    """Кто на связи: спрашиваем у frps, до самих роутеров не ходим.

    Здесь же автоактивация: отгруженный роутер вышел на связь — подписка
    включается сразу, а не на следующем круге показаний через полчаса.
    """
    if not settings.frp.is_configured:
        log.info("routers.frp_not_configured", missing=settings.frp.missing_keys)
        return 0

    try:
        online = await dashboard().online_routers()
    except Exception as exc:  # noqa: BLE001 — frps недоступен, попробуем в следующий раз
        log.warning("routers.frps_unavailable", error=str(exc))
        return 0

    candidates: list[int] = []
    async with session_scope() as session:
        # 1. Отмечаем тех, кто на связи, и заводим незнакомых.
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

            # Кандидатов на автоактивацию только запоминаем. Условия проверит
            # сама `auto_activate_if_shipped`, здесь достаточно дешёвого отсева
            # по полям, которые уже в памяти.
            if device.activated_at is None and device.order_id is not None:
                candidates.append(device.id)

        # 2. Кто пропал из списка — офлайн.
        known = list(await session.scalars(select(Device).where(Device.frp_online.is_(True))))
        for device in known:
            if device.mac not in online:
                await router_service.mark_offline(session, device)

        await session.flush()

    # Отгруженный заказ вышел на связь — значит роутер уже у клиента, и подписку
    # можно отдать без участия оператора. Всё, что не отгружено (в том числе
    # роутер на столе у мастера), внутрь не пройдёт.
    #
    # Каждая активация — своя короткая транзакция, и не внутри обхода: она ходит
    # в панель и по SSH к роутеру домой к клиенту. Раньше это держало транзакцию
    # присутствия всего парка, и одна задумавшаяся панель растягивала её на весь
    # круг. Теперь отметки связи уже сохранены, а зависшая активация задерживает
    # только свой роутер.
    for device_id in candidates:
        async with session_scope() as session:
            device = await session.get(Device, device_id)
            if device is None:
                continue
            try:
                await activation.auto_activate_if_shipped(session, device)
            except Exception as exc:  # noqa: BLE001 — один роутер не должен ронять обход
                log.warning("routers.auto_activation_failed", mac=device.mac, error=str(exc))

    log.info("routers.presence_synced", online=len(online), activations_tried=len(candidates))
    return len(online)


async def poll_router_stats() -> int:
    """Снимает показания с тех, кто на связи.

    Отдельно от присутствия и заметно реже: каждое снятие — соединение до
    роутера домой к клиенту, а CPU и аптайм никто не смотрит чаще, чем раз
    в полчаса. Между кругами в таблице показания прошлого круга, и это
    честнее постоянного стука в дверь.
    """
    if not settings.frp.is_configured:
        return 0

    polled = 0
    async with session_scope() as session:
        targets = [
            (device.id, device.frp_visitor_port)
            for device in await session.scalars(
                select(Device).where(
                    Device.frp_online.is_(True), Device.frp_visitor_port.is_not(None)
                )
            )
        ]

    # Обход — вне транзакции. Один роутер занимает до шестнадцати секунд:
    # клиент сначала пробует HTTP, потом HTTPS, по восемь на попытку. При
    # восьми параллельных и сотне устройств молчащий парк растягивал круг на
    # три минуты, и всё это время висела открытая транзакция на пустом месте:
    # писать по итогам обхода — работа на секунды.
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def guarded(device_id: int, port: int):
        async with semaphore:
            return await _poll_one(device_id, port)

    results = await asyncio.gather(*(guarded(*target) for target in targets))

    async with session_scope() as session:
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

    log.info("routers.stats_polled", asked=len(targets), polled=polled)
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


EVENTS_RETENTION_DAYS = 60
"""Дольше журнал устройств не держим.

Он пишется на каждую команду, каждую привязку и каждый показ пароля, а читают
его в пределах последних дней: «что было с этим роутером вчера». Без чистки
таблица растёт вечно и страница журнала превращается в свалку."""


async def cleanup_device_events() -> int:
    """Убирает старые записи журнала устройств."""
    threshold = dt.datetime.now(dt.UTC) - dt.timedelta(days=EVENTS_RETENTION_DAYS)
    async with session_scope() as session:
        result = await session.execute(
            delete(DeviceEvent).where(DeviceEvent.created_at < threshold)
        )
    count = result.rowcount or 0
    if count:
        log.info("routers.events_cleaned", count=count, older_than_days=EVENTS_RETENTION_DAYS)
    return count
