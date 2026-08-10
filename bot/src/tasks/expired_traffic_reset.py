"""Сброс суточного трафика (total_bytes) у клиентов с истёкшей подпиской."""

from __future__ import annotations

import asyncio
import os

from loguru import logger

import db_helpers

CHECK_INTERVAL_SECONDS = int(os.getenv('EXPIRED_TRAFFIC_RESET_INTERVAL', str(4 * 3600)))


async def reset_expired_users_daily_traffic() -> int:
    """Обнуляет total_bytes у пользователей с subscription_end_date в прошлом."""
    async with db_helpers.get_db_connection_safe() as db:
        async with db.execute('PRAGMA table_info(users)') as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if 'total_bytes' not in cols:
            return 0

        cur = await db.execute(
            """
            UPDATE users
            SET total_bytes = 0
            WHERE subscription_end_date IS NOT NULL
              AND datetime(subscription_end_date) <= datetime('now', 'utc')
              AND COALESCE(total_bytes, 0) != 0
            """
        )
        updated = int(cur.rowcount or 0)
        if updated:
            await db.commit()
        return updated


async def expired_traffic_reset_loop(
    interval_seconds: int = CHECK_INTERVAL_SECONDS,
) -> None:
    while True:
        try:
            updated = await reset_expired_users_daily_traffic()
            if updated:
                logger.info(
                    f"[EXPIRED_TRAFFIC_RESET] Сброшен суточный трафик у {updated} истёкших клиентов"
                )
        except Exception as e:
            logger.error(f"[EXPIRED_TRAFFIC_RESET] Ошибка: {e}", exc_info=True)

        await asyncio.sleep(interval_seconds)


def start_expired_traffic_reset_task() -> None:
    """Запускает фоновую задачу. Вызывать после старта event loop."""
    asyncio.create_task(expired_traffic_reset_loop())
