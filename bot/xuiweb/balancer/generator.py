"""Xray JSON config builder for one balancer group."""

from __future__ import annotations

import copy
import logging
from typing import Any

from .groups import sanitize_tag, is_lte_outbound
from .lte_tiers import active_lte_tiers, lte_tier_summary, split_lte_by_tier
from .routing import RoutingSettings, build_routing_rules, fallback_routing_rules
from .settings import BalancerSettings, DEFAULT_DNS_SERVERS

logger = logging.getLogger(__name__)

MANAGED_FIELDS = frozenset({
    'remarks', 'meta', 'dns', 'inbounds', 'outbounds', 'routing', 'burstObservatory', 'log',
})


def _clamp_expected(settings: BalancerSettings, selector_len: int) -> int:
    if selector_len <= 0:
        return 1
    return min(settings.load_expected, selector_len)


def _balancer_strategy(
    settings: BalancerSettings,
    selector_len: int,
    *,
    tolerance: float,
    baseline_ms: int | None = None,
) -> dict:
    strategy_settings: dict[str, Any] = {
        'expected': _clamp_expected(settings, selector_len),
        'tolerance': tolerance,
    }
    if baseline_ms is not None and settings.strategy in ('leastLoad', 'leastPing'):
        strategy_settings['baselines'] = [f'{baseline_ms}ms']
    return {'type': settings.strategy, 'settings': strategy_settings}


def _resolve_dns(base_config: dict, cfg: BalancerSettings) -> dict:
    """Admin DNS settings override Remnawave template (servers + queryStrategy)."""
    servers = list(cfg.dns_servers or DEFAULT_DNS_SERVERS)
    query_strategy = cfg.dns_query_strategy or 'UseIP'
    if base_config.get('dns'):
        dns = copy.deepcopy(base_config['dns'])
        dns['servers'] = servers
        dns['queryStrategy'] = query_strategy
        return dns
    return {'servers': servers, 'queryStrategy': query_strategy}


def _resolve_domain_strategy(
    base_config: dict,
    balancer_settings: BalancerSettings | None,
    routing_settings: RoutingSettings | None = None,
) -> str:
    if balancer_settings and balancer_settings.domain_strategy:
        return balancer_settings.domain_strategy
    if routing_settings and routing_settings.domain_strategy:
        return routing_settings.domain_strategy
    return (base_config.get('routing') or {}).get('domainStrategy') or 'IPIfNonMatch'


def prepare_base_template(
    base_config: dict,
    settings: BalancerSettings | None = None,
) -> dict:
    cfg = settings or BalancerSettings()
    inherited = {
        k: copy.deepcopy(v)
        for k, v in base_config.items()
        if k not in MANAGED_FIELDS
    }
    dns = _resolve_dns(base_config, cfg)
    inbounds = copy.deepcopy(base_config['inbounds']) if base_config.get('inbounds') else [
        {
            'tag': 'socks', 'port': 10808, 'listen': '127.0.0.1', 'protocol': 'socks',
            'settings': {'udp': True, 'auth': 'noauth'},
            'sniffing': {'enabled': True, 'routeOnly': False, 'destOverride': ['http', 'tls', 'quic']},
        },
        {
            'tag': 'http', 'port': 10809, 'listen': '127.0.0.1', 'protocol': 'http',
            'settings': {'allowTransparent': False},
            'sniffing': {'enabled': True, 'routeOnly': False, 'destOverride': ['http', 'tls', 'quic']},
        },
    ]
    return {'inherited': inherited, 'dns': dns, 'inbounds': inbounds}


def _make_loopback(prefix: str, kind: str, target_balancer_tag: str) -> dict:
    tag = f'{prefix}-{kind}-loopback'
    inbound_tag = f'{prefix}-{kind}-lb-in'
    return {
        'outbound': {'tag': tag, 'protocol': 'loopback', 'settings': {'inboundTag': inbound_tag}},
        'rule': {'type': 'field', 'inboundTag': [inbound_tag], 'balancerTag': target_balancer_tag},
        'loopback_tag': tag,
    }


def _append_balancer_chain(
    *,
    prefix: str,
    cfg: BalancerSettings,
    steps: list[tuple[str, list[str], int, float]],
    final_balancer_tag: str,
    balancers: list[dict],
    extra_outbounds: list[dict],
    loopback_rules: list[dict],
) -> str:
    """Build bottom-up balancer chain; return entry (first-hop) balancer tag."""
    next_target = final_balancer_tag
    entry_tag = final_balancer_tag
    for kind, tags, baseline_ms, tolerance in reversed(steps):
        bal_tag = f'{prefix}-{kind}-balancer'
        lb = _make_loopback(prefix, kind, next_target)
        extra_outbounds.append(lb['outbound'])
        loopback_rules.append(lb['rule'])
        balancers.append({
            'tag': bal_tag,
            'selector': tags,
            'strategy': _balancer_strategy(
                cfg, len(tags),
                tolerance=tolerance,
                baseline_ms=baseline_ms,
            ),
            'fallbackTag': lb['loopback_tag'],
        })
        next_target = bal_tag
        entry_tag = bal_tag
    return entry_tag


