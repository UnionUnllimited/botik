"""Общие фикстуры и окружение тестов.

Переменные ставим до импорта core.config: настройки читаются один раз на процесс.
"""

from __future__ import annotations

import base64
import os

os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault("APP_TIMEZONE", "Europe/Moscow")
os.environ.setdefault("APP_BOT_USERNAME", "test_router_bot")
os.environ.setdefault("SECURITY_ENCRYPTION_KEY", base64.b64encode(b"\x11" * 32).decode())
os.environ.setdefault("SECURITY_SECRET_KEY", "test-secret-key")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("LOG_FORMAT", "console")
