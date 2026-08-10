"""Белый список доменов email для website и бота.

Чистая логика без зависимостей от web_admin / aiogram.
Настройки хранятся в settings.website_cabinet_config (JSON).
"""
from __future__ import annotations

import json
import re
from typing import Any

SETTING_KEY = 'website_cabinet_config'

EMAIL_DOMAIN_REJECT_MESSAGE = (
    'Данная почта не подходит — возможно, она корпоративная или заблокирована. '
    'Используйте популярные почтовые сервисы (Gmail, Yandex, Mail.ru и т.д.).'
)

POPULAR_DOMAIN_PRESETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('Google', ('gmail.com', 'googlemail.com')),
    ('Yandex', ('yandex.ru', 'yandex.com', 'ya.ru')),
    ('Mail.ru', ('mail.ru', 'inbox.ru', 'list.ru', 'bk.ru', 'internet.ru')),
    ('VK', ('vk.com', 'vk.ru')),
    ('Apple', ('icloud.com', 'me.com', 'mac.com')),
    ('Microsoft', ('outlook.com', 'hotmail.com', 'live.com', 'msn.com')),
    ('Proton', ('proton.me', 'protonmail.com', 'pm.me')),
    ('Rambler', ('rambler.ru', 'lenta.ru', 'autorambler.ru', 'myrambler.ru', 'ro.ru')),
    ('Yahoo', ('yahoo.com', 'yahoo.ru')),
)

_DOMAIN_RE = re.compile(
    r'^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$',
    re.IGNORECASE,
)


def default_config() -> dict[str, Any]:
    return {
        'email_whitelist_enabled': False,
        'email_allow_existing_users': True,
        'email_allowed_domains': [],
    }


def normalize_domain(domain: str) -> str:
    return (domain or '').strip().lower().lstrip('@').rstrip('.')


def extract_email_domain(email: str) -> str | None:
    addr = (email or '').strip().lower()
    if '@' not in addr:
        return None
    return normalize_domain(addr.rsplit('@', 1)[1])


def parse_domains_input(value: Any) -> list[str]:
    raw_items: list[str] = []
    if isinstance(value, list):
        raw_items = [str(x) for x in value]
    elif isinstance(value, str):
        raw_items = re.split(r'[\s,;]+', value)
    seen: set[str] = set()
    out: list[str] = []
    for item in raw_items:
        dom = normalize_domain(item)
        if not dom or dom in seen or not _DOMAIN_RE.match(dom):
            continue
        seen.add(dom)
        out.append(dom)
    return out


def merge_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    base = default_config()
    if not isinstance(raw, dict):
        return base
    if isinstance(raw.get('email_whitelist_enabled'), bool):
        base['email_whitelist_enabled'] = raw['email_whitelist_enabled']
    if isinstance(raw.get('email_allow_existing_users'), bool):
        base['email_allow_existing_users'] = raw['email_allow_existing_users']
    base['email_allowed_domains'] = parse_domains_input(raw.get('email_allowed_domains'))
    return base


def validate_config(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    enabled_raw = data.get('email_whitelist_enabled')
    allow_existing_raw = data.get('email_allow_existing_users')
    cfg = default_config()
    cfg['email_whitelist_enabled'] = bool(
        enabled_raw is True or enabled_raw == 1 or str(enabled_raw).strip().lower() in ('1', 'true', 'yes')
    )
    cfg['email_allow_existing_users'] = not (
        allow_existing_raw is False or allow_existing_raw == 0
        or str(allow_existing_raw).strip().lower() in ('0', 'false', 'no')
    )
    cfg['email_allowed_domains'] = parse_domains_input(data.get('email_allowed_domains'))
    return cfg


def config_from_setting_value(raw_value: str | None) -> dict[str, Any]:
    if not raw_value:
        return default_config()
    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default_config()
    if not isinstance(parsed, dict):
        return default_config()
    return merge_config(parsed)


def is_email_domain_allowed(email: str, cfg: dict[str, Any], *, user_already_exists: bool) -> bool:
    if not cfg.get('email_whitelist_enabled'):
        return True
    if user_already_exists and cfg.get('email_allow_existing_users', True):
        return True
    domain = extract_email_domain(email)
    if not domain:
        return False
    allowed = {normalize_domain(d) for d in cfg.get('email_allowed_domains') or []}
    if not allowed:
        return False
    return domain in allowed


def all_preset_domains() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for _, domains in POPULAR_DOMAIN_PRESETS:
        for dom in domains:
            d = normalize_domain(dom)
            if d and d not in seen:
                seen.add(d)
                out.append(d)
    return out
