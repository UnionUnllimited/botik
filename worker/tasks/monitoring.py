"""Ежедневная сводка оператору: что в парке требует внимания.

Раз в сутки, а не по событию: алерт на каждое происшествие требует помнить,
о чём уже сообщали, иначе один молчащий роутер шлёт сообщение каждый круг.
Сводка описывает состояние и такой памяти не требует.
"""

from __future__ import annotations

import datetime as dt

import structlog

from core import texts as ru
from core.dates import to_display, utcnow
from core.db import session_scope
from core.notifications import notify_admins
from core.services import monitoring

log = structlog.get_logger("worker.monitoring")


def _msk(value: dt.datetime | None) -> str:
    """Дата для человека — в Москве: оператор живёт в ней, а не в UTC."""
    if value is None:
        return "—"
    return to_display(value).strftime("%d.%m в %H:%M")


async def daily_digest() -> int:
    """Собирает сводку и шлёт оператору. Возвращает число поводов.

    Ноль — значит не отправляли ничего. Ежедневное «всё в порядке» читают
    неделю, а потом перестают читать и всё остальное.
    """
    now = utcnow()
    async with session_scope() as session:
        digest = await monitoring.collect(session, now=now)
        if digest.is_empty:
            log.info("monitoring.nothing_to_report")
            return 0

        silent = [
            (
                device.mac,
                device.user.display_name if device.user else "",
                _msk(
                    max(
                        (
                            value
                            for value in (
                                device.last_heartbeat_at,
                                device.last_poll_at,
                                device.frp_last_seen_at,
                            )
                            if value
                        ),
                        default=None,
                    )
                ),
            )
            for device in digest.silent
        ]
        shipped = [
            (order.public_number, device.mac, max(0, (now - order.shipped_at).days))
            for order, device in digest.shipped_silent
        ]
        expiring = [
            (
                user.display_name,
                subscription.expires_at.strftime("%d.%m"),
                max(0, (subscription.expires_at - now).days),
            )
            for subscription, user in digest.expiring
        ]

        await notify_admins(
            ru.fleet_digest(silent=silent, shipped_silent=shipped, expiring=expiring),
            session=session,
        )
        total = len(silent) + len(shipped) + len(expiring)
        log.info("monitoring.digest_sent", total=total)
        return total
