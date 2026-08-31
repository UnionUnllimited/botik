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
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from pydantic_settings.sources import PydanticBaseSettingsSource

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

_CONFIG = SettingsConfigDict(
    env_file=ENV_FILE,
    env_file_encoding="utf-8",
    extra="ignore",
    case_sensitive=False,
)


class _SkipEmptySource(PydanticBaseSettingsSource):
    """Пустое значение в .env означает «не задано», а не «пустая строка».

    Без этого `BOT_ALERTS_CHAT_ID=` роняет запуск всех процессов: pydantic
    не умеет привести пустую строку к int. Шаблон .env.example специально
    содержит пустые необязательные переменные, поэтому обрабатываем это
    в одном месте, а не аннотацией на каждом поле.
    """

    def __init__(self, source: PydanticBaseSettingsSource) -> None:
        super().__init__(source.settings_cls)
        self._source = source

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._source.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        return {key: value for key, value in self._source().items() if value != ""}

    def __repr__(self) -> str:
        return f"SkipEmpty({self._source!r})"


class EnvSettings(BaseSettings):
    """База для всех секций конфигурации."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            _SkipEmptySource(env_settings),
            _SkipEmptySource(dotenv_settings),
            file_secret_settings,
        )


def _split_ints(value: Any) -> Any:
    """Позволяет писать списки id через запятую: BOT_ADMIN_IDS=1,2,3."""
    if isinstance(value, str):
        return [int(chunk) for chunk in value.replace(";", ",").split(",") if chunk.strip()]
    return value


def _split_strings(value: Any) -> Any:
    """То же для строковых списков: PLATEGA_ALLOWED_IPS=1.2.3.4,5.6.7.0/24."""
    if isinstance(value, str):
        return [chunk.strip() for chunk in value.split(",") if chunk.strip()]
    return value


# NoDecode обязателен: без него pydantic-settings пытается разобрать значение
# как JSON ещё до валидатора, и пустая строка в .env роняет весь конфиг.
IdList = Annotated[list[int], NoDecode, BeforeValidator(_split_ints)]
StrList = Annotated[list[str], NoDecode, BeforeValidator(_split_strings)]


class AppSettings(EnvSettings):
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
    media_dir: str = "/app/media"
    """Куда складываются картинки товаров. Том, иначе они пропадут при пересборке."""
    media_max_bytes: int = 3 * 1024 * 1024
    """Предел на файл: витрину открывают с телефона, мегабайтные фото ей не нужны."""
    firmware_max_bytes: int = 128 * 1024 * 1024
    """Предел на образ прошивки. Они лежат в том же томе, но мерка другая:
    сейчас образы весят 27–54 МБ, и запас нужен на вырост, а не на опечатку."""

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


class LogSettings(EnvSettings):
    model_config = _CONFIG | SettingsConfigDict(env_prefix="LOG_")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "console"] = "json"
    sql_echo: bool = False


class DatabaseSettings(EnvSettings):
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


class RedisSettings(EnvSettings):
    model_config = _CONFIG | SettingsConfigDict(env_prefix="REDIS_")

    url: str = "redis://redis:6379/0"
    key_prefix: str = "rs"
    """Общий префикс ключей, чтобы можно было делить один Redis между стендами."""
    socket_timeout: float = 5.0
    max_connections: int = 50

    def key(self, *parts: str) -> str:
        return ":".join((self.key_prefix, *parts))


class BotSettings(EnvSettings):
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


class ApiSettings(EnvSettings):
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
    fleet_token: SecretStr = SecretStr("")
    """Токен для чтения парка роутеров снаружи — им ходит вкладка «Роутеры»
    в админке бота. Пустой значит выключено: без токена ручка отвечает 404,
    а не отдаёт список устройств всем желающим."""


class SecuritySettings(EnvSettings):
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

    client_session_ttl_days: int = 30
    """Сессия клиента на сайте: заходить каждый день его никто не заставляет."""
    client_login_attempts_per_hour: int = 20
    """Лимит попыток входа на пару «IP + адрес». Учётку не блокируем: зная чужую
    почту, конкурент запирал бы человека снаружи."""

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


class SubscriptionSettings(EnvSettings):
    model_config = _CONFIG | SettingsConfigDict(env_prefix="SUBSCRIPTION_")

    grace_days: int = 0
    """Сколько дней доступ ещё работает после окончания оплаченного периода.

    Ноль — решение заказчика от 21 августа 2026: отключаем день в день.
    Так было и на деле — в панель кладётся дата окончания, и доступ обрывался
    ровно в срок, — а клиенту при этом обещали льготные дни. Обещание убрано,
    поведение оставлено."""

    reminder_days_before: IdList = Field(default_factory=lambda: [7, 3, 1, 0])
    reminder_days_after: IdList = Field(default_factory=lambda: [1])
    """Напоминание после отключения — одно, на следующий день. Дальше клиент
    либо продлил, либо ушёл, и третье сообщение только раздражает."""
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


class PlategaSettings(EnvSettings):
    """Реквизиты и пути API PLATEGA (docs.platega.io).

    Пути вынесены в настройки намеренно: в документации соседствуют
    `/v2/transaction/process` и `/transaction/process`, и провайдер может
    поменять префикс без нашего релиза.
    """

    model_config = _CONFIG | SettingsConfigDict(env_prefix="PLATEGA_")

    merchant_id: str = ""
    secret: SecretStr = SecretStr("")
    base_url: str = "https://app.platega.io"
    create_path: str = "/v2/transaction/process"
    """Создание ссылки без заданного метода — способ выбирает клиент."""
    create_path_with_method: str = "/transaction/process"
    status_path: str = "/transaction"
    default_method: str = "any"
    """any | sbp | card | international | crypto либо числовой код провайдера."""
    timeout_sec: float = 20.0
    link_ttl_min: int = 15
    """Сколько живёт платёжная ссылка, если провайдер не сказал этого сам.

    В ответе PLATEGA срок приходит полем `expiresIn`, но приходит не всегда.
    Без него платёж оставался «ждёт оплаты» навсегда: выборка просроченных
    смотрит на срок, а его нет. Пятнадцать минут — то, что провайдер
    возвращает, когда возвращает."""
    allowed_ips: StrList = Field(default_factory=list)

    partner_callback_url: str = "http://127.0.0.1:8081/platega/callback"
    """Куда отдать колбэк, который относится не к нашему заказу.

    Провайдер шлёт уведомления по одному адресу на мерчанта, а платежей
    два вида: железо продаём мы, подписку — бот. Публичный приёмник поэтому
    один, наш, и он передаёт чужие уведомления боту как есть.

    Пусто — не передавать. Тогда чужой платёж останется без подтверждения:
    клиент заплатил, а подписка не включилась."""
    """Необязательный белый список IP для колбэков (через запятую)."""

    @property
    def enabled(self) -> bool:
        return bool(self.merchant_id and self.secret.get_secret_value())


class FrpSettings(EnvSettings):
    """Доступ к роутерам через frp.

    Сервер frps остаётся на российской площадке — роутеры подключаются к нему
    и держат обратные туннели. Наша админка работает с ним снаружи:
      * дашборд frps отдаёт список подключённых прокси — это онлайн-статус;
      * контейнер frpc в режиме visitor открывает локальные порты к роутерам,
        через них мы зовём их HTTP-API.
    """

    model_config = _CONFIG | SettingsConfigDict(env_prefix="FRP_")

    enabled: bool = False
    server_host: str = ""
    """Хост frps — тот же, к которому подключаются роутеры."""
    server_port: int = 8443
    token: SecretStr = SecretStr("")
    stcp_secret: SecretStr = SecretStr("")
    """Ключ STCP: без него visitor не подключится к прокси роутера."""
    tls_enabled: bool = True
    """У frps включён TLS на транспорте — visitor обязан подключаться так же."""

    dashboard_url: str = ""
    """Например https://origin.example.ru:7500 — API дашборда frps."""
    dashboard_user: str = "admin"
    dashboard_password: SecretStr = SecretStr("")
    dashboard_timeout_sec: float = 10.0

    visitor_host: str = "frpc"
    """Имя контейнера frpc в docker-сети."""
    visitor_base_port: int = 20000
    """Порты visitor'ов раздаются подряд от этой границы."""
    router_http_timeout_sec: float = 8.0
    stats_path: str = "/cgi-bin/stats"
    poll_interval_sec: int = 60
    """Как часто спрашиваем у frps, кто на связи. До роутеров не доходит:
    один запрос к дашборду на весь парк. Отсюда же автоактивация, поэтому
    часто — клиент включил роутер и ждёт подписку, а не полчаса."""

    stats_interval_sec: int = 1800
    """Как часто снимаем показания через туннели. Каждое снятие — соединение
    до роутера домой к клиенту, а CPU и аптайм никто не смотрит чаще, чем раз
    в полчаса. Раз в минуту это был постоянный стук в дверь по всему парку."""
    metrics_retention_days: int = 14

    ssh_user: str = "root"
    ssh_password: SecretStr = SecretStr("")
    """Запасной статический пароль, если вывод из MAC не используется."""
    ssh_password_salt: SecretStr = SecretStr("")
    """Соль для вывода пароля из MAC — так их назначает прошивка при первом запуске."""
    ssh_timeout_sec: float = 15.0
    ssh_visitor_offset: int = 10000
    """Порт SSH-туннеля = порт панели + это смещение."""

    luci_prefix: str = "luci"
    ssh_prefix: str = "ssh"
    """Префиксы имён прокси: luci<MAC> — веб-панель роутера, ssh<MAC> — SSH."""

    @property
    def missing_keys(self) -> list[str]:
        """Каких переменных не хватает — сообщение оператору должно быть точным."""
        missing: list[str] = []
        if not self.enabled:
            missing.append("FRP_ENABLED=true")
        if not self.dashboard_url:
            missing.append("FRP_DASHBOARD_URL")
        if not self.dashboard_password.get_secret_value():
            missing.append("FRP_DASHBOARD_PASSWORD")
        if not self.token.get_secret_value():
            missing.append("FRP_TOKEN")
        if not self.stcp_secret.get_secret_value():
            missing.append("FRP_STCP_SECRET")
        if not self.server_host:
            missing.append("FRP_SERVER_HOST")
        return missing

    @property
    def is_configured(self) -> bool:
        """Для чтения статусов хватает дашборда: туннели нужны только для показаний."""
        return bool(self.enabled and self.dashboard_url and self.dashboard_password.get_secret_value())


