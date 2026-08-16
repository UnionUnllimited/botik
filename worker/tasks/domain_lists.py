"""Пересборка списков доменов по расписанию.

Раньше это делал `cron` на сервере frps, дёргая `sync_local.sh`. Теперь круг
здесь: источники и свой список лежат у нас, и держать расписание отдельно
от данных значит однажды забыть про один из двух.
"""

from __future__ import annotations

import structlog

from core.db import session_scope
from core.services import domain_lists as service

log = structlog.get_logger("worker.domain_lists")


async def rebuild() -> int:
    """Собирает списки и возвращает число доменов — для журнала задачи."""
    async with session_scope() as session:
        record = await service.build(session)
        return record.domains
