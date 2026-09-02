"""Разбор конфигурации из окружения.

Списки через запятую — отдельная история: pydantic-settings по умолчанию
пытается разобрать значение поля-списка как JSON ещё до валидаторов,
и пустая строка в .env роняет запуск всех процессов.
"""

from __future__ import annotations

import pytest

from core.config import BotSettings, PlategaSettings, Settings, SubscriptionSettings


class TestListParsing:
    def test_empty_admin_ids_gives_empty_list(self, monkeypatch):
        monkeypatch.setenv("BOT_ADMIN_IDS", "")
        assert BotSettings().admin_ids == []

    def test_unset_admin_ids_gives_empty_list(self, monkeypatch):
        monkeypatch.delenv("BOT_ADMIN_IDS", raising=False)
        assert BotSettings().admin_ids == []

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("111", [111]),
            ("111,222", [111, 222]),
            ("111, 222 , 333", [111, 222, 333]),
            ("111;222", [111, 222]),
            ("111,,222", [111, 222]),
        ],
    )
    def test_admin_ids_variants(self, monkeypatch, raw, expected):
        monkeypatch.setenv("BOT_ADMIN_IDS", raw)
        assert BotSettings().admin_ids == expected

    def test_reminder_days(self, monkeypatch):
        monkeypatch.setenv("SUBSCRIPTION_REMINDER_DAYS_BEFORE", "7,3,1,0")
        assert SubscriptionSettings().reminder_days_before == [7, 3, 1, 0]

    def test_empty_list_falls_back_to_default(self, monkeypatch):
        """Пустая строка = «не задано», поэтому подставляется дефолт, а не []."""
        monkeypatch.setenv("SUBSCRIPTION_REMINDER_DAYS_AFTER", "")
        assert SubscriptionSettings().reminder_days_after == [1]

    def test_explicit_zero_list_is_respected(self, monkeypatch):
        monkeypatch.setenv("SUBSCRIPTION_REMINDER_DAYS_AFTER", "0")
        assert SubscriptionSettings().reminder_days_after == [0]

    def test_allowed_ips(self, monkeypatch):
        monkeypatch.setenv("PLATEGA_ALLOWED_IPS", "1.2.3.4, 5.6.7.0/24")
        assert PlategaSettings().allowed_ips == ["1.2.3.4", "5.6.7.0/24"]

    def test_empty_allowed_ips(self, monkeypatch):
        monkeypatch.setenv("PLATEGA_ALLOWED_IPS", "")
        assert PlategaSettings().allowed_ips == []


class TestEmptyValues:
    """Пустая переменная в .env = «не задано». Шаблон полон таких строк."""

    @pytest.mark.parametrize(
        "variable",
        ["BOT_OWNER_ID", "BOT_SUPPORT_GROUP_ID", "BOT_ALERTS_CHAT_ID", "BOT_INTERNAL_PORT"],
    )
    def test_empty_int_falls_back_to_default(self, monkeypatch, variable):
        monkeypatch.setenv(variable, "")
        settings = BotSettings()
        assert isinstance(getattr(settings, variable.removeprefix("BOT_").lower()), int)

    def test_empty_optional_ids_are_zero(self, monkeypatch):
        monkeypatch.setenv("BOT_SUPPORT_GROUP_ID", "")
        monkeypatch.setenv("BOT_ALERTS_CHAT_ID", "")
        settings = BotSettings()
        assert settings.support_group_id == 0
        assert settings.alerts_chat_id == 0

    def test_empty_secret_is_empty_not_error(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "")
        assert BotSettings().token.get_secret_value() == ""

    def test_empty_float_falls_back(self, monkeypatch):
        monkeypatch.setenv("PLATEGA_TIMEOUT_SEC", "")
        assert PlategaSettings().timeout_sec == 20.0

    def test_whole_config_builds_with_empty_optionals(self, monkeypatch):
        for variable in (
            "BOT_ADMIN_IDS",
            "BOT_SUPPORT_GROUP_ID",
            "BOT_ALERTS_CHAT_ID",
            "PLATEGA_ALLOWED_IPS",
            "PLATEGA_MERCHANT_ID",
            "SENTRY_DSN",
            "API_WORKERS",
        ):
            monkeypatch.setenv(variable, "")
        monkeypatch.setenv("APP_ENV", "dev")
        settings = Settings()
        assert settings.bot.alerts_chat_id == 0
        assert settings.platega.enabled is False


class TestAdminCheck:
    def test_owner_is_admin(self, monkeypatch):
        monkeypatch.setenv("BOT_OWNER_ID", "500")
        monkeypatch.setenv("BOT_ADMIN_IDS", "")
        settings = BotSettings()
        assert settings.is_admin(500) is True
        assert settings.is_admin(501) is False

    def test_listed_admin(self, monkeypatch):
        monkeypatch.setenv("BOT_OWNER_ID", "500")
        monkeypatch.setenv("BOT_ADMIN_IDS", "600,700")
        settings = BotSettings()
        assert settings.is_admin(600) is True
        assert settings.is_admin(800) is False