class RemnawaveSettings(EnvSettings):
    """Панель Remnawave — источник узлов доступа и учёток для роутеров.

    Панель стоит на том же сервере в своей docker-сети, поэтому по умолчанию
    зовём её по внутреннему имени контейнера, а не через публичный домен.

    Пути вынесены в переменные по той же причине, что и у PLATEGA: панель
    активно развивается, список узлов в разных её версиях лежал то на
    `/api/nodes`, то на `/api/nodes/get-all`. Переезд ручки не должен
    требовать нашего релиза — админ поправит переменную и перезапустит.
    """

    model_config = _CONFIG | SettingsConfigDict(env_prefix="REMNAWAVE_")

    enabled: bool = False
    base_url: str = ""
    """Например http://remnawave-backend:3000 внутри сети или https://panel.example."""
    token: SecretStr = SecretStr("")
    """Bearer-токен из настроек панели."""
    proxy_token: SecretStr = SecretStr("")
    """X-Api-Key, если панель дополнительно закрыта прокси."""
    timeout_sec: float = 15.0
    verify_tls: bool = True

    stats_path: str = "/api/system/stats"
    nodes_path: str = "/api/nodes"
    hosts_path: str = "/api/hosts"
    users_path: str = "/api/users"
    squads_path: str = "/api/internal-squads"

    sub_public_host: str = ""
    """Хост, через который роутер ходит за подпиской вместо домена панели.

    Панель отдаёт ссылку на себя, и роутер стучится прямо к ней — то есть
    её адрес виден и достижим у каждого клиента. Здесь задаётся прикрытие
    (например, домен за DDoS-Guard), и при доставке хост в ссылке
    подменяется на него. Путь и токен остаются панельными: по ним она
    и узнаёт клиента.

    Пусто — ссылка уходит роутеру такой, какой её выдала панель."""

    squad_uuids: StrList = Field(default_factory=list)
    """Сквады, в которые попадает новый клиент. Без них панель не выдаст ему узлов."""
    username_template: str = "tg{tg_id}_{mac}"
    """Имя учётки в панели. Панель принимает только латиницу, цифры, дефис и подчёркивание."""
    username_template_no_tg: str = "id{user_id}_{mac}"
    """Для клиентов с сайта: Telegram у них нет, и основной шаблон дал бы «tgNone_...».
    Отдельный шаблон, а не общий с {user_id}: менять имена уже заведённых учёток нельзя,
    панель ищет их по имени."""
    traffic_limit_bytes: int = 0
    """0 — без ограничения трафика."""

    @property
    def missing_keys(self) -> list[str]:
        """Каких переменных не хватает — сообщение оператору должно быть точным."""
        missing: list[str] = []
        if not self.enabled:
            missing.append("REMNAWAVE_ENABLED=true")
        if not self.base_url:
            missing.append("REMNAWAVE_BASE_URL")
        if not self.token.get_secret_value():
            missing.append("REMNAWAVE_TOKEN")
        return missing

    @property
    def is_configured(self) -> bool:
        return not self.missing_keys


