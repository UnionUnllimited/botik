"""Host/node online cache for text subscription link balancing."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import unquote

import httpx

from .link_dedup import extract_link_display_name

logger = logging.getLogger(__name__)

DEFAULT_ONLINE_THRESHOLD = 100
DEFAULT_INTERVAL_SEC = 60


@dataclass
class TextLinkHostStatsSettings:
    enabled: bool = False
    online_threshold: int = DEFAULT_ONLINE_THRESHOLD
    interval_sec: int = DEFAULT_INTERVAL_SEC


@dataclass
class _HostEntry:
    uuid: str
    remark: str
    address: str
    port: int
    has_nodes: bool
    eligible: bool
    users_online: int


def _parse_endpoint(link: str) -> tuple[str | None, int | None]:
    raw = (link or '').strip()
    if not raw:
        return None, None

    if raw.startswith('vmess://'):
        payload = raw[8:].split('?', 1)[0].split('#', 1)[0]
        pad = '=' * (-len(payload) % 4)
        try:
            data = json.loads(base64.urlsafe_b64decode(payload + pad))
            addr = str(data.get('add') or '').strip()
            port = int(data.get('port') or 0)
            return (addr.strip('[]'), port) if addr and port > 0 else (None, None)
        except Exception:
            return None, None

    if '://' not in raw:
        return None, None

    rest = raw.split('://', 1)[1]
    if '@' in rest:
        rest = rest.split('@', 1)[1]
    host_part = rest.split('?', 1)[0].split('#', 1)[0]
    if not host_part:
        return None, None

    if host_part.startswith('['):
        m = re.match(r'^\[([^\]]+)\]:(\d+)$', host_part)
        if m:
            return m.group(1), int(m.group(2))
        return None, None

    if ':' in host_part:
        host, port_str = host_part.rsplit(':', 1)
        try:
            return host.strip(), int(port_str)
        except ValueError:
            return None, None
    return None, None


def _host_nodes(host: dict) -> list[str]:
    nodes = host.get('nodes') or []
    out: list[str] = []
    for item in nodes:
        if item is None:
            continue
        out.append(str(item))
    return out


def _node_online(node: dict) -> int:
    try:
        return int(node.get('usersOnline') or node.get('users_online') or 0)
    except (TypeError, ValueError):
        return 0


def _evaluate_host(host: dict, nodes_by_uuid: dict[str, dict], threshold: int) -> _HostEntry:
    remark = str(host.get('remark') or host.get('tag') or '').strip()
    address = str(host.get('address') or '').strip()
    try:
        port = int(host.get('port') or 0)
    except (TypeError, ValueError):
        port = 0
    node_uuids = _host_nodes(host)
    has_nodes = bool(node_uuids)

    if host.get('isDisabled') or host.get('is_disabled'):
        return _HostEntry(
            uuid=str(host.get('uuid') or ''),
            remark=remark,
            address=address,
            port=port,
            has_nodes=has_nodes,
            eligible=False,
            users_online=0,
        )

    if not has_nodes:
        return _HostEntry(
            uuid=str(host.get('uuid') or ''),
            remark=remark,
            address=address,
            port=port,
            has_nodes=False,
            eligible=True,
            users_online=0,
        )

    max_online = 0
    for node_uuid in node_uuids:
        node = nodes_by_uuid.get(node_uuid)
        if not node:
            return _HostEntry(
                uuid=str(host.get('uuid') or ''),
                remark=remark,
                address=address,
                port=port,
                has_nodes=True,
                eligible=False,
                users_online=max_online,
            )
        if not node.get('isConnected') or node.get('isDisabled') or node.get('is_disabled'):
            return _HostEntry(
                uuid=str(host.get('uuid') or ''),
                remark=remark,
                address=address,
                port=port,
                has_nodes=True,
                eligible=False,
                users_online=max_online,
            )
        max_online = max(max_online, _node_online(node))

    eligible = max_online <= threshold
    return _HostEntry(
        uuid=str(host.get('uuid') or ''),
        remark=remark,
        address=address,
        port=port,
        has_nodes=True,
        eligible=eligible,
        users_online=max_online,
    )


async def _fetch_json_list(
    url: str,
    token: str | None,
    http_client: httpx.AsyncClient | None,
) -> list | None:
    headers: dict[str, str] = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        if http_client is not None:
            resp = await http_client.get(url, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
                resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.warning('[link-host-stats] %s → HTTP %s', url, resp.status_code)
            return None
        data = resp.json()
        if isinstance(data, dict):
            payload = data.get('response')
            if isinstance(payload, list):
                return payload
            return data.get('hosts')
        if isinstance(data, list):
            return data
    except Exception as exc:
        logger.warning('[link-host-stats] fetch failed %s: %s', url, exc)
    return None


class TextLinkHostCache:
    """Maps subscription URIs to host eligibility by Remnawave host/node stats."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._by_endpoint: dict[tuple[str, int], list[_HostEntry]] = {}
        self._last_refresh_at: float | None = None

    @property
    def last_refresh_at(self) -> float | None:
        return self._last_refresh_at

    def is_ready(self) -> bool:
        return self._last_refresh_at is not None

    async def refresh(
        self,
        *,
        rw_base_url: str,
        rw_token: str | None,
        http_client: httpx.AsyncClient | None,
        settings: TextLinkHostStatsSettings,
    ) -> None:
        if not rw_base_url:
            return
        base = rw_base_url.rstrip('/')
        nodes_raw = await _fetch_json_list(f'{base}/api/nodes/', rw_token, http_client)
        hosts_raw = await _fetch_json_list(f'{base}/api/hosts/', rw_token, http_client)
        if nodes_raw is None or hosts_raw is None:
            return

        nodes_by_uuid: dict[str, dict] = {}
        for node in nodes_raw:
            if not isinstance(node, dict):
                continue
            uid = str(node.get('uuid') or '')
            if uid:
                nodes_by_uuid[uid] = node

        by_endpoint: dict[tuple[str, int], list[_HostEntry]] = {}
        eligible_count = 0
        for host in hosts_raw:
            if not isinstance(host, dict):
                continue
            entry = _evaluate_host(host, nodes_by_uuid, settings.online_threshold)
            if not entry.address or entry.port <= 0:
                continue
            key = (entry.address.lower(), entry.port)
            by_endpoint.setdefault(key, []).append(entry)
            if entry.eligible:
                eligible_count += 1

        async with self._lock:
            self._by_endpoint = by_endpoint
            self._last_refresh_at = time.time()

        logger.info(
            '[link-host-stats] обновлено %s endpoint(s), %s/%s хостов доступны (порог ≤%s online)',
            len(by_endpoint),
            eligible_count,
            sum(len(v) for v in by_endpoint.values()),
            settings.online_threshold,
        )

    def _resolve(self, link: str) -> _HostEntry | None:
        addr, port = _parse_endpoint(link)
        if not addr or not port:
            return None
        entries = self._by_endpoint.get((addr.lower(), port))
        if not entries:
            return None
        if len(entries) == 1:
            return entries[0]

        remark = extract_link_display_name(link)
        if remark:
            remark_cf = remark.casefold()
            for entry in entries:
                if entry.remark.casefold() == remark_cf:
                    return entry
            for entry in entries:
                if unquote(entry.remark).casefold() == remark_cf:
                    return entry
        return entries[0]

    def is_link_eligible(self, link: str) -> bool:
        entry = self._resolve(link)
        if entry is None:
            return True
        if not entry.has_nodes:
            return True
        return entry.eligible

    def link_users_online(self, link: str) -> int:
        entry = self._resolve(link)
        if entry is None:
            return 0
        return entry.users_online


text_link_host_cache = TextLinkHostCache()
