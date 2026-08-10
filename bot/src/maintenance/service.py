"""Проверки и тексты сервисного режима."""
from __future__ import annotations

from app_config import app_conf
from src.core.utils import is_bot_admin
from src.texts import TXT_MAINTENANCE

_SETTING_ENABLED = 'bot_maintenance_enabled'
_SETTING_MESSAGE = 'bot_maintenance_message'


def is_maintenance_enabled() -> bool:
    return str(app_conf.get(_SETTING_ENABLED, '0')).strip() in ('1', 'true', 'yes', 'on')


def get_maintenance_message() -> str:
    raw = app_conf.get(_SETTING_MESSAGE, '') or ''
    text = str(raw).strip()
    return text or TXT_MAINTENANCE


def is_admin_user(user_id: int) -> bool:
    """Админы из admin_ids проходят через сервисный режим."""
    return is_bot_admin(user_id)
