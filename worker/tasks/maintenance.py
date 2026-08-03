"""Регулярное обслуживание: оффлайн-устройства, чистка телеметрии, устаревшие токены."""

from __future__ import annotations

import datetime as dt

import structlog
from sqlalchemy import delete, func, select, update

from core.config import settings
from core.db import session_scope
from core.enums import CommandStatus, DeviceServiceStatus
from core.models import Device, DeviceCommand, Heartbeat, SubscriptionAccessLog

log = structlog.get_logger("worker.maintenance")


async def mark_offline_devices() -> int:
    """Устройства без heartbeat дольше порога считаем не в сети."""
    threshold = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=settings.subscription.heartbeat_offline_min)
    async with session_scope() as session:
        result = await session.execute(
            update(Device)
            .where(
                Device.last_heartbeat_at.is_not(None),
                Device.last_heartbeat_at < threshold,
                Device.service_status != DeviceServiceStatus.UNKNOWN,
            )
            .values(service_status=DeviceServiceStatus.UNKNOWN)
        )
    count = result.rowcount or 0
    if count:
        log.info("devices.marked_offline", count=count)
    return count


async def cleanup_heartbeats() -> int:
    """Телеметрия старше SUBSCRIPTION_HEARTBEAT_RETENTION_DAYS не нужна."""
    threshold = dt.datetime.now(dt.UTC) - dt.timedelta(days=settings.subscription.heartbeat_retention_days)
    async with session_scope() as session:
        result = await session.execute(delete(Heartbeat).where(Heartbeat.created_at < threshold))
    count = result.rowcount or 0
    if count:
        log.info("heartbeats.cleaned", count=count)
    return count


async def cleanup_access_log() -> int:
    threshold = dt.datetime.now(dt.UTC) - dt.timedelta(days=settings.subscription.access_log_retention_days)
    async with session_scope() as session:
        result = await session.execute(
            delete(SubscriptionAccessLog).where(SubscriptionAccessLog.created_at < threshold)
        )
    count = result.rowcount or 0
    if count:
        log.info("access_log.cleaned", count=count)
    return count


async def expire_stale_commands() -> int:
    """Команды, которые устройство не забрало до дедлайна, помечаем истёкшими."""
    now = dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        result = await session.execute(
            update(DeviceCommand)
            .where(
                DeviceCommand.status.in_([CommandStatus.PENDING, CommandStatus.SENT]),
                DeviceCommand.expires_at.is_not(None),
                DeviceCommand.expires_at < now,
            )
            .values(status=CommandStatus.EXPIRED)
        )
    count = result.rowcount or 0
    if count:
        log.info("commands.expired", count=count)
    return count


async def clear_expired_prev_tokens() -> int:
    """Старый токен подписки живёт ограниченное время после ротации."""
    now = dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        result = await session.execute(
            update(Device)
            .where(
                Device.prev_sub_token_hash.is_not(None),
                Device.prev_sub_token_expires_at.is_not(None),
                Device.prev_sub_token_expires_at < now,
            )
            .values(prev_sub_token_hash=None, prev_sub_token_expires_at=None)
        )
    count = result.rowcount or 0
    if count:
        log.info("devices.prev_tokens_cleared", count=count)
    return count


async def log_fleet_summary() -> dict[str, int]:
    """Короткая сводка в логи раз в час — удобно для алертов по логам."""
    online_since = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=settings.subscription.heartbeat_offline_min)
    async with session_scope() as session:
        total = await session.scalar(select(func.count()).select_from(Device)) or 0
        online = (
            await session.scalar(
                select(func.count()).select_from(Device).where(Device.last_heartbeat_at >= online_since)
            )
            or 0
        )
    summary = {"devices_total": total, "devices_online": online}
    log.info("fleet.summary", **summary)
    return summary
