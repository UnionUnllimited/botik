"""TTL-кэш онлайн-клиентов 3X-UI серверов.

Зачем нужно: вызов `xui_manager.get_online_clients_count(...)` дёргает панель
по `POST /panel/api/inbounds/onlines` и на больших панелях ощутимо тормозит.
Этот ответ почти не меняется в течение пары минут, поэтому держим его в общем
кэше, а раз в `REFRESH_INTERVAL_SECONDS` фоновой задачей обновляем разом по
всем серверам.

Использование:
- Горячий путь (страница «Сервера», `_fetch_one_server_status`,
  `online_clients_total`) — читает значения из `get_cached_online(server_id)`
  и `get_cached_total()`. Если кэша нет (только что стартовали или сервер
  новый) — может опционально дёрнуть «дозагрузку» через `prime_one(...)`.
- Планировщик в `web_admin/run.py` запускает `refresh_all_async(...)` каждые
  `REFRESH_INTERVAL_SECONDS` секунд.

Все функции потокобезопасны в рамках event loop (Python GIL + dict.update —
атомарны для нашей нагрузки; явных блокировок не нужно).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from loguru import logger


# ─────────────────────────────────────────────────────────────────────────
# Конфигурация
# ─────────────────────────────────────────────────────────────────────────

# Дефолтный интервал/TTL — 3 минуты, как договорились.
REFRESH_INTERVAL_SECONDS: int = 180
# TTL «свежести» отдельного значения. Делаем чуть больше интервала,
# чтобы между тиками планировщика не было дыры в 1 секунду.
ENTRY_TTL_SECONDS: int = REFRESH_INTERVAL_SECONDS + 60

# Максимальное время на сбор онлайна с одного сервера в фоне.
PER_SERVER_TIMEOUT: float = 8.0


# ─────────────────────────────────────────────────────────────────────────
# Хранилище
# ─────────────────────────────────────────────────────────────────────────

# server_id -> {'online': int, 'ts': float (epoch sec)}
_CACHE: dict[int, dict] = {}
# Сводка по всем 3X-UI серверам.
_TOTAL_CACHE: dict = {'total': 0, 'ts': 0.0, 'have_servers': 0}
# Защита от параллельных полных рефрешей (например, тик планировщика +
# первый «прогревающий» вызов).
_refresh_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


def _now() -> float:
    return time.time()


def _fresh(entry: Optional[dict]) -> bool:
    if not entry:
        return False
    ts = entry.get('ts') or 0
    return (_now() - ts) <= ENTRY_TTL_SECONDS


# ─────────────────────────────────────────────────────────────────────────
# Чтение из кэша (быстрый путь)
# ─────────────────────────────────────────────────────────────────────────

def get_cached_online(server_id: int) -> Optional[int]:
    """Вернёт значение онлайна, если оно свежее. Иначе None."""
    if server_id is None:
        return None
    entry = _CACHE.get(int(server_id))
    if _fresh(entry):
        return int(entry.get('online') or 0)
    return None


def get_cached_total() -> Optional[dict]:
    """Сводка `{total, ts, have_servers, fresh}` или None если ещё не считали."""
    if not _TOTAL_CACHE.get('ts'):
        return None
    return {
        'total': int(_TOTAL_CACHE.get('total') or 0),
        'ts': _TOTAL_CACHE.get('ts') or 0.0,
        'have_servers': int(_TOTAL_CACHE.get('have_servers') or 0),
        'fresh': _fresh(_TOTAL_CACHE),
    }


def is_cache_warm() -> bool:
    """True если хотя бы один полный рефреш уже прошёл."""
    return bool(_TOTAL_CACHE.get('ts'))


# ─────────────────────────────────────────────────────────────────────────
# Сборка (медленный путь — за пределами горячих хендлеров)
# ─────────────────────────────────────────────────────────────────────────

async def _fetch_one(server_conf: dict) -> tuple[Optional[int], Optional[int]]:
    """Возвращает `(server_id, online)`; online=None если не удалось."""
    sid = server_conf.get('id')
    name = server_conf.get('name', f'id={sid}')
    try:
        # Локальный импорт, чтобы не утаскивать xui_manager в импорт-граф core.
        from x_ui_manager import xui_manager_instance  # noqa: WPS433

        online = await asyncio.wait_for(
            xui_manager_instance.get_online_clients_count(server_conf),
            timeout=PER_SERVER_TIMEOUT,
        )
        if online is None:
            return sid, None
        return sid, int(online)
    except asyncio.TimeoutError:
        logger.debug(f"[XUI-ONLINE] Таймаут получения онлайна с '{name}'")
        return sid, None
    except Exception as e:
        logger.debug(f"[XUI-ONLINE] Ошибка получения онлайна с '{name}': {e}")
        return sid, None


async def _load_servers() -> list[dict]:
    """Достаёт список 3X-UI серверов из settings (без зависимости от
    web_admin.routes.api, чтобы не плодить циклические импорты).
    """
    try:
        from web_admin.async_db import async_query_db  # noqa: WPS433
        row = await async_query_db(
            "SELECT value FROM settings WHERE key = 'xui_servers'", (), one=True,
        )
        if not row or not row.get('value'):
            return []
        servers = json.loads(row['value'])
        return servers if isinstance(servers, list) else []
    except Exception as e:
        logger.warning(f"[XUI-ONLINE] Не удалось прочитать xui_servers: {e}")
        return []


async def refresh_all_async(servers: Optional[list[dict]] = None) -> dict:
    """Полный рефреш кэша по всем серверам параллельно.

    Возвращает summary `{total, have_servers, online_servers, missed}`.
    Безопасно вызывается из планировщика и из «прогревов» — лочится.
    """
    async with _get_lock():
        if servers is None:
            servers = await _load_servers()
        if not servers:
            # Список пуст — кэш просто очищаем и помечаем.
            _CACHE.clear()
            _TOTAL_CACHE.update({'total': 0, 'ts': _now(), 'have_servers': 0})
            return {'total': 0, 'have_servers': 0, 'online_servers': 0, 'missed': 0}

        results = await asyncio.gather(
            *[_fetch_one(s) for s in servers], return_exceptions=False
        )

        now = _now()
        total = 0
        ok_servers = 0
        missed = 0
        for sid, online in results:
            if sid is None:
                continue
            if online is None:
                # Не обновляем старое значение — оно может стать stale естественно.
                missed += 1
                continue
            _CACHE[int(sid)] = {'online': int(online), 'ts': now}
            total += int(online)
            ok_servers += 1

        _TOTAL_CACHE.update({
            'total': total,
            'ts': now,
            'have_servers': len(servers),
        })

        logger.info(
            f"[XUI-ONLINE] Рефреш кэша: {ok_servers}/{len(servers)} серверов, "
            f"online={total}, не ответили={missed}"
        )
        return {
            'total': total,
            'have_servers': len(servers),
            'online_servers': ok_servers,
            'missed': missed,
        }


async def prime_one(server_conf: dict) -> Optional[int]:
    """Точечный прогрев одного сервера (можно вызывать из горячих путей,
    если в кэше нет данных и хочется получить значение сейчас).
    """
    sid, online = await _fetch_one(server_conf)
    if sid is not None and online is not None:
        _CACHE[int(sid)] = {'online': int(online), 'ts': _now()}
    return online


__all__ = [
    'REFRESH_INTERVAL_SECONDS',
    'ENTRY_TTL_SECONDS',
    'get_cached_online',
    'get_cached_total',
    'is_cache_warm',
    'refresh_all_async',
    'prime_one',
]
