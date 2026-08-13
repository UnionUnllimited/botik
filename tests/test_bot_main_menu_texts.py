"""Главное меню не должно падать из-за незаведённого текста.

Чужой продукт всегда ставился с готовой базой, и часть текстов в код так
и не попала. `app_conf.get()` отдаёт на них None, а дальше `.format()` или
склейка строк — и валится не текст, а всё меню: бот молча перестаёт отвечать
на `/start`, потому что исключение ловит глобальный обработчик.

Так и случилось с блоком подписки: ветка выполняется только при активной
подписке, а подписок в базе бота не было вовсе, пока мы не начали зеркалить
их туда. У первого же оплатившего клиента меню перестало открываться.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parents[1] / "bot"
MAIN = (BOT / "main.py").read_text(encoding="utf-8")
DB_HELPERS = (BOT / "db_helpers.py").read_text(encoding="utf-8")

# Ключи, которые главное меню форматирует или приклеивает к строке.
MENU_TEXT_KEYS = (
    "text_welcome_message",
    "text_subscription_info",
    "text_subscription_expired_main",
    "text_about_service",
    "text_promo_code_success",
)


class TestMenuTextsAreSeeded:
    @pytest.mark.parametrize("key", MENU_TEXT_KEYS)
    def test_key_has_a_default_in_the_database(self, key):
        """У ключа есть значение по умолчанию: на чистой базе меню откроется."""
        assert f"('{key}'," in DB_HELPERS, (
            f"{key} читается в главном меню, но никем не заводится — "
            "на свежей базе меню упадёт"
        )


class TestMenuTextsSurviveAnEmptySetting:
    """Настройку можно стереть на странице текстов — это не должно ронять меню."""

    @pytest.mark.parametrize("key", MENU_TEXT_KEYS)
    def test_read_site_has_a_fallback(self, key):
        reads = [ln for ln in MAIN.splitlines() if f"app_conf.get('{key}')" in ln]
        assert reads, f"{key} больше не читается в main.py — проверить тест"
        for line in reads:
            assert " or DEFAULT" in line, (
                f"{key} читается без запасного значения: {line.strip()!r}. "
                "Пустая настройка снова уронит главное меню."
            )

    def test_no_bare_format_on_a_setting(self):
        """`app_conf.get(...).format(...)` — это падение на первом же пустом ключе."""
        bare = re.findall(r"app_conf\.get\('[^']+'\)\.format\(", MAIN)
        assert not bare, f"нашлось {bare}: значение из базы форматируется без проверки"


class TestSubscriptionInfoPlaceholders:
    def test_default_uses_only_provided_names(self):
        """Лишнее имя в шаблоне — KeyError, то есть снова молчащее меню."""
        provided = {"status", "expiry_date", "limit_ip", "traffic", "sub_link"}
        match = re.search(
            r'DEFAULT_SUBSCRIPTION_INFO = """(.*?)"""', MAIN, re.DOTALL
        )
        assert match, "DEFAULT_SUBSCRIPTION_INFO не найден"
        used = set(re.findall(r"\{(\w+)\}", match.group(1)))
        assert used <= provided, f"в шаблоне неизвестные имена: {used - provided}"
