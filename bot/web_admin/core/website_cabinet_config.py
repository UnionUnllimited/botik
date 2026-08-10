"""Загрузка/сохранение website_cabinet_config в settings (только web_admin)."""
from __future__ import annotations

import json
from typing import Any

from web_admin.async_db import async_execute_db, async_query_db
from website.email_domain_policy import (
    SETTING_KEY,
    POPULAR_DOMAIN_PRESETS,
    merge_config,
    validate_config,
)

__all__ = [
    'SETTING_KEY',
    'POPULAR_DOMAIN_PRESETS',
    'load_config',
    'save_config',
]


async def load_config() -> dict[str, Any]:
    row = await async_query_db(
        "SELECT value FROM settings WHERE key = ?", (SETTING_KEY,), one=True,
    )
    raw: dict[str, Any] = {}
    if row and row.get('value'):
        try:
            parsed = json.loads(row['value'])
            if isinstance(parsed, dict):
                raw = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
    return merge_config(raw)


async def save_config(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = validate_config(payload)
    serialized = json.dumps(cfg, ensure_ascii=False)
    existing = await async_query_db(
        "SELECT 1 FROM settings WHERE key = ?", (SETTING_KEY,), one=True,
    )
    if existing:
        await async_execute_db(
            "UPDATE settings SET value = ? WHERE key = ?",
            (serialized, SETTING_KEY),
        )
    else:
        await async_execute_db(
            "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
            (
                SETTING_KEY,
                serialized,
                'JSON-настройки веб-кабинета (website), /settings/website-cabinet',
            ),
        )
    return cfg