def _baseline_for_trigger(cfg: BalancerSettings, trigger_word: str) -> int:
    for word, baseline_ms in cfg.lte_trigger_tiers:
        if word == trigger_word:
            return baseline_ms
    return cfg.lte_baseline_ms


def _build_lte_entry_balancer(
    *,
    prefix: str,
    cfg: BalancerSettings,
    lte_outbounds: list[dict],
    all_selector_tags: list[str],
    balancers: list[dict],
    extra_outbounds: list[dict],
    loopback_rules: list[dict],
) -> str:
    """One cascade hop per LTE trigger (priority = list order) → all. Returns entry balancer tag."""
    lte_tags = [o['tag'] for o in lte_outbounds]
    all_balancer_tag = f'{prefix}-all-balancer'
    tiers_cfg = cfg.lte_trigger_tiers

    balancers.append({
        'tag': all_balancer_tag,
        'selector': all_selector_tags,
        'strategy': _balancer_strategy(cfg, len(all_selector_tags), tolerance=cfg.bal_tolerance_fb),
    })

    buckets = split_lte_by_tier(lte_outbounds, tiers_cfg)
    tier_keys = active_lte_tiers(buckets, tiers_cfg)

    if len(tier_keys) <= 1:
        trigger = tier_keys[0] if tier_keys else (tiers_cfg[0][0] if tiers_cfg else 'LTE')
        lte_bal_tag = f'{prefix}-lte-balancer'
        lb = _make_loopback(prefix, 'lte', all_balancer_tag)
        extra_outbounds.append(lb['outbound'])
        loopback_rules.append(lb['rule'])
        balancers.append({
            'tag': lte_bal_tag,
            'selector': lte_tags,
            'strategy': _balancer_strategy(
                cfg, len(lte_tags),
                tolerance=cfg.bal_tolerance_fb,
                baseline_ms=_baseline_for_trigger(cfg, trigger),
            ),
            'fallbackTag': lb['loopback_tag'],
        })
        return lte_bal_tag

    baseline_map = {word: ms for word, ms in tiers_cfg}
    steps: list[tuple[str, list[str], int, float]] = []
    for idx, trigger in enumerate(tier_keys):
        tags = [o['tag'] for o in buckets[trigger]]
        steps.append((
            f'lte-{sanitize_tag(trigger)}',
            tags,
            baseline_map.get(trigger, _baseline_for_trigger(cfg, trigger)),
            cfg.bal_tolerance if idx == 0 else cfg.bal_tolerance_fb,
        ))

    entry = _append_balancer_chain(
        prefix=prefix,
        cfg=cfg,
        steps=steps,
        final_balancer_tag=all_balancer_tag,
        balancers=balancers,
        extra_outbounds=extra_outbounds,
        loopback_rules=loopback_rules,
    )
    summary = lte_tier_summary(lte_outbounds, tiers_cfg)
    logger.info('LTE trigger tiers "%s": %s', prefix, summary)
    return entry


