"""Build host-balancer groups for web admin (same rules as link_dedup_grouping)."""

from __future__ import annotations

from typing import Any

from link_dedup_grouping import (
    base_name_for_grouping,
    extract_trailing_index,
    is_pool_balanced,
    platform_tags_key,
    pool_key_from_remark,
)


def _host_visible_in_squad(host: dict, squad_uuid: str) -> bool:
    excluded = host.get('excluded_internal_squads') or host.get('excludedInternalSquads') or []
    ex_set = {str(x).lower() for x in excluded}
    return str(squad_uuid).lower() not in ex_set


def _squads_for_host(host: dict, squads: list[dict]) -> list[dict]:
    out = []
    for sq in squads or []:
        uid = sq.get('uuid')
        if uid and _host_visible_in_squad(host, str(uid)):
            out.append({'uuid': str(uid), 'name': sq.get('name') or '—'})
    return out


def _resolve_node(nodes_by_uuid: dict[str, dict], node_uuids: list) -> dict | None:
    for uid in node_uuids or []:
        n = nodes_by_uuid.get(str(uid).lower())
        if n:
            return n
    return None


def _enrich_member(host: dict, nodes_by_uuid: dict[str, dict], online_threshold: int) -> dict:
    node_uuids = [str(x) for x in (host.get('nodes') or [])]
    node = _resolve_node(nodes_by_uuid, node_uuids)
    has_nodes = bool(node_uuids)
    users_online = 0
    node_name = None
    node_uuid = None
    node_ok = True
    if node:
        node_uuid = node.get('uuid')
        node_name = node.get('name')
        users_online = int(node.get('users_online') or 0)
        if node.get('is_disabled'):
            node_ok = False
        elif not node.get('is_connected'):
            node_ok = False
        elif has_nodes and users_online > online_threshold:
            node_ok = False
    member = {
        'uuid': host.get('uuid'),
        'remark': host.get('remark') or '',
        'address': host.get('address') or '',
        'port': host.get('port') or 0,
        'is_disabled': bool(host.get('is_disabled')),
        'index': extract_trailing_index(host.get('remark') or ''),
        'nodes': node_uuids,
        'node_uuid': node_uuid,
        'node_name': node_name,
        'users_online': users_online,
        'has_nodes': has_nodes,
        'node_ok': node_ok if has_nodes else True,
        'inbound_tag': None,
    }
    inbound = host.get('inbound') or {}
    if isinstance(inbound, dict):
        member['inbound_profile_uuid'] = inbound.get('configProfileUuid') or inbound.get('config_profile_uuid')
        member['inbound_uuid'] = inbound.get('configProfileInboundUuid') or inbound.get('config_profile_inbound_uuid')
    return member


def build_host_balancer_groups(
    hosts: list[dict],
    *,
    squads: list[dict] | None = None,
    nodes: list[dict] | None = None,
    squad_filter: str | None = None,
    platform_filter: str | None = None,
    view: str = 'all',
    online_threshold: int = 100,
) -> dict[str, Any]:
    squads = squads or []
    nodes_by_uuid: dict[str, dict] = {}
    for n in nodes or []:
        uid = str(n.get('uuid') or '').lower()
        if uid:
            nodes_by_uuid[uid] = n

    groups_map: dict[str, list[dict]] = {}
    order: list[str] = []

    for h in hosts or []:
        if not isinstance(h, dict):
            continue
        if h.get('is_hidden') or h.get('isHidden'):
            continue
        remark = h.get('remark') or ''
        if platform_filter:
            tags = platform_tags_key(remark)
            if platform_filter == 'none' and tags:
                continue
            if platform_filter != 'none' and platform_filter not in (tags or '').split(','):
                continue
        if squad_filter and not _host_visible_in_squad(h, squad_filter):
            continue
        key = pool_key_from_remark(remark)
        if key not in groups_map:
            groups_map[key] = []
            order.append(key)
        groups_map[key].append(h)

    pools: list[dict] = []
    singles: list[dict] = []

    for key in order:
        raw_members = groups_map[key]
        canonical = base_name_for_grouping(raw_members[0].get('remark') or '')
        members = [
            _enrich_member(h, nodes_by_uuid, online_threshold)
            for h in sorted(
                raw_members,
                key=lambda x: extract_trailing_index(x.get('remark') or '') or 0,
            )
        ]
        squad_names: dict[str, str] = {}
        for h in raw_members:
            for sq in _squads_for_host(h, squads):
                squad_names[sq['uuid']] = sq['name']
        pool = {
            'key': key,
            'canonical_name': canonical,
            'platform_tags': platform_tags_key(raw_members[0].get('remark') or ''),
            'client_name': canonical,
            'member_count': len(members),
            'is_balanced': is_pool_balanced(members),
            'members': members,
            'squads': [{'uuid': u, 'name': n} for u, n in squad_names.items()],
        }
        if pool['is_balanced']:
            pools.append(pool)
        else:
            singles.append(pool)

    if view == 'pools':
        singles = []
    elif view == 'singles':
        pools = []

    return {
        'pools': pools,
        'singles': singles,
        'stats': {
            'total_hosts': len(hosts or []),
            'pool_count': len(pools),
            'single_count': len(singles),
            'balanced_members': sum(p['member_count'] for p in pools),
        },
        'hosts_catalog': build_hosts_catalog(hosts, nodes_by_uuid),
    }


def build_hosts_catalog(hosts: list[dict], nodes_by_uuid: dict[str, dict]) -> list[dict]:
    """Плоский список хостов для выбора «дубликат» в модалке."""
    out: list[dict] = []
    for h in hosts or []:
        if not isinstance(h, dict):
            continue
        if h.get('is_hidden') or h.get('isHidden'):
            continue
        node_uuids = [str(x) for x in (h.get('nodes') or [])]
        node = _resolve_node(nodes_by_uuid, node_uuids)
        out.append({
            'uuid': str(h.get('uuid') or ''),
            'remark': h.get('remark') or '',
            'address': h.get('address') or '',
            'port': int(h.get('port') or 0),
            'has_nodes': bool(node_uuids),
            'node_uuid': str(node.get('uuid')) if node else None,
            'node_name': node.get('name') if node else None,
        })
    out.sort(key=lambda x: (x.get('remark') or '').casefold())
    return out
