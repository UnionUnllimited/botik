"""
Кэш онлайн-статуса клиентов Remnawave (in-memory, на процесс).

Обновляется по запросу при открытии списка клиентов (не фоновая задача).
Таблица клиентов читает отсюда, чтобы показать бейдж «Онлайн».
"""
from __future__ import annotations

import time
from typing import Iterable, Set

_online_ids: Set[int] = set()
_last_refresh_ts: float = 0.0


def set_online_ids(ids: Iterable[int]) -> None:
    """Полностью заменяет множество онлайн telegram_id (вызывает фоновая задача)."""
    global _online_ids, _last_refresh_ts
    cleaned: Set[int] = set()
    for v in ids or ():
        try:
            cleaned.add(int(v))
        except (TypeError, ValueError):
            continue
    _online_ids = cleaned
    _last_refresh_ts = time.time()


def is_online(telegram_id) -> bool:
    if telegram_id is None:
        return False
    try:
        return int(telegram_id) in _online_ids
    except (TypeError, ValueError):
        return False


def has_data() -> bool:
    """True, если задача хоть раз успешно отработала (есть актуальные данные)."""
    return _last_refresh_ts > 0


def online_count() -> int:
    return len(_online_ids)


def last_refresh_ts() -> float:
    return _last_refresh_ts