def build_group_config(
    base_config: dict,
    group_name: str,
    outbounds: list[dict] | None,
    fallback_outbounds: list[dict] | None = None,
    base_tpl: dict | None = None,
    routing_settings: RoutingSettings | None = None,
    balancer_settings: BalancerSettings | None = None,
) -> dict | None:
    if not outbounds and not fallback_outbounds:
        return None

    cfg = balancer_settings or BalancerSettings()
    tpl = base_tpl or prepare_base_template(base_config, cfg)
    prefix = sanitize_tag(group_name)

    lte_triggers = cfg.lte_triggers
    inline_main = [o for o in (outbounds or []) if not is_lte_outbound(o['tag'], lte_triggers)]
    inline_lte = [o for o in (outbounds or []) if is_lte_outbound(o['tag'], lte_triggers)]
    external_lte = list(fallback_outbounds or [])

    main = inline_main
    lte = external_lte + inline_lte
    lte_only = not main and bool(lte)

    if lte_only:
        main_tags: list[str] = []
        lte_tags = [o['tag'] for o in lte]
        all_tags = lte_tags
    else:
        if not main and lte:
            main = lte
            lte = []
        if not main:
            return None
        all_outbounds = main + lte
        main_tags = [o['tag'] for o in main]
        lte_tags = [o['tag'] for o in lte]
        all_tags = [o['tag'] for o in all_outbounds]

    balancers: list[dict] = []
    extra_outbounds: list[dict] = []
    loopback_rules: list[dict] = []

    main_balancer_tag = f'{prefix}-balancer'

    if lte_only:
        entry_balancer_tag = _build_lte_entry_balancer(
            prefix=prefix,
            cfg=cfg,
            lte_outbounds=lte,
            all_selector_tags=lte_tags,
            balancers=balancers,
            extra_outbounds=extra_outbounds,
            loopback_rules=loopback_rules,
        )
        cfg_outbounds_source = lte
    elif lte:
        all_combined = main_tags + lte_tags
        lte_entry = _build_lte_entry_balancer(
            prefix=prefix,
            cfg=cfg,
            lte_outbounds=lte,
            all_selector_tags=all_combined,
            balancers=balancers,
            extra_outbounds=extra_outbounds,
            loopback_rules=loopback_rules,
        )
        lb_to_lte = _make_loopback(prefix, 'main', lte_entry)
        extra_outbounds.append(lb_to_lte['outbound'])
        loopback_rules.append(lb_to_lte['rule'])
        balancers.append({
            'tag': main_balancer_tag,
            'selector': main_tags,
            'strategy': _balancer_strategy(
                cfg, len(main_tags),
                tolerance=cfg.bal_tolerance,
                baseline_ms=cfg.main_lte_baseline_ms,
            ),
            'fallbackTag': lb_to_lte['loopback_tag'],
        })
        entry_balancer_tag = main_balancer_tag
        cfg_outbounds_source = main + lte
        logger.debug('LTE group "%s": main=%s lte=%s', group_name, len(main), len(lte))
    else:
        balancers.append({
            'tag': main_balancer_tag,
            'selector': main_tags,
            'strategy': _balancer_strategy(
                cfg, len(main_tags),
                tolerance=cfg.bal_tolerance_fb,
                baseline_ms=cfg.single_baseline_ms,
            ),
        })
        entry_balancer_tag = main_balancer_tag
        cfg_outbounds_source = main

    cfg_outbounds = copy.deepcopy(cfg_outbounds_source) + extra_outbounds + [
        {'tag': 'direct', 'protocol': 'freedom'},
        {'tag': 'block', 'protocol': 'blackhole'},
    ]

    result: dict[str, Any] = {
        'remarks': group_name,
        **copy.deepcopy(tpl['inherited']),
        'dns': copy.deepcopy(tpl['dns']),
        'inbounds': copy.deepcopy(tpl['inbounds']),
        'outbounds': cfg_outbounds,
        'burstObservatory': {
            'subjectSelector': all_tags,
            'pingConfig': {
                'destination': cfg.probe_url,
                'interval': cfg.probe_interval,
                'sampling': cfg.probe_sampling,
                'timeout': cfg.probe_timeout,
            },
        },
    }

    if base_config.get('meta'):
        result['meta'] = copy.deepcopy(base_config['meta'])

    if routing_settings and routing_settings.enabled:
        routing_rules = build_routing_rules(routing_settings, entry_balancer_tag)
    else:
        routing_rules = fallback_routing_rules(entry_balancer_tag)
    if loopback_rules:
        routing_rules = loopback_rules + routing_rules

    result['routing'] = {
        'domainStrategy': _resolve_domain_strategy(base_config, cfg, routing_settings),
        'domainMatcher': 'hybrid',
        'balancers': balancers,
        'rules': routing_rules,
    }
    return result


def _routing_rules_for_outbound(
    routing_settings: RoutingSettings | None,
    outbound_tag: str,
) -> list[dict]:
    if routing_settings and routing_settings.enabled:
        rules = build_routing_rules(routing_settings, outbound_tag)
    else:
        rules = fallback_routing_rules(outbound_tag)
    fixed: list[dict] = []
    for rule in rules:
        nr = dict(rule)
        if 'balancerTag' in nr:
            nr['outboundTag'] = nr.pop('balancerTag')
        fixed.append(nr)
    return fixed


def build_standalone_config(
    base_config: dict,
    display_name: str,
    outbound: dict,
    base_tpl: dict | None = None,
    routing_settings: RoutingSettings | None = None,
    balancer_settings: BalancerSettings | None = None,
) -> dict | None:
    """Single-server config without AUTO/country balancer (no-auto hosts)."""
    cfg = balancer_settings or BalancerSettings()
    tpl = base_tpl or prepare_base_template(base_config, cfg)
    proxy = copy.deepcopy(outbound)
    proxy_tag = proxy.get('tag')
    if not proxy_tag:
        return None

    cfg_outbounds = [proxy] + [
        {'tag': 'direct', 'protocol': 'freedom'},
        {'tag': 'block', 'protocol': 'blackhole'},
    ]

    result: dict[str, Any] = {
        'remarks': display_name,
        **copy.deepcopy(tpl['inherited']),
        'dns': copy.deepcopy(tpl['dns']),
        'inbounds': copy.deepcopy(tpl['inbounds']),
        'outbounds': cfg_outbounds,
        'routing': {
            'domainStrategy': _resolve_domain_strategy(base_config, cfg, routing_settings),
            'domainMatcher': 'hybrid',
            'rules': _routing_rules_for_outbound(routing_settings, proxy_tag),
        },
    }
    if base_config.get('meta'):
        result['meta'] = copy.deepcopy(base_config['meta'])
    return result
