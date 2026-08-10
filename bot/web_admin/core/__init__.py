"""
Core модули для веб-админки
"""
from .user_limit import update_user_device_limit
from .server_health import (
    check_remnawave,
    RemnawaveHealth,
)
from .revoke import (
    revoke_subscription,
    preflight_check,
    PreflightResult,
    RevokeResult,
    SubscriptionClass,
    ClientModeInfo,
    REVOKE_TOTAL_STEPS,
)
from .ip_whitelist import (
    parse_cidr_list,
    resolve_client_ip,
    is_ip_allowed,
    is_loopback,
    get_whitelist_state,
    invalidate_cache as invalidate_whitelist_cache,
)

__all__ = [
    'update_user_device_limit',
    'check_remnawave',
    'RemnawaveHealth',
    'revoke_subscription', 'preflight_check',
    'PreflightResult', 'RevokeResult', 'SubscriptionClass', 'ClientModeInfo',
    'REVOKE_TOTAL_STEPS',
    'parse_cidr_list', 'resolve_client_ip', 'is_ip_allowed', 'is_loopback',
    'get_whitelist_state', 'invalidate_whitelist_cache',
]
