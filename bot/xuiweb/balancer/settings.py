"""Configurable balancer parameters (loaded from DB / admin)."""

from __future__ import annotations

from dataclasses import dataclass, field

VALID_STRATEGIES = frozenset({'leastLoad', 'random', 'roundRobin', 'leastPing'})
VALID_DOMAIN_STRATEGIES = frozenset({'AsIs', 'IPIfNonMatch', 'IPOnDemand'})
DEFAULT_DOMAIN_STRATEGY = 'IPIfNonMatch'

DEFAULT_AUTO_GROUP_NAME = '🇪🇺 AUTO | Самые быстрые'
DEFAULT_LTE_AUTO_GROUP_NAME = '🇷🇺 LTE AUTO | Самые быстрые'
DEFAULT_STRATEGY = 'leastLoad'
DEFAULT_PROBE_URL = 'https://www.gstatic.com/generate_204'
DEFAULT_PROBE_INTERVAL = '3m'
DEFAULT_PROBE_SAMPLING = 3
DEFAULT_PROBE_TIMEOUT = '3s'
DEFAULT_BAL_TOLERANCE = 0.5
DEFAULT_BAL_TOLERANCE_FB = 0.8
DEFAULT_LOAD_EXPECTED = 3
DEFAULT_MAIN_LTE_BASELINE_MS = 500
DEFAULT_SINGLE_BASELINE_MS = 800
DEFAULT_LTE_BASELINE_MS = 1500
DEFAULT_LTE_PLUS_BASELINE_MS = 900
DEFAULT_LTE_MINUS_BASELINE_MS = 2800
DEFAULT_DNS_SERVERS = ('1.1.1.1', '1.0.0.1')
VALID_DNS_QUERY_STRATEGIES = frozenset({'AsIs', 'UseIP', 'UseIPv4', 'UseIPv6'})
DEFAULT_DNS_QUERY_STRATEGY = 'UseIP'
DEFAULT_LTE_TRIGGERS = ('LTE',)

BALANCER_MODE_FULL = 'full'
BALANCER_MODE_AUTO_ONLY = 'auto_only'
VALID_BALANCER_MODES = frozenset({BALANCER_MODE_FULL, BALANCER_MODE_AUTO_ONLY})
DEFAULT_BALANCER_MODE = BALANCER_MODE_FULL


@dataclass
class BalancerSettings:
    mode: str = DEFAULT_BALANCER_MODE
    strategy: str = DEFAULT_STRATEGY
    auto_group_name: str = DEFAULT_AUTO_GROUP_NAME
    lte_auto_group_name: str = DEFAULT_LTE_AUTO_GROUP_NAME
    probe_url: str = DEFAULT_PROBE_URL
    probe_interval: str = DEFAULT_PROBE_INTERVAL
    probe_sampling: int = DEFAULT_PROBE_SAMPLING
    probe_timeout: str = DEFAULT_PROBE_TIMEOUT
    bal_tolerance: float = DEFAULT_BAL_TOLERANCE
    bal_tolerance_fb: float = DEFAULT_BAL_TOLERANCE_FB
    load_expected: int = DEFAULT_LOAD_EXPECTED
    main_lte_baseline_ms: int = DEFAULT_MAIN_LTE_BASELINE_MS
    single_baseline_ms: int = DEFAULT_SINGLE_BASELINE_MS
    lte_baseline_ms: int = DEFAULT_LTE_BASELINE_MS
    lte_plus_baseline_ms: int = DEFAULT_LTE_PLUS_BASELINE_MS
    lte_minus_baseline_ms: int = DEFAULT_LTE_MINUS_BASELINE_MS
    dns_servers: list[str] = field(default_factory=lambda: list(DEFAULT_DNS_SERVERS))
    dns_query_strategy: str = DEFAULT_DNS_QUERY_STRATEGY
    domain_strategy: str = DEFAULT_DOMAIN_STRATEGY
    lte_trigger_tiers: list[tuple[str, int]] = field(
        default_factory=lambda: [('LTE', DEFAULT_LTE_BASELINE_MS)],
    )
    auto_exclude_patterns: list[str] = field(default_factory=list)

    @property
    def lte_triggers(self) -> list[str]:
        return [word for word, _ in self.lte_trigger_tiers if str(word).strip()]


def _parse_float(val: str | None, default: float) -> float:
    try:
        if val is None or str(val).strip() == '':
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _parse_int(val: str | None, default: int, *, minimum: int = 1) -> int:
    try:
        if val is None or str(val).strip() == '':
            return default
        n = int(float(val))
        return max(minimum, n)
    except (TypeError, ValueError):
        return default


def _parse_dns_servers(raw: str | None) -> list[str]:
    if not raw or not str(raw).strip():
        return list(DEFAULT_DNS_SERVERS)
    parts = []
    for chunk in str(raw).replace('\n', ',').split(','):
        item = chunk.strip()
        if item:
            parts.append(item)
    return parts or list(DEFAULT_DNS_SERVERS)


def _parse_dns_query_strategy(val: str | None) -> str:
    strategy = str(val or DEFAULT_DNS_QUERY_STRATEGY).strip()
    if strategy not in VALID_DNS_QUERY_STRATEGIES:
        return DEFAULT_DNS_QUERY_STRATEGY
    return strategy


def _parse_domain_strategy(val: str | None) -> str:
    strategy = str(val or DEFAULT_DOMAIN_STRATEGY).strip()
    if strategy not in VALID_DOMAIN_STRATEGIES:
        return DEFAULT_DOMAIN_STRATEGY
    return strategy


