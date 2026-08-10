"""
Чтение трафика Remnawave из ``remnawave.db`` (веб-админка).
"""
import os
from typing import Any, Dict, List

import aiosqlite
from loguru import logger

from config import migrate_remnawave_db_if_needed


async def get_remnawave_traffic_by_telegram(telegram_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """Трафик Remnawave по telegram_id из ``remnawave.db``.

    Returns:
        {telegram_id: {'total_traffic': <всё время, байт>, 'daily_traffic': <сутки, байт>}}
    """
    if not telegram_ids:
        return {}

    ids = [int(t) for t in telegram_ids if t is not None]
    if not ids:
        return {}

    result: Dict[int, Dict[str, Any]] = {}
    try:
        db_path = migrate_remnawave_db_if_needed()
        if not os.path.isfile(db_path):
            return {}
        conn = await aiosqlite.connect(db_path, timeout=10)
        try:
            await conn.execute("PRAGMA busy_timeout=15000;")
            conn.row_factory = aiosqlite.Row
            async with conn.execute("PRAGMA table_info(user_traffic)") as cur:
                rw_cols = {row[1] for row in await cur.fetchall()}
            has_online = 'online_at' in rw_cols
            select_cols = "telegram_id, total_bytes, total_total_bytes"
            if has_online:
                select_cols += ", online_at"
            batch_size = 900
            for i in range(0, len(ids), batch_size):
                batch = ids[i:i + batch_size]
                placeholders = ','.join('?' * len(batch))
                async with conn.execute(f"""
                    SELECT {select_cols}
                    FROM user_traffic
                    WHERE telegram_id IN ({placeholders})
                """, batch) as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        tg = row['telegram_id']
                        if tg is None:
                            continue
                        result[int(tg)] = {
                            'total_traffic': row['total_total_bytes'] or 0,
                            'daily_traffic': row['total_bytes'] or 0,
                        }
                        if 'online_at' in row.keys() and row['online_at']:
                            result[int(tg)]['online_at'] = row['online_at']
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"Ошибка чтения remnawave.db: {e}")
    return result
