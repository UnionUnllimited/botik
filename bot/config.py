import os
import shutil
from dotenv import load_dotenv

load_dotenv()

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_NAME = os.path.join(_BASE_DIR, 'vpn_bot.db')
REMNAWAVE_DB_PATH = os.path.join(_BASE_DIR, 'remnawave.db')
_LEGACY_REMNAWAVE_DB_PATH = os.path.join(_BASE_DIR, 'analytics', 'remnawave.db')


def migrate_remnawave_db_if_needed() -> str:
    """Переносит legacy ``analytics/remnawave.db`` рядом с ``vpn_bot.db`` (один раз)."""
    if os.path.isfile(REMNAWAVE_DB_PATH):
        return REMNAWAVE_DB_PATH
    if os.path.isfile(_LEGACY_REMNAWAVE_DB_PATH):
        try:
            shutil.move(_LEGACY_REMNAWAVE_DB_PATH, REMNAWAVE_DB_PATH)
        except Exception:
            return _LEGACY_REMNAWAVE_DB_PATH
    return REMNAWAVE_DB_PATH