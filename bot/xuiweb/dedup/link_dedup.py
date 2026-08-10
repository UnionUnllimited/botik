"""One subscription link per display name (plain-text keys, not Xray JSON)."""

from __future__ import annotations

import base64
import json
import random
import re
from typing import TYPE_CHECKING, Protocol
from urllib.parse import quote, unquote

from .link_dedup_grouping import base_name_for_grouping, pool_key_from_remark

if TYPE_CHECKING:
    from .link_host_stats import TextLinkHostCache

_STUB_PREFIXES = (
    'vless://0000',
    'vless://00000000-0000-0000-0000-000000000000',
)
_VALID_MODES = frozenset({'random', 'online'})


class _HostStatsReader(Protocol):
    def is_ready(self) -> bool: ...
    def is_link_eligible(self, link: str) -> bool: ...
    def link_users_online(self, link: str) -> int: ...


def extract_link_display_name(link: str) -> str | None:
    """Profile name from URI fragment (or vmess ps)."""
    raw = (link or '').strip()
    if not raw:
        return None

    if raw.startswith('vmess://'):
        payload = raw[8:].split('?', 1)[0].split('#', 1)[0]
        pad = '=' * (-len(payload) % 4)
        try:
            data = json.loads(base64.urlsafe_b64decode(payload + pad))
            ps = str(data.get('ps') or '').strip()
            return ps or None
        except Exception:
            return None

    if '#' in raw:
        frag = raw.rsplit('#', 1)[-1]
        name = unquote(frag).strip()
        return name or None

    return None


def _group_key(link: str) -> str:
    name = extract_link_display_name(link)
    if name:
        return pool_key_from_remark(name)
    return f'uri:{link.strip()}'


def _is_stub_link(link: str) -> bool:
    s = (link or '').strip()
    return any(s.startswith(p) for p in _STUB_PREFIXES)


def _set_link_display_name(link: str, display_name: str) -> str:
    raw = (link or '').strip()
    if not display_name or _is_stub_link(raw):
        return raw

    if raw.startswith('vmess://'):
        main = raw[8:]
        hash_part = ''
        if '#' in main:
            main, hash_part = main.split('#', 1)
        query = ''
        if '?' in main:
            main, query_part = main.split('?', 1)
            query = f'?{query_part}'
        pad = '=' * (-len(main) % 4)
        try:
            data = json.loads(base64.urlsafe_b64decode(main + pad))
            data['ps'] = display_name
            payload = base64.urlsafe_b64encode(
                json.dumps(data, ensure_ascii=False).encode('utf-8'),
            ).decode('utf-8').rstrip('=')
            suffix = f'#{hash_part}' if hash_part else ''
            return f'vmess://{payload}{query}{suffix}'
        except Exception:
            return raw

    if '#' in raw:
        body = raw.rsplit('#', 1)[0]
        return f'{body}#{quote(display_name, safe="")}'

    return raw


def _emit_deduped_link(link: str) -> str:
    name = extract_link_display_name(link)
    if not name:
        return link
    canonical = base_name_for_grouping(name)
    if not canonical:
        return link
    return _set_link_display_name(link, canonical)


def _pick_online_balanced(
    candidates: list[str],
    host_stats: _HostStatsReader | None,
    *,
    online_threshold: int = 100,
) -> str | None:
    if not candidates:
        return None
    if not host_stats or not host_stats.is_ready():
        return random.choice(candidates)

    threshold = max(1, int(online_threshold or 100))
    pool: list[str] = []
    weights: list[float] = []
    for link in candidates:
        if not host_stats.is_link_eligible(link):
            continue
        online = max(0, host_stats.link_users_online(link))
        headroom = max(0, threshold - online)
        pool.append(link)
        weights.append(float(headroom + 1))

    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]
    return random.choices(pool, weights=weights, k=1)[0]


def deduplicate_subscription_links(
    links: list[str],
    mode: str = 'random',
    host_stats: _HostStatsReader | None = None,
    *,
    online_threshold: int = 100,
) -> list[str]:
    if not links:
        return []

    pick_mode = mode if mode in _VALID_MODES else 'random'

    groups: dict[str, list[str]] = {}
    order: list[str] = []

    for link in links:
        if not isinstance(link, str):
            continue
        s = link.strip()
        if not s:
            continue
        key = _group_key(s)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(s)

    out: list[str] = []
    for key in order:
        candidates = list(dict.fromkeys(groups[key]))
        if not candidates:
            continue
        if len(candidates) == 1 or _is_stub_link(candidates[0]):
            if pick_mode == 'online' and host_stats and host_stats.is_ready():
                if not host_stats.is_link_eligible(candidates[0]):
                    continue
            out.append(_emit_deduped_link(candidates[0]))
            continue
        if pick_mode == 'online':
            picked = _pick_online_balanced(
                candidates, host_stats, online_threshold=online_threshold,
            )
            if not picked:
                continue
        elif pick_mode == 'random':
            picked = random.choice(candidates)
        else:
            picked = candidates[0]
        out.append(_emit_deduped_link(picked))
    return out