class TestWebhookUrl:
    def test_url_is_assembled(self, monkeypatch):
        monkeypatch.setenv("BOT_WEBHOOK_BASE_URL", "https://api.example.ru/")
        monkeypatch.setenv("BOT_WEBHOOK_PATH", "/tg/webhook")
        assert BotSettings().webhook_url == "https://api.example.ru/tg/webhook"


class TestProdValidation:
    """В проде процесс обязан падать на старте, а не работать с полуконфигом."""

    @pytest.fixture
    def prod_env(self, monkeypatch):
        for key, value in {
            "APP_ENV": "prod",
            "BOT_TOKEN": "123:abc",
            "BOT_MODE": "polling",
            "SECURITY_SECRET_KEY": "x" * 32,
            "SECURITY_ENCRYPTION_KEY": "ERERERERERERERERERERERERERERERERERERERERERE=",
            "POSTGRES_PASSWORD": "secret",
            "API_PUBLIC_BASE_URL": "https://api.example.ru",
        }.items():
            monkeypatch.setenv(key, value)
        return monkeypatch

    def test_valid_prod_config(self, prod_env):
        assert Settings().app.is_prod is True

    def test_missing_bot_token_is_fine(self, prod_env):
        """Своего бота у нас нет: клиенту пишет бот стороннего продукта своим
        токеном, а сообщения мы кладём в очередь. Требовать мёртвый токен ради
        запуска — способ держать его в .env вечно."""
        prod_env.setenv("BOT_TOKEN", "")
        assert Settings().app.is_prod is True

    def test_plain_http_public_url_fails(self, prod_env):
        """Ссылки подписки прошиваются в роутеры — там не может быть http."""
        prod_env.setenv("API_PUBLIC_BASE_URL", "http://localhost:8000")
        with pytest.raises(ValueError, match="API_PUBLIC_BASE_URL"):
            Settings()


    def test_dev_env_skips_checks(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "dev")
        monkeypatch.setenv("BOT_TOKEN", "")
        monkeypatch.setenv("API_PUBLIC_BASE_URL", "http://localhost:8000")
        assert Settings().app.is_prod is False


class TestEncryptionKey:
    def test_invalid_base64_rejected(self, monkeypatch):
        monkeypatch.setenv("SECURITY_ENCRYPTION_KEY", "не base64!")
        with pytest.raises(ValueError, match="base64"):
            Settings()

    def test_wrong_length_rejected(self, monkeypatch):
        monkeypatch.setenv("SECURITY_ENCRYPTION_KEY", "c2hvcnQ=")  # 5 байт
        with pytest.raises(ValueError, match="32 байта"):
            Settings()


class TestMiniappToken:
    """Подпись входа в приложение проверяется токеном бота.

    Держать в окружении второй экземпляр того же секрета — верный способ
    однажды поменять его в одном месте и полдня искать, почему вход перестал
    сходиться. Поэтому приложение берёт общий `BOT_TOKEN`, а своя переменная
    остаётся на случай, когда приложение живёт в отдельном боте.
    """

    def test_shared_bot_token_is_used(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "123:general")
        monkeypatch.setenv("MINIAPP_BOT_TOKEN", "")
        assert Settings().miniapp.bot_token.get_secret_value() == "123:general"

    def test_own_token_wins(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "123:general")
        monkeypatch.setenv("MINIAPP_BOT_TOKEN", "456:separate")
        assert Settings().miniapp.bot_token.get_secret_value() == "456:separate"

    def test_without_any_token_the_app_is_off(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "")
        monkeypatch.setenv("MINIAPP_BOT_TOKEN", "")
        monkeypatch.setenv("MINIAPP_ALLOWED_TG_IDS", "614685408")
        assert Settings().miniapp.is_configured is False

    def test_empty_allowlist_means_nobody(self, monkeypatch):
        """Пустой список — «никому», а не «всем»: приложение на обкатке."""
        monkeypatch.setenv("BOT_TOKEN", "123:general")
        monkeypatch.setenv("MINIAPP_ALLOWED_TG_IDS", "")
        settings = Settings()
        assert settings.miniapp.is_configured is False
        assert settings.miniapp.is_allowed(614685408) is False

    def test_allowlist_is_read_from_a_comma_list(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "123:general")
        monkeypatch.setenv("MINIAPP_ALLOWED_TG_IDS", "614685408, 777")
        settings = Settings()
        assert settings.miniapp.is_configured is True
        assert settings.miniapp.is_allowed(614685408) is True
        assert settings.miniapp.is_allowed(999) is False
