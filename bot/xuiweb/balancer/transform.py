"""Apply smart balancing to Remnawave Xray JSON subscription."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .generator import build_group_config, build_standalone_config, prepare_base_template
from .groups import (
    collect_all_proxy_outbounds,
    collect_passthrough_configs,
    group_outbounds,
    is_auto_excluded_outbound,
    is_fake_config,
    is_lte_outbound,
    is_no_auto_outbound,
    resolve_groups,
    standalone_display_name,
)
from .node_stats import NodeStatsCache, NodeStatsSettings, node_stats_cache
from .routing import RoutingSettings, parse_routing_config
from .settings import BALANCER_MODE_AUTO_ONLY, BalancerSettings

logger = logging.getLogger(__name__)

_XRAY_JSON_CLIENT_MARKERS = ('happ', 'incy', 'v2raytun')


@dataclass
class BalancerOptions:
    node_stats: NodeStatsSettings | None = None
    routing: RoutingSettings | None = None
    balancer: BalancerSettings = field(default_factory=BalancerSettings)


def is_xray_json_client(
    user_agent: str | None,
    allowed_markers: tuple[str, ...] | list[str] | None = None,
) -> bool:
    if not user_agent:
        return False
    markers = allowed_markers if allowed_markers is not None else _XRAY_JSON_CLIENT_MARKERS
    if not markers:
        return False
    ua = user_agent.lower()
    return any(marker in ua for marker in markers)


async def _fetch_remnawave_hosts(
    base_url: str,
    token: str | None,
    http_client: httpx.AsyncClient | None,
) -> list | None:
    if not base_url:
        return None
    url = f"{base_url.rstrip('/')}/api/hosts/"
    headers: dict[str, str] = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        if http_client is not None:
            resp = await http_client.get(url, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.warning('[balancer] Remnawave /api/hosts/ → %s', resp.status_code)
            return None
        data = resp.json()
        if isinstance(data, dict):
            return data.get('response') or data.get('hosts')
        if isinstance(data, list):
            return data
    except Exception as exc:
        logger.warning('[balancer] hosts fetch failed: %s', exc)
    return None


def _normalize_config_array(xray_config: Any) -> list[dict] | None:
    if xray_config is None:
        return None
    if isinstance(xray_config, str):
        import json
        try:
            xray_config = json.loads(xray_config)
        except Exception:
            return None
    if isinstance(xray_config, dict):
        return [xray_config]
    if isinstance(xray_config, list):
        return xray_config
    return None


def _build_balanced_configs(
    config_array: list[dict],
    groups: dict[str, list[str]],
    *,
    options: BalancerOptions | None = None,
    stats_cache: NodeStatsCache | None = None,
) -> list[dict]:
    if is_fake_config(config_array):
        logger.info('[balancer] fake/stub config — skip balancing')
        return config_array

    base_config = config_array[0]
    all_outbounds = collect_all_proxy_outbounds(config_array)
    if not all_outbounds:
        return config_array

    opts = options or BalancerOptions()
    bal_cfg = opts.balancer
    lte_auto_name = bal_cfg.lte_auto_group_name
    auto_name = bal_cfg.auto_group_name

    ns_opts = opts.node_stats
    cache = stats_cache or node_stats_cache
    if ns_opts and ns_opts.enabled and cache.is_ready():
        all_outbounds = cache.filter_and_sort_by_load(
            all_outbounds,
            node_load_threshold=ns_opts.node_load_threshold,
        )
        if not all_outbounds:
            logger.warning('[node-stats] все серверы отфильтрованы — используем исходный список')
            all_outbounds = collect_all_proxy_outbounds(config_array)

    grouped = group_outbounds(
        all_outbounds,
        groups,
        lte_auto_group_name=lte_auto_name,
        lte_patterns=bal_cfg.lte_triggers,
    )
    if not grouped:
        has_auto = any(
            not is_auto_excluded_outbound(
                o['tag'], groups,
                lte_patterns=bal_cfg.lte_triggers,
                auto_exclude_patterns=bal_cfg.auto_exclude_patterns,
            )
            for o in all_outbounds
        )
        has_standalone = any(is_no_auto_outbound(o['tag']) for o in all_outbounds)
        if not has_auto and not has_standalone:
            logger.warning('[balancer] no groups matched for %s outbounds', len(all_outbounds))
            return config_array

    auto_only_mode = bal_cfg.mode == BALANCER_MODE_AUTO_ONLY
    base_tpl = prepare_base_template(base_config, bal_cfg)
    logger.info(
        '[balancer] mode=%s dns=%s queryStrategy=%s domainStrategy=%s',
        bal_cfg.mode,
        (base_tpl.get('dns') or {}).get('servers'),
        (base_tpl.get('dns') or {}).get('queryStrategy'),
        bal_cfg.domain_strategy,
    )
    result: list[dict] = []

    routing_settings = opts.routing

    auto_outbounds = [
        o for o in all_outbounds
        if not is_auto_excluded_outbound(
            o['tag'], groups,
            lte_patterns=bal_cfg.lte_triggers,
            auto_exclude_patterns=bal_cfg.auto_exclude_patterns,
        )
    ]
    if auto_outbounds:
        auto_cfg = build_group_config(
            base_config,
            auto_name,
            auto_outbounds,
            None,
            base_tpl,
            routing_settings,
            bal_cfg,
        )
        if auto_cfg:
            result.append(auto_cfg)

    lte_outbounds = grouped.get(lte_auto_name)
    if lte_outbounds:
        lte_auto_cfg = build_group_config(
            base_config,
            lte_auto_name,
            lte_outbounds,
            None,
            base_tpl,
            routing_settings,
            bal_cfg,
        )
        if lte_auto_cfg:
            result.append(lte_auto_cfg)

    if auto_only_mode:
        passthrough = collect_passthrough_configs(config_array)
        result.extend(passthrough)
        if not result:
            return config_array
        logger.info(
            '[balancer] auto_only: %s auto/lte + %s passthrough → %s configs',
            len(result) - len(passthrough), len(passthrough), len(result),
        )
        return result

    group_order = list(groups.keys())
    for group_name in group_order:
        if group_name == lte_auto_name:
            continue
        obs = grouped.get(group_name)
        if not obs:
            continue
        group_cfg = build_group_config(
            base_config, group_name, obs, None, base_tpl, routing_settings, bal_cfg,
        )
        if group_cfg:
            result.append(group_cfg)

    for ob in all_outbounds:
        if not is_no_auto_outbound(ob['tag']):
            continue
        standalone_cfg = build_standalone_config(
            base_config,
            standalone_display_name(ob['tag']),
            ob,
            base_tpl,
            routing_settings,
            bal_cfg,
        )
        if standalone_cfg:
            result.append(standalone_cfg)

    if not result:
        return config_array
    logger.info(
        '[balancer] full: %s groups / %s servers → %s configs',
        len(grouped), len(all_outbounds), len(result),
    )
    return result


async def apply_xray_balancer(
    xray_config: Any,
    *,
    rw_base_url: str | None,
    rw_token: str | None,
    http_client: httpx.AsyncClient | None = None,
    options: BalancerOptions | None = None,
    stats_cache: NodeStatsCache | None = None,
) -> Any:
    """Transform raw Remnawave Xray JSON into grouped balancer configs."""
    config_array = _normalize_config_array(xray_config)
    if not config_array:
        return xray_config

    cache = stats_cache or node_stats_cache
    ns_opts = options.node_stats if options else None
    if ns_opts and ns_opts.enabled and not cache.is_ready() and rw_base_url and rw_token:
        await cache.refresh(
            rw_base_url=rw_base_url,
            rw_token=rw_token,
            http_client=http_client,
            settings=ns_opts,
        )

    hosts = await _fetch_remnawave_hosts(rw_base_url or '', rw_token, http_client)
    groups = resolve_groups(hosts)
    balanced = _build_balanced_configs(
        config_array,
        groups,
        options=options,
        stats_cache=cache,
    )
    if balanced is config_array:
        return xray_config
    return balanced