class SentrySettings(EnvSettings):
    model_config = _CONFIG | SettingsConfigDict(env_prefix="SENTRY_")

    dsn: str = ""
    traces_sample_rate: float = 0.0
    enabled: bool = False

    @model_validator(mode="after")
    def _auto_enable(self) -> SentrySettings:
        if self.dsn and not self.enabled:
            object.__setattr__(self, "enabled", True)
        return self


class ListsSettings(EnvSettings):
    """Списки доменов: как часто собирать и куда класть копию.

    Списки тянет весь парк разом, и выкат нашего сервера не должен оставлять
    роутеры без обновления. Копия в хранилище это переживает.

    Провайдеры S3-совместимы и различаются только адресом, поэтому клиент
    один, а выбор — значение `endpoint_url`:
      Yandex — https://storage.yandexcloud.net
      VK     — https://hb.vkcs.cloud
    Пустой bucket выключает выкладку целиком.
    """

    model_config = _CONFIG | SettingsConfigDict(env_prefix="LISTS_")

    s3_bucket: str = ""
    s3_endpoint: str = ""
    s3_region: str = "ru-central1"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    s3_prefix: str = "lists/"
    local_dir: str = ""
    """Каталог на диске, куда положить копию собранных списков. Нужен, когда
    те же файлы отдаёт свой веб-сервер с другого домена. Пусто — не класть."""

    poll_interval_min: int = 10
    """Как часто спрашивать источники. Круг условный: неизменившийся файл
    отвечает 304 без тела, поэтому частота упирается не в трафик, а в вежливость
    к отдающей стороне."""

    @property
    def is_configured(self) -> bool:
        return bool(
            self.s3_bucket
            and self.s3_endpoint
            and self.s3_access_key.get_secret_value()
            and self.s3_secret_key.get_secret_value()
        )


