"""Конфигурация всех процессов проекта (bot / api / worker).

Все значения приходят из окружения (.env или переменные контейнера).
Секретов в репозитории нет — только .env.example с описанием переменных.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

_CONFIG = SettingsConfigDict(
    env_file=ENV_FILE,
    env_file_encoding="utf-8",
    extra="ignore",
    case_sensitive=False,
)


def _split_ints(value: Any) -> Any:
    """Позволяет писать списки id через запятую: ADMIN_IDS=1,2,3."""
    if isinstance(value, str):
        return [int(chunk) for chunk in value.replace(";", ",").split(",") if chunk.strip()]
    return value


IdList = Annotated[list[int], BeforeValidator(_split_ints)]


class AppSettings(BaseSettings):
    model_config = _CONFIG | SettingsConfigDict(env_prefix="APP_")

    env: Literal["dev", "prod"] = "dev"
    debug: bool = False
    brand: str = "Router Shop"
    """Название бренда, подставляется в тексты бота и админки."""
    bot_username: str = ""
    """username бота без @ — нужен для сборки deep-link и реферальных ссылок."""
    timezone: str = "Europe/Moscow"
    """Пояс отображения. В БД всё хранится в UTC (timestamptz)."""
    currency: str = "RUB"

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


class LogSettings(BaseSettings):
    model_config = _CONFIG | SettingsConfigDict(env_prefix="LOG_")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "console"] = "json"
    sql_echo: bool = False


class DatabaseSettings(BaseSettings):
    model_config = _CONFIG | SettingsConfigDict(env_prefix="POSTGRES_")

    host: str = "postgres"
    port: int = 5432
    db: str = "router_shop"
    user: str = "router_shop"
    password: SecretStr = SecretStr("")
    pool_size: int = 10
    max_overflow: int = 10
    pool_timeout: int = 30
    pool_recycle: int = 1800
    statement_timeout_ms: int = 15_000

    def dsn(self, *, driver: str = "postgresql+asyncpg") -> str:
        pwd = self.password.get_secret_value()
        return f"{driver}://{self.user}:{pwd}@{self.host}:{self.port}/{self.db}"

    @property
    def async_dsn(self) -> str:
        return self.dsn()

    @property
    def sync_dsn(self) -> str:
        """Для утилит (pg_dump/psql в скриптах он не нужен, но alembic offline — да)."""
        return self.dsn(driver="postgresql+psycopg")


class RedisSettings(BaseSettings):
    model_config = _CONFIG | SettingsConfigDict(env_prefix="REDIS_")

    url: str = "redis://redis:6379/0"
    key_prefix: str = "rs"
    """Общий префикс ключей, чтобы можно было делить один Redis между стендами."""
    socket_timeout: float = 5.0
    max_connections: int = 50

    def key(self, *parts: str) -> str:
        return ":".join((self.key_prefix, *parts))


class BotSettings(BaseSettings):
    model_config = _CONFIG | SettingsConfigDict(env_prefix="BOT_")

    token: SecretStr = SecretStr("")
    mode: Literal["polling", "webhook"] = "polling"
    webhook_base_url: str = ""
    """Публичный https-URL, на который Telegram шлёт апдейты (без пути)."""
    webhook_path: str = "/tg/webhook"
    webhook_secret: SecretStr = SecretStr("")
    """Значение X-Telegram-Bot-Api-Secret-Token."""
    internal_host: str = "0.0.0.0"  # noqa: S104 — слушаем внутри docker-сети
    internal_port: int = 8081
    drop_pending_updates: bool = False

    owner_id: int = 0
    """TG-id владельца: полные права, получает критичные алерты."""
    admin_ids: IdList = Field(default_factory=list)
    support_group_id: int = 0
    """Супергруппа с топиками (forum) — по топику на тикет."""
    alerts_chat_id: int = 0
    """Канал/чат для служебных алертов (заказы, платежи, фрод)."""

    @property
    def webhook_url(self) -> str:
        return f"{self.webhook_base_url.rstrip('/')}{self.webhook_path}"

    def is_admin(self, tg_id: int) -> bool:
        return tg_id == self.owner_id or tg_id in self.admin_ids


class ApiSettings(BaseSettings):
    model_config = _CONFIG | SettingsConfigDict(env_prefix="API_")

    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8000
    workers: int = 1
    public_base_url: str = "http://localhost:8000"
    """Внешний адрес API — из него собираются ссылки подписки для роутеров."""
    admin_base_url: str = "http://localhost:8000"
    trusted_proxy_hops: int = 1
    """Сколько прокси перед приложением (для корректного разбора X-Forwarded-For)."""
    docs_enabled: bool = False


class SecuritySettings(BaseSettings):
    model_config = _CONFIG | SettingsConfigDict(env_prefix="SECURITY_")

    secret_key: SecretStr = SecretStr("")
    """Подпись сессий и CSRF-токенов админки."""
    encryption_key: SecretStr = SecretStr("")
    """base64(32 байта) — AES-256-GCM для секретов устройств и TOTP-секретов админов."""

    device_clock_skew_sec: int = 300
    device_nonce_ttl_sec: int = 600
    device_rate_limit_per_min: int = 60
    activation_attempts_per_hour: int = 10
    sub_token_grace_hours: int = 24
    """Сколько живёт старый токен подписки после ротации."""
    sub_distinct_ip_alert: int = 5
    """Порог разных IP на одном токене за час → алерт о возможной перепродаже."""

    admin_login_max_attempts: int = 5
    admin_lockout_minutes: int = 15
    admin_session_ttl_hours: int = 12

    @field_validator("encryption_key")
    @classmethod
    def _check_encryption_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw:
            return value
        try:
            decoded = base64.b64decode(raw, validate=True)
        except Exception as exc:
            raise ValueError("SECURITY_ENCRYPTION_KEY должен быть base64") from exc
        if len(decoded) != 32:
            raise ValueError("SECURITY_ENCRYPTION_KEY должен декодироваться в ровно 32 байта")
        return value


class SubscriptionSettings(BaseSettings):
    model_config = _CONFIG | SettingsConfigDict(env_prefix="SUBSCRIPTION_")

    grace_days: int = 3
    """Сколько дней доступ ещё работает после окончания оплаченного периода."""
    reminder_days_before: IdList = Field(default_factory=lambda: [7, 3, 1, 0])
    reminder_days_after: IdList = Field(default_factory=lambda: [1, 3])
    devices_per_user: int = 1
    activation_deadline_days: int = 180
    """Оплаченная, но не активированная подписка сгорает через N дней (напоминаем заранее)."""

    heartbeat_interval_min: int = 10
    heartbeat_offline_min: int = 15
    heartbeat_retention_days: int = 30
    access_log_retention_days: int = 30

    referral_bonus_days: int = 14
    node_prefix: str = "Router_"
    """Обязательный префикс имени узла — по нему фильтрует клиент на роутере."""


class SentrySettings(BaseSettings):
    model_config = _CONFIG | SettingsConfigDict(env_prefix="SENTRY_")

    dsn: str = ""
    traces_sample_rate: float = 0.0
    enabled: bool = False

    @model_validator(mode="after")
    def _auto_enable(self) -> SentrySettings:
        if self.dsn and not self.enabled:
            object.__setattr__(self, "enabled", True)
        return self


class Settings(BaseSettings):
    model_config = _CONFIG

    app: AppSettings = Field(default_factory=AppSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    bot: BotSettings = Field(default_factory=BotSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    subscription: SubscriptionSettings = Field(default_factory=SubscriptionSettings)
    sentry: SentrySettings = Field(default_factory=SentrySettings)

    @model_validator(mode="after")
    def _validate_prod(self) -> Settings:
        if not self.app.is_prod:
            return self
        missing: list[str] = []
        if not self.bot.token.get_secret_value():
            missing.append("BOT_TOKEN")
        if not self.security.secret_key.get_secret_value():
            missing.append("SECURITY_SECRET_KEY")
        if not self.security.encryption_key.get_secret_value():
            missing.append("SECURITY_ENCRYPTION_KEY")
        if not self.db.password.get_secret_value():
            missing.append("POSTGRES_PASSWORD")
        if self.bot.mode == "webhook":
            if not self.bot.webhook_base_url.startswith("https://"):
                missing.append("BOT_WEBHOOK_BASE_URL (должен быть https)")
            if not self.bot.webhook_secret.get_secret_value():
                missing.append("BOT_WEBHOOK_SECRET")
        if not self.api.public_base_url.startswith("https://"):
            missing.append("API_PUBLIC_BASE_URL (должен быть https)")
        if missing:
            raise ValueError("Не заданы обязательные переменные для APP_ENV=prod: " + ", ".join(missing))
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
"""Синглтон на процесс. В тестах нужные секции конструируются напрямую (AppSettings(...))."""
