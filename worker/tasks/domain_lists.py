"""Пересборка списков доменов по расписанию.

Раньше это делал `cron` на сервере frps, дёргая `sync_local.sh`. Теперь круг
здесь: источники и свой список лежат у нас, и держать расписание отдельно
от данных значит однажды забыть про один из двух.
"""

from __future__ import annotations

import time

import structlog

from core.db import session_scope
from core.services import domain_lists as service

log = structlog.get_logger("worker.domain_lists")

TICK_SEC = 60
"""Планировщик будит задачу раз в минуту, а решает она сама."""

_state: dict[str, float] = {}
"""Когда круг проходил в последний раз. Словарь, а не переменная модуля:
`global` на каждый заход читается хуже, чем один изменяемый объект."""


async def rebuild() -> int:
    """Собирает списки и возвращает число доменов — для журнала задачи.

    Круг стоит в планировщике коротким, а фактическую частоту задаёт настройка
    со страницы: задача сверяется с ней сама и пропускает лишние заходы.
    Так интервал меняется без перезапуска worker'а — а именно этого от него
    и ждут, правя настройку в панели.
    """
    async with session_scope() as session:
        conf = await service.config(session)
        try:
            wanted = max(1, int(conf.get("lists_poll_interval_min") or 10))
        except ValueError:
            wanted = 10
        now = time.monotonic()
        last = _state.get("last_run")
        if last is not None and (now - last) < wanted * 60 - TICK_SEC:
            return -1
        _state["last_run"] = now
        record = await service.build(session)
        return record.domains
