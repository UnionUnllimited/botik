"""Xray routing rules for balancer JSON configs."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ROUTE_ORDER = 'block-proxy-direct'
PRIVATE_IP_RANGES = [
    '127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
    '169.254.0.0/16', '::1/128', 'fc00::/7', 'fe80::/10',
]


@dataclass
class RoutingSettings:
    enabled: bool = False
    route_order: str = DEFAULT_ROUTE_ORDER
    udp_block_quic: bool = True
    block_sites: list[str] = field(default_factory=list)
    block_ip: list[str] = field(default_factory=list)
    proxy_sites: list[str] = field(default_factory=list)
    proxy_ip: list[str] = field(default_factory=list)
    direct_sites: list[str] = field(default_factory=list)
    direct_ip: list[str] = field(default_factory=list)
    domain_strategy: str = 'IPIfNonMatch'


def _parse_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    return []


def parse_routing_config(raw: str | None) -> RoutingSettings:
    if not raw or not str(raw).strip():
        return RoutingSettings()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning('[routing] invalid JSON config: %s', exc)
        return RoutingSettings()
    if not isinstance(data, dict):
        return RoutingSettings()

    return RoutingSettings(
        enabled=bool(data.get('enabled')),
        route_order=str(data.get('route_order') or DEFAULT_ROUTE_ORDER),
        udp_block_quic=bool(data.get('udp_block_quic', True)),
        block_sites=_parse_lines(data.get('block_sites')),
        block_ip=_parse_lines(data.get('block_ip')),
        proxy_sites=_parse_lines(data.get('proxy_sites')),
        proxy_ip=_parse_lines(data.get('proxy_ip')),
        direct_sites=_parse_lines(data.get('direct_sites')),
        direct_ip=_parse_lines(data.get('direct_ip')),
        domain_strategy=str(
            data.get('domain_strategy') or data.get('domainStrategy') or 'IPIfNonMatch'
        ),
    )


def routing_config_to_json(settings: RoutingSettings) -> str:
    return json.dumps(
        {
            'enabled': settings.enabled,
            'route_order': settings.route_order,
            'udp_block_quic': settings.udp_block_quic,
            'block_sites': settings.block_sites,
            'block_ip': settings.block_ip,
            'proxy_sites': settings.proxy_sites,
            'proxy_ip': settings.proxy_ip,
            'direct_sites': settings.direct_sites,
            'direct_ip': settings.direct_ip,
            'domain_strategy': settings.domain_strategy,
        },
        ensure_ascii=False,
    )


def build_routing_rules(settings: RoutingSettings, balancer_tag: str) -> list[dict]:
    """Build Xray routing.rules; catch-all traffic goes through balancer_tag."""
    rules: list[dict] = []

    rules.append({'type': 'field', 'protocol': ['bittorrent'], 'outboundTag': 'direct'})
    rules.append({'type': 'field', 'ip': list(PRIVATE_IP_RANGES), 'outboundTag': 'direct'})

    if settings.udp_block_quic:
        rules.append({'type': 'field', 'network': 'udp', 'port': '443', 'outboundTag': 'block'})

    order = [part.strip() for part in (settings.route_order or DEFAULT_ROUTE_ORDER).split('-') if part.strip()]
    if not order:
        order = DEFAULT_ROUTE_ORDER.split('-')

    for step in order:
        if step == 'block':
            if settings.block_sites:
                rules.append({'type': 'field', 'domain': list(settings.block_sites), 'outboundTag': 'block'})
            if settings.block_ip:
                rules.append({'type': 'field', 'ip': list(settings.block_ip), 'outboundTag': 'block'})
        elif step == 'proxy':
            if settings.proxy_sites:
                rules.append({'type': 'field', 'domain': list(settings.proxy_sites), 'balancerTag': balancer_tag})
            if settings.proxy_ip:
                rules.append({'type': 'field', 'ip': list(settings.proxy_ip), 'balancerTag': balancer_tag})
        elif step == 'direct':
            if settings.direct_sites:
                rules.append({'type': 'field', 'domain': list(settings.direct_sites), 'outboundTag': 'direct'})
            if settings.direct_ip:
                rules.append({'type': 'field', 'ip': list(settings.direct_ip), 'outboundTag': 'direct'})

    rules.append({'type': 'field', 'network': 'tcp,udp', 'balancerTag': balancer_tag})
    return rules


def fallback_routing_rules(balancer_tag: str) -> list[dict]:
    return build_routing_rules(RoutingSettings(enabled=False), balancer_tag)