def _parse_word_list(raw: str | None, *, default: list[str] | tuple[str, ...] | None = None) -> list[str]:
    if raw is None or not str(raw).strip():
        return list(default) if default else []
    parts: list[str] = []
    for chunk in str(raw).replace('\n', ',').split(','):
        item = chunk.strip()
        if item:
            parts.append(item)
    if not parts and default:
        return list(default)
    return parts


def _parse_balancer_mode(val: str | None) -> str:
    mode = str(val or DEFAULT_BALANCER_MODE).strip()
    if mode not in VALID_BALANCER_MODES:
        return DEFAULT_BALANCER_MODE
    return mode


def _parse_lte_trigger_tiers(
    raw: str | None,
    *,
    fallback_baselines: tuple[int, int, int],
    default_baseline: int,
) -> list[tuple[str, int]]:
    """
    Parse LTE trigger list with optional per-trigger baseline.

    Examples:
      LTE:900, MOBILE:1500, 4G:2800
      LTE, MOBILE          → baselines from fallbacks by position (900, 1500, 2800…)
    """
    parts = _parse_word_list(raw, default=list(DEFAULT_LTE_TRIGGERS))
    if not parts:
        return [('LTE', default_baseline)]
    fb = list(fallback_baselines)
    result: list[tuple[str, int]] = []
    for i, part in enumerate(parts):
        chunk = str(part).strip()
        if not chunk:
            continue
        if ':' in chunk:
            word, ms_raw = chunk.rsplit(':', 1)
            word = word.strip()
            ms = _parse_int(ms_raw.strip(), fb[i] if i < len(fb) else default_baseline, minimum=1)
        else:
            word = chunk
            ms = fb[i] if i < len(fb) else default_baseline
        if word:
            result.append((word, ms))
    return result or [('LTE', default_baseline)]


def parse_balancer_settings(raw: dict[str, str | None] | None = None) -> BalancerSettings:
    data = raw or {}
    strategy = str(data.get('xray_balancer_strategy') or DEFAULT_STRATEGY).strip()
    if strategy not in VALID_STRATEGIES:
        strategy = DEFAULT_STRATEGY
    auto_name = str(data.get('xray_balancer_auto_group_name') or '').strip() or DEFAULT_AUTO_GROUP_NAME
    lte_name = str(data.get('xray_balancer_lte_auto_group_name') or '').strip() or DEFAULT_LTE_AUTO_GROUP_NAME
    lte_plus = _parse_int(data.get('xray_balancer_lte_plus_baseline_ms'), DEFAULT_LTE_PLUS_BASELINE_MS, minimum=1)
    lte_std = _parse_int(data.get('xray_balancer_lte_baseline_ms'), DEFAULT_LTE_BASELINE_MS, minimum=1)
    lte_minus = _parse_int(data.get('xray_balancer_lte_minus_baseline_ms'), DEFAULT_LTE_MINUS_BASELINE_MS, minimum=1)
    lte_trigger_tiers = _parse_lte_trigger_tiers(
        data.get('xray_balancer_lte_triggers'),
        fallback_baselines=(lte_plus, lte_std, lte_minus),
        default_baseline=lte_std,
    )
    return BalancerSettings(
        mode=_parse_balancer_mode(data.get('xray_balancer_mode')),
        strategy=strategy,
        auto_group_name=auto_name,
        lte_auto_group_name=lte_name,
        probe_url=str(data.get('xray_balancer_probe_url') or DEFAULT_PROBE_URL).strip() or DEFAULT_PROBE_URL,
        probe_interval=str(data.get('xray_balancer_probe_interval') or DEFAULT_PROBE_INTERVAL).strip() or DEFAULT_PROBE_INTERVAL,
        probe_sampling=_parse_int(data.get('xray_balancer_probe_sampling'), DEFAULT_PROBE_SAMPLING, minimum=1),
        probe_timeout=str(data.get('xray_balancer_probe_timeout') or DEFAULT_PROBE_TIMEOUT).strip() or DEFAULT_PROBE_TIMEOUT,
        bal_tolerance=_parse_float(data.get('xray_balancer_tolerance'), DEFAULT_BAL_TOLERANCE),
        bal_tolerance_fb=_parse_float(data.get('xray_balancer_tolerance_fallback'), DEFAULT_BAL_TOLERANCE_FB),
        load_expected=_parse_int(data.get('xray_balancer_load_expected'), DEFAULT_LOAD_EXPECTED, minimum=1),
        main_lte_baseline_ms=_parse_int(data.get('xray_balancer_main_lte_baseline_ms'), DEFAULT_MAIN_LTE_BASELINE_MS, minimum=1),
        single_baseline_ms=_parse_int(data.get('xray_balancer_single_baseline_ms'), DEFAULT_SINGLE_BASELINE_MS, minimum=1),
        lte_baseline_ms=_parse_int(data.get('xray_balancer_lte_baseline_ms'), DEFAULT_LTE_BASELINE_MS, minimum=1),
        lte_plus_baseline_ms=_parse_int(data.get('xray_balancer_lte_plus_baseline_ms'), DEFAULT_LTE_PLUS_BASELINE_MS, minimum=1),
        lte_minus_baseline_ms=_parse_int(data.get('xray_balancer_lte_minus_baseline_ms'), DEFAULT_LTE_MINUS_BASELINE_MS, minimum=1),
        dns_servers=_parse_dns_servers(data.get('xray_balancer_dns_servers')),
        dns_query_strategy=_parse_dns_query_strategy(data.get('xray_balancer_dns_query_strategy')),
        domain_strategy=_parse_domain_strategy(data.get('xray_balancer_domain_strategy')),
        lte_trigger_tiers=lte_trigger_tiers,
        auto_exclude_patterns=_parse_word_list(data.get('xray_balancer_auto_exclude')),
    )
