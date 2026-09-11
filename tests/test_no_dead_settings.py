"""Настройка, которую никто не читает, обещает то, чего нет.

В примере `.env` таких было пятнадцать. Хуже прочих —
`SECURITY_ADMIN_LOGIN_MAX_ATTEMPTS=5` и `SECURITY_ADMIN_LOCKOUT_MINUTES=15`:
оператор читает их как защиту от подбора пароля и считает вход прикрытым.
Ни то, ни другое не читалось ни одной строкой кода.

Остались они от времён, когда у нас были свой бот, своя админка и кабинет
клиента на сайте. Кабинет удалён вместе с сайтом, бот теперь чужой, админка
тоже — и со своим входом.

Общую проверку «каждая переменная кем-то читается» здесь не делаем: часть
переменных примера читает не питон, а docker compose и nginx, и такая
проверка вышла бы длиннее пользы. Держим список тех, что уже убраны.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = (ROOT / "core" / "config.py").read_text(encoding="utf-8")
EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")

REMOVED = {
    # Обещали защиту входа в админку, которой нет.
    "SECURITY_ADMIN_LOGIN_MAX_ATTEMPTS": "admin_login_max_attempts",
    "SECURITY_ADMIN_LOCKOUT_MINUTES": "admin_lockout_minutes",
    "SECURITY_ADMIN_SESSION_TTL_HOURS": "admin_session_ttl_hours",
    # Кабинет клиента на сайте удалён вместе с сайтом.
    "SECURITY_CLIENT_SESSION_TTL_DAYS": "client_session_ttl_days",
    "SECURITY_CLIENT_LOGIN_ATTEMPTS_PER_HOUR": "client_login_attempts_per_hour",
    # Токены подписки ротирует панель, а не мы.
    "SECURITY_SUB_TOKEN_GRACE_HOURS": "sub_token_grace_hours",
    "SECURITY_SUB_DISTINCT_IP_ALERT": "sub_distinct_ip_alert",
    # Роутер к нам не ходит: показания снимаем мы сами по SSH.
    "SECURITY_DEVICE_CLOCK_SKEW_SEC": "device_clock_skew_sec",
    "SECURITY_DEVICE_RATE_LIMIT_PER_MIN": "device_rate_limit_per_min",
    # Своего бота у нас больше нет.
    "BOT_MODE": "mode",
    "BOT_WEBHOOK_SECRET": "webhook_secret",
    "BOT_INTERNAL_PORT": "internal_port",
    "BOT_DROP_PENDING_UPDATES": "drop_pending_updates",
    "BOT_SUPPORT_GROUP_ID": "support_group_id",
    # Предела на число роутеров нет и не нужно: второй продаётся со своей
    # подпиской, и ограничивать их число значило бы запрещать вторую покупку.
    "SUBSCRIPTION_DEVICES_PER_USER": "devices_per_user",
}


@pytest.mark.parametrize(("variable", "field"), sorted(REMOVED.items()))
def test_a_dead_setting_does_not_come_back(variable, field):
    assert variable not in EXAMPLE, f"{variable} снова обещает то, чего нет"
    assert not re.search(rf"^    {field}:", CONFIG, re.M), f"{field} снова объявлено"


def test_the_example_still_has_the_настройки_that_matter():
    """Подстраховка: не вырезали ли заодно нужное."""
    for variable in (
        "BOT_TOKEN",
        "BOT_ADMIN_IDS",
        "API_FLEET_TOKEN",
        "MINIAPP_OPEN_TO_ALL",
        "PLATEGA_MERCHANT_ID",
        "SUBSCRIPTION_ACTIVATION_DEADLINE_DAYS",
        "ORDER_ABANDONED_AFTER_HOURS",
    ):
        assert f"{variable}=" in EXAMPLE, variable


def test_what_is_left_of_the_security_block_is_read():
    """Две оставшиеся настройки безопасности — живые."""
    code = "\n".join(
        path.read_text(encoding="utf-8-sig")
        for tree in ("core", "api", "worker")
        for path in sorted((ROOT / tree).rglob("*.py"))
        if "__pycache__" not in path.parts and path.name != "config.py"
    )
    assert "device_nonce_ttl_sec" in code
    assert "activation_attempts_per_hour" in code