class Settings(EnvSettings):
    model_config = _CONFIG

    app: AppSettings = Field(default_factory=AppSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    bot: BotSettings = Field(default_factory=BotSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    subscription: SubscriptionSettings = Field(default_factory=SubscriptionSettings)
    platega: PlategaSettings = Field(default_factory=PlategaSettings)
    frp: FrpSettings = Field(default_factory=FrpSettings)
    remnawave: RemnawaveSettings = Field(default_factory=RemnawaveSettings)
    lists: ListsSettings = Field(default_factory=ListsSettings)
    sentry: SentrySettings = Field(default_factory=SentrySettings)

    @model_validator(mode="after")
    def _validate_prod(self) -> Settings:
        if not self.app.is_prod:
            return self
        missing: list[str] = []
        # BOT_TOKEN здесь больше не требуется: своего бота у нас нет, клиенту
        # пишет бот стороннего продукта своим токеном. Требовать мёртвый токен
        # ради запуска — верный способ держать его в .env вечно.
        if not self.security.secret_key.get_secret_value():
            missing.append("SECURITY_SECRET_KEY")
        if not self.security.encryption_key.get_secret_value():
            missing.append("SECURITY_ENCRYPTION_KEY")
        if not self.db.password.get_secret_value():
            missing.append("POSTGRES_PASSWORD")
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
