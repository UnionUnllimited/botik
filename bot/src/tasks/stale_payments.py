"""Авто-отмена зависших платежей (pending / processing старше 24 ч)."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import MutableMapping, Optional

from loguru import logger

import db_helpers

STALE_PAYMENT_HOURS = float(os.getenv('STALE_PAYMENT_HOURS', '24'))
CHECK_INTERVAL_SECONDS = int(os.getenv('STALE_PAYMENT_CHECK_INTERVAL', '3600'))
BATCH_LIMIT = int(os.getenv('STALE_PAYMENT_BATCH_LIMIT', '500'))

_STALE_STATUSES = ('pending', 'processing')


def _stop_payment_checker(
    payment_id: str,
    active_checkers: Optional[MutableMapping[str, asyncio.Task]],
) -> None:
    if not active_checkers:
        return
    task = active_checkers.pop(payment_id, None)
    if task and not task.done():
        task.cancel()


async def cancel_stale_payments(
    max_age_hours: float = STALE_PAYMENT_HOURS,
    batch_limit: int = BATCH_LIMIT,
) -> list[str]:
    """Помечает зависшие платежи как canceled. Возвращает список payment_id."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    cutoff_iso = cutoff.isoformat()
    canceled: list[str] = []

    async with db_helpers.get_db_connection_safe() as db:
        placeholders = ','.join('?' for _ in _STALE_STATUSES)
        async with db.execute(
            f"""
            SELECT payment_id
            FROM payments
            WHERE status IN ({placeholders})
              AND created_at IS NOT NULL
              AND created_at <= ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (*_STALE_STATUSES, cutoff_iso, batch_limit),
        ) as cursor:
            rows = await cursor.fetchall()

        payment_ids = [str(r[0]) for r in rows]
        if not payment_ids:
            return canceled

        id_placeholders = ','.join('?' for _ in payment_ids)
        cur = await db.execute(
            f"""
            UPDATE payments
            SET status = 'canceled'
            WHERE payment_id IN ({id_placeholders})
              AND status IN ({placeholders})
            """,
            (*payment_ids, *_STALE_STATUSES),
        )
        if cur.rowcount:
            canceled = payment_ids
            await db.commit()

    return canceled


async def stale_payments_loop(
    active_checkers: Optional[MutableMapping[str, asyncio.Task]] = None,
    max_age_hours: float = STALE_PAYMENT_HOURS,
    interval_seconds: int = CHECK_INTERVAL_SECONDS,
) -> None:
    while True:
        try:
            canceled = await cancel_stale_payments(max_age_hours=max_age_hours)
            if canceled:
                for payment_id in canceled:
                    _stop_payment_checker(payment_id, active_checkers)
                logger.info(
                    f"[STALE_PAYMENTS] Отменено {len(canceled)} платежей "
                    f"(старше {max_age_hours:g} ч): {', '.join(canceled[:5])}"
                    f"{'…' if len(canceled) > 5 else ''}"
                )
        except Exception as e:
            logger.error(f"[STALE_PAYMENTS] Ошибка авто-отмены: {e}", exc_info=True)

        await asyncio.sleep(interval_seconds)


def start_stale_payments_task(
    active_checkers: Optional[MutableMapping[str, asyncio.Task]] = None,
) -> None:
    """Запускает фоновую задачу. Вызывать после старта event loop."""
    asyncio.create_task(stale_payments_loop(active_checkers=active_checkers))
