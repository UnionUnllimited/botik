"""Group matching and LTE detection."""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from .countries import (
    COUNTRY_PATTERNS,
    _RENEWAL_DATE_IN_NAME_RE,
    _TRAILING_SUFFIX_RE,
    _best_pattern_match,
    build_groups_from_hosts,
    is_lte_tag,
    is_no_auto_tag,
    is_russia_remark,
    strip_no_auto_markers,
)
from .settings import DEFAULT_AUTO_GROUP_NAME, DEFAULT_LTE_AUTO_GROUP_NAME

LTE_PATTERNS = ['LTE']
AUTO_GROUP_NAME = DEFAULT_AUTO_GROUP_NAME
LTE_AUTO_GROUP_NAME = DEFAULT_LTE_AUTO_GROUP_NAME
SYSTEM_PROTOCOLS = frozenset({'freedom', 'blackhole', 'dns'})


def sanitize_tag(name: str) -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9_-]', '_', str(name))
    cleaned = re.sub(r'_+', '_', cleaned).strip('_')
    if cleaned:
        return cleaned
    digest = hashlib.sha256(str(name).encode()).hexdigest()[:8]
    return f'g{digest}'


def is_lte_outbound(tag: str, lte_patterns: list[str] | None = None) -> bool:
    return is_lte_tag(tag, lte_patterns)


def match_name_patterns(text: str, patterns: list[str] | None) -> bool:
    """True if host remark/tag contains any of the configured substrings."""
    if not patterns:
        return False
    raw = str(text or '')
    text_lower = raw.lower()
    for pattern in patterns:
        item = str(pattern or '').strip()
        if not item:
            continue
        if item in raw or item.lower() in text_lower:
            return True
    return False


def is_auto_excluded_outbound(
    tag: str,
    groups: dict[str, list[str]] | None,
    *,
    lte_patterns: list[str] | None = None,
    auto_exclude_patterns: list[str] | None = None,
) -> bool:
    """Hosts excluded from AUTO group entirely (not from the LTE tier cascade inside it)."""
    if is_no_auto_outbound(tag):
        return True
    if is_russia_outbound(tag, groups):
        return True
    if match_name_patterns(tag, auto_exclude_patterns):
        return True
    return False


def is_no_auto_outbound(tag: str) -> bool:
    return is_no_auto_tag(tag)


def is_russia_outbound(tag: str, groups: dict[str, list[str]] | None = None) -> bool:
    if is_russia_remark(tag):
        return True
    if not groups:
        return False
    matched = match_group(tag, groups)
    if not matched:
        return False
    if '🇷🇺' in matched:
        return True
    return _best_pattern_match(matched, {'Russia': COUNTRY_PATTERNS['Russia']}) == 'Russia'


def standalone_display_name(tag: str) -> str:
    """Happ button title for a no-auto host (remark with markers stripped)."""
    text = str(tag or '').strip()
    text = _RENEWAL_DATE_IN_NAME_RE.sub('', text).strip()
    text = strip_no_auto_markers(text)
    text = re.sub(r'\s*\([^)]*\)', '', text).strip()
    text = re.sub(r'\s*\[[^\]]*\]', '', text).strip()
    text = _TRAILING_SUFFIX_RE.sub('', text).strip()
    return text or str(tag)


def match_group(outbound_tag: str, groups: dict[str, list[str]]) -> str | None:
    return _best_pattern_match(outbound_tag, groups)


def is_stub_proxy_outbound(ob: dict) -> bool:
    settings = ob.get('settings') or {}
    vnext = (settings.get('vnext') or [{}])[0]
    servers = (settings.get('servers') or [{}])[0]
    addr = vnext.get('address') or servers.get('address') or ''
    port = vnext.get('port') if vnext.get('port') is not None else servers.get('port')
    return addr == '0.0.0.0' and port == 1


def config_proxy_outbounds(cfg: dict) -> list[dict]:
    out: list[dict] = []
    for ob in cfg.get('outbounds') or []:
        if ob.get('protocol') in SYSTEM_PROTOCOLS or not ob.get('tag'):
            continue
        out.append(ob)
    return out


def is_passthrough_config(cfg: dict) -> bool:
    """True if cfg is a real Remnawave host entry (not stub/fake)."""
    proxies = config_proxy_outbounds(cfg)
    if not proxies:
        return False
    return not all(is_stub_proxy_outbound(ob) for ob in proxies)


def collect_passthrough_configs(config_array: list[dict]) -> list[dict]:
    """Original Remnawave configs unchanged — for auto_only balancer mode."""
    return [copy.deepcopy(cfg) for cfg in config_array if is_passthrough_config(cfg)]


def is_fake_config(config_array: list[dict]) -> bool:
    proxy_count = 0
    fake_count = 0
    for cfg in config_array:
        for ob in cfg.get('outbounds') or []:
            if ob.get('protocol') in SYSTEM_PROTOCOLS or not ob.get('tag'):
                continue
            proxy_count += 1
            if is_stub_proxy_outbound(ob):
                fake_count += 1
    return proxy_count > 0 and proxy_count == fake_count


def collect_all_proxy_outbounds(config_array: list[dict]) -> list[dict[str, Any]]:
    all_out: list[dict[str, Any]] = []
    seen_tags: set[str] = set()
    for i, cfg in enumerate(config_array):
        remarks = cfg.get('remarks') or f'connection-{i}'
        for ob in cfg.get('outbounds') or []:
            if ob.get('protocol') in SYSTEM_PROTOCOLS or not ob.get('tag'):
                continue
            cloned = copy.deepcopy(ob)
            tag = cloned['tag']
            if tag == 'proxy' and remarks:
                tag = remarks
            if tag in seen_tags:
                tag = f'{tag}-{i}'
            cloned['tag'] = tag
            seen_tags.add(tag)
            all_out.append(cloned)
    return all_out


def group_outbounds(
    outbounds: list[dict],
    groups: dict[str, list[str]],
    *,
    lte_auto_group_name: str | None = None,
    lte_patterns: list[str] | None = None,
) -> dict[str, list[dict]]:
    lte_key = (lte_auto_group_name or LTE_AUTO_GROUP_NAME).strip() or LTE_AUTO_GROUP_NAME
    grouped: dict[str, list[dict]] = {}
    for ob in outbounds:
        if is_no_auto_outbound(ob['tag']):
            continue
        if is_lte_outbound(ob['tag'], lte_patterns):
            grouped.setdefault(lte_key, []).append(ob)
            country_group = match_group(ob['tag'], groups)
            if country_group:
                grouped.setdefault(country_group, []).append(ob)
            continue
        group = match_group(ob['tag'], groups)
        if group:
            grouped.setdefault(group, []).append(ob)
    return grouped


def resolve_groups(hosts: list | None) -> dict[str, list[str]]:
    if hosts:
        built = build_groups_from_hosts(hosts)
        if built:
            return built
    return {
        f"{info['emoji']} {info.get('label', name)}": info['patterns']
        for name, info in COUNTRY_PATTERNS.items()
        if name != 'LTE'
    }
