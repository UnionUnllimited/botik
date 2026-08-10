"""Remnawave node load cache and outbound filtering for the balancer."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from copy import deepcopy
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MAX_USERS_PER_GB = 50.0
DEFAULT_MAX_USERS_PER_CPU = 80.0
DEFAULT_NODE_LOAD_THRESHOLD = 1.0  # sync with admin defaults xray_balancer_node_*


class NodeStatsSettings:
    def __init__(
        self,
        enabled: bool = False,
        max_users_per_gb: float = DEFAULT_MAX_USERS_PER_GB,
        max_users_per_cpu: float = DEFAULT_MAX_USERS_PER_CPU,
        node_load_threshold: float = DEFAULT_NODE_LOAD_THRESHOLD,
        interval_sec: int = 120,
    ) -> None:
        self.enabled = enabled
        self.max_users_per_gb = max_users_per_gb
        self.max_users_per_cpu = max_users_per_cpu
        self.node_load_threshold = node_load_threshold
        self.interval_sec = interval_sec


def parse_ram_gb(ram: Any) -> float | None:
    if ram is None:
        return None
    if isinstance(ram, (int, float)):
        val = float(ram)
        if val <= 0:
            return None
        return val / (1024 ** 3)
    if not isinstance(ram, str):
        return None
    match = re.match(r'([\d.]+)\s*(GB|MB|KB|B)', ram.strip(), re.I)
    if not match:
        return None
    val = float(match.group(1))
    if val <= 0:
        return None
    unit = match.group(2).upper()
    if unit == 'KB':
        return val / (1024 ** 2)
    if unit == 'B':
        return val / (1024 ** 3)
    if unit == 'MB':
        return val / 1024
    return val


def _escape_regex(text: str) -> str:
    return re.escape(text)


def _compute_load(
    users_online: int,
    total_ram_gb: float | None,
    cpu_count: int | None,
    *,
    max_users_per_gb: float,
    max_users_per_cpu: float,
) -> float | None:
    ram_load = None
    cpu_load = None
    if total_ram_gb is not None and total_ram_gb > 0:
        ram_load = users_online / total_ram_gb
    if cpu_count is not None and cpu_count > 0:
        cpu_load = users_online / cpu_count

    if ram_load is not None and cpu_load is not None:
        return round(max(ram_load / max_users_per_gb, cpu_load / max_users_per_cpu) * 100) / 100
    if ram_load is not None:
        return round((ram_load / max_users_per_gb) * 100) / 100
    if cpu_load is not None:
        return round((cpu_load / max_users_per_cpu) * 100) / 100
    return None


class NodeStatsCache:
    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}
        self._last_refresh_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def last_refresh_at(self) -> float | None:
        return self._last_refresh_at

    def is_ready(self) -> bool:
        return bool(self._cache)

    async def refresh(
        self,
        *,
        rw_base_url: str,
        rw_token: str | None,
        http_client: httpx.AsyncClient | None,
        settings: NodeStatsSettings,
    ) -> None:
        if not rw_base_url or not rw_token:
            logger.warning('[node-stats] пропуск: нет Remnawave URL или API token')
            return

        url = f"{rw_base_url.rstrip('/')}/api/nodes/"
        headers = {'Accept': 'application/json', 'Authorization': f'Bearer {rw_token}'}
        try:
            if http_client is not None:
                resp = await http_client.get(url, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                    resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning('[node-stats] /api/nodes/ → %s', resp.status_code)
                return
            data = resp.json()
            nodes = data.get('response') if isinstance(data, dict) else data
            if not isinstance(nodes, list):
                return
        except Exception as exc:
            logger.warning('[node-stats] fetch failed: %s', exc)
            return

        new_cache: dict[str, dict] = {}
        for node in nodes:
            name = node.get('name') or ''
            if not name:
                continue
            users_online = int(node.get('usersOnline') or node.get('connected_users') or 0)
            sys = node.get('system') or {}
            sys_info = sys.get('info') or sys
            total_ram_gb = parse_ram_gb(
                sys_info.get('memoryTotal') or sys_info.get('totalRam') or node.get('totalRam')
            )
            cpu_raw = sys_info.get('cpuCount') or sys.get('cpuCount') or node.get('cpuCount')
            try:
                cpu_count = int(cpu_raw) if cpu_raw is not None and int(cpu_raw) > 0 else None
            except (TypeError, ValueError):
                cpu_count = None

            load = _compute_load(
                users_online,
                total_ram_gb,
                cpu_count,
                max_users_per_gb=settings.max_users_per_gb,
                max_users_per_cpu=settings.max_users_per_cpu,
            )
            stats = {
                'usersOnline': users_online,
                'totalRamGb': total_ram_gb,
                'cpuCount': cpu_count,
                'load': load,
                'isConnected': bool(node.get('isConnected')),
                'isDisabled': bool(node.get('isDisabled')),
            }
            new_cache[name] = stats
            profile = node.get('configProfile') or {}
            for inb in profile.get('activeInbounds') or []:
                tag = inb.get('tag')
                if tag and tag != name:
                    new_cache[tag] = deepcopy(stats)

        async with self._lock:
            self._cache = new_cache
            self._last_refresh_at = time.time()

        logger.info(
            '[node-stats] обновлено %s записей (пороги: %s u/GB, %s u/CPU, load<=%s)',
            len(new_cache),
            settings.max_users_per_gb,
            settings.max_users_per_cpu,
            settings.node_load_threshold,
        )

    def get_node_stats(self, outbound_tag: str) -> dict | None:
        if not outbound_tag or not self._cache:
            return None
        if outbound_tag in self._cache:
            return self._cache[outbound_tag]

        tag_lower = outbound_tag.lower()
        for node_name, stats in self._cache.items():
            if node_name.lower() == tag_lower:
                return stats

        best_boundary = None
        best_boundary_len = 0
        best_substr = None
        best_substr_len = 0
        for node_name, stats in self._cache.items():
            name_l = node_name.lower()
            if len(name_l) < 3 or name_l not in tag_lower:
                continue
            pattern = rf'(?:^|[^a-z0-9]){_escape_regex(name_l)}(?:$|[^a-z0-9])'
            if re.search(pattern, tag_lower) and len(name_l) > best_boundary_len:
                best_boundary = stats
                best_boundary_len = len(name_l)
            elif len(name_l) > best_substr_len:
                best_substr = stats
                best_substr_len = len(name_l)
        return best_boundary or best_substr

    def filter_and_sort_by_load(
        self,
        outbounds: list[dict],
        *,
        node_load_threshold: float = DEFAULT_NODE_LOAD_THRESHOLD,
    ) -> list[dict]:
        if not self._cache:
            return outbounds

        with_stats = []
        for ob in outbounds:
            stats = self.get_node_stats(ob.get('tag', ''))
            load = stats.get('load') if stats and stats.get('load') is not None else 0.5
            with_stats.append({'ob': ob, 'stats': stats, 'load': load})

        filtered = []
        for item in with_stats:
            stats = item['stats']
            if not stats:
                filtered.append(item)
                continue
            if not stats.get('isConnected') or stats.get('isDisabled'):
                continue
            load = stats.get('load')
            if load is not None and load > node_load_threshold:
                continue
            filtered.append(item)

        if not filtered:
            connected = [
                item for item in with_stats
                if not item['stats'] or (item['stats'].get('isConnected') and not item['stats'].get('isDisabled'))
            ]
            if not connected:
                return outbounds
            connected.sort(key=lambda x: x['load'])
            return [x['ob'] for x in connected]

        before = len(outbounds)
        filtered.sort(key=lambda x: x['load'])
        result = [x['ob'] for x in filtered]
        if len(result) < before:
            logger.info('[node-stats] отфильтровано: %s → %s серверов', before, len(result))
        return result


node_stats_cache = NodeStatsCache()
