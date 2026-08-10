"""Единый порядок и доступность платёжных методов (бот + кабинет)."""

from __future__ import annotations

import json
from typing import Any, Callable, Literal

from app_config import app_conf

Scope = Literal['bot', 'site']

SETTINGS_ORDER_KEY = 'payment_methods_order'

ConfGetter = Callable[[str, Any], Any]

# show_key, btn_key (bot), scopes, проверка credentials
PAYMENT_METHODS: dict[str, dict[str, Any]] = {
    'yookassa': {
        'show_key': 'show_payment_yookassa',
        'btn_key': 'btn_payment_yookassa',
        'scopes': ('bot', 'site'),
        'requires': lambda g: str(g('show_payment_yookassa', '1')) == '1'
            and bool(g('yookassa_shop_id')) and bool(g('yookassa_secret_key')),
    },
    'platega': {
        'show_key': 'show_payment_platega',
        'btn_key': 'btn_payment_platega',
        'scopes': ('bot', 'site'),
        'requires': lambda g: str(g('show_payment_platega', '0')) == '1'
            and bool(g('platega_merchant_id')) and bool(g('platega_api_secret')),
    },
    'wata': {
        'show_key': 'show_payment_wata',
        'btn_key': 'btn_payment_wata',
        'scopes': ('bot', 'site'),
        'requires': lambda g: str(g('show_payment_wata', '0')) == '1'
            and bool(g('wata_access_token')),
    },
    'yoomoney': {
        'show_key': 'show_payment_yoomoney',
        'btn_key': 'btn_payment_yoomoney',
        'scopes': ('bot', 'site'),
        'requires': lambda g: str(g('show_payment_yoomoney', '1')) == '1'
            and bool(g('yoomoney_account')) and bool(g('yoomoney_notification_secret')),
    },
    'tgstar': {
        'show_key': 'show_payment_tgstar',
        'btn_key': 'btn_payment_tgstar',
        'scopes': ('bot',),
        'requires': lambda g: str(g('show_payment_tgstar', '1')) == '1',
    },
    'cryptobot': {
        'show_key': 'show_payment_cryptobot',
        'btn_key': 'btn_payment_cryptobot',
        'scopes': ('bot',),
        'requires': lambda g: str(g('show_payment_cryptobot', '1')) == '1'
            and bool(g('cryptobot_token')),
    },
    'manual': {
        'show_key': 'show_payment_manual',
        'btn_key': 'btn_payment_manual',
        'scopes': ('bot',),
        'requires': lambda g: str(g('show_payment_manual', '0')) == '1',
    },
    'promo': {
        'show_key': 'show_payment_promo_code',
        'btn_key': 'btn_activate_code',
        'scopes': ('bot',),
        'requires': lambda g: str(g('show_payment_promo_code', '1')) == '1',
    },
}

# Актуальный порядок в боте (wata/platega сверху через insert(0))
DEFAULT_BOT_ORDER: list[str] = [
    'wata', 'platega', 'yookassa', 'tgstar', 'cryptobot', 'yoomoney', 'manual', 'promo',
]
DEFAULT_SITE_ORDER: list[str] = ['yookassa', 'platega', 'yoomoney', 'wata']

PAYMENT_METHOD_LABELS: dict[str, str] = {
    'yookassa': 'YooKassa',
    'platega': 'Platega (СБП)',
    'wata': 'Wata',
    'yoomoney': 'YooMoney',
    'tgstar': 'Telegram Stars',
    'cryptobot': 'CryptoBot',
    'manual': 'CloudTips / перевод',
    'promo': 'Оплатить кодом',
}


def _make_getter(conf: dict[str, Any] | None) -> ConfGetter:
    if conf is not None:
        def getter(key: str, default: Any = '') -> Any:
            val = conf.get(key)
            if val is None:
                return default
            if isinstance(val, tuple):
                return val[0]
            return val
        return getter
    return app_conf.get


def parse_payment_order(raw: str | list | None, scope: Scope = 'bot') -> list[str]:
    """Разбирает JSON-порядок из settings; неизвестные ключи отбрасываются."""
    default = DEFAULT_SITE_ORDER if scope == 'site' else DEFAULT_BOT_ORDER
    if not raw:
        return list(default)
    try:
        order = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(order, list):
            return list(default)
    except (json.JSONDecodeError, TypeError):
        return list(default)

    valid = set(PAYMENT_METHODS.keys())
    seen: set[str] = set()
    result: list[str] = []
    for item in order:
        mid = str(item).strip().lower()
        if mid not in valid or mid in seen:
            continue
        if scope == 'site' and 'site' not in PAYMENT_METHODS[mid]['scopes']:
            continue
        result.append(mid)
        seen.add(mid)

    for mid in default:
        if mid not in seen:
            if scope == 'site' and 'site' not in PAYMENT_METHODS[mid]['scopes']:
                continue
            result.append(mid)
            seen.add(mid)
    return result


def is_payment_method_available(method_id: str, conf: dict[str, Any] | None = None) -> bool:
    meta = PAYMENT_METHODS.get(method_id)
    if not meta:
        return False
    getter = _make_getter(conf)
    try:
        return bool(meta['requires'](getter))
    except Exception:
        return False


def get_ordered_payment_methods(
    scope: Scope = 'bot',
    conf: dict[str, Any] | None = None,
) -> list[str]:
    """Включённые методы в порядке из settings (только реально доступные)."""
    getter = _make_getter(conf)
    raw = getter(SETTINGS_ORDER_KEY, '')
    order = parse_payment_order(raw, scope=scope)
    return [m for m in order if is_payment_method_available(m, conf)]


def build_bot_payment_keyboard_rows(
    callback_for: Callable[[str], str | None],
    conf: dict[str, Any] | None = None,
    *,
    exclude: frozenset[str] | None = None,
) -> list[list]:
    """Строки inline-клавиатуры для выбора способа оплаты в боте."""
    from button_helpers import btn

    skip = exclude or frozenset()
    rows: list[list] = []
    for mid in get_ordered_payment_methods('bot', conf):
        if mid in skip:
            continue
        cb = callback_for(mid)
        if not cb:
            continue
        btn_key = PAYMENT_METHODS[mid]['btn_key']
        rows.append([btn(btn_key, callback_data=cb)])
    return rows


def payment_order_for_admin(scope: Scope = 'bot') -> list[dict[str, Any]]:
    """Список методов для UI админки (все из реестра scope, с флагом enabled)."""
    raw = app_conf.get(SETTINGS_ORDER_KEY, '')
    order = parse_payment_order(raw, scope=scope)
    result = []
    for mid in order:
        if scope == 'site' and 'site' not in PAYMENT_METHODS[mid]['scopes']:
            continue
        result.append({
            'id': mid,
            'label': PAYMENT_METHOD_LABELS.get(mid, mid),
            'enabled': is_payment_method_available(mid),
            'scope': scope,
        })
    return result
