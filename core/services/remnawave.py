"""Клиент панели Remnawave.

Панель управляет серверами доступа и учётками на них. Нам она нужна как
источник правды по узлам: адреса, порты и параметры подключения заводятся
там, а мы отдаём их роутеру в подписке — дублировать это руками в двух
местах значит рано или поздно разъехаться.

Два принципа, из-за которых код выглядит осторожнее обычного клиента:

  * **Ответ разбирается терпимо.** Панель заворачивает полезную нагрузку
    в `{"response": ...}`, но в разных версиях встречались и голый массив,
    и `{"data": ...}`, а имена полей приходят в camelCase. Поэтому значения
    достаём через `_pick`, а не по одному жёсткому ключу.
  * **Ошибка связи — не исключение, а состояние.** Раздел админки обязан
    открыться и объяснить, что не так, даже если панель лежит. Для этого
    есть `probe()`, который ничего не бросает.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from core.config import settings

log = structlog.get_logger("services.remnawave")

# Ключи, под которыми панель отдаёт список в объекте-обёртке.
_LIST_KEYS = ("nodes", "hosts", "users", "items", "list", "response", "data", "result")


class RemnawaveError(RuntimeError):
    """Панель недоступна, отказала в доступе или ответила не тем, чего ждём."""


class UserExistsError(RemnawaveError):
    """Учётка с таким именем уже заведена — повторная активация того же роутера."""


def _int_of(value: Any) -> int:
    """Панель отдаёт байты то числом, то строкой, то вовсе не отдаёт."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _pick(item: dict[str, Any], *names: str, default: Any = None) -> Any:
    """Первое непустое значение из перечисленных ключей.

    Имена перебираем и как есть, и в snake_case: панель отвечает camelCase,
    но в её же вебхуках те же поля приходили с подчёркиваниями.
    """
    for name in names:
        for variant in (name, _snake(name)):
            if variant in item and item[variant] not in (None, ""):
                return item[variant]
    return default


def _snake(name: str) -> str:
    return "".join(f"_{ch.lower()}" if ch.isupper() else ch for ch in name)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def unwrap(payload: Any) -> Any:
    """Разворачивает конверт `{"response": {...}}` до полезной нагрузки."""
    seen = 0
    while isinstance(payload, dict) and seen < 3:
        for key in ("response", "data", "result"):
            if key in payload:
                payload = payload[key]
                break
        else:
            break
        seen += 1
    return payload


def as_list(payload: Any) -> list[dict[str, Any]]:
    """Достаёт список словарей из чего угодно, что вернула панель.

    Известные имена ключей проверяются первыми, но полагаться только на них
    нельзя: у сквадов список лежит под `internalSquads`, и такой ключ будет
    у каждой новой сущности свой. Поэтому если ничего не совпало — берём
    первый же список объектов внутри. Конверт к этому моменту уже снят,
    так что перепутать его не с чем.
    """
    payload = unwrap(payload)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    for key in _LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    for value in payload.values():
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            return [item for item in value if isinstance(item, dict)]
    return []


@dataclass(frozen=True, slots=True)
class RemnaNode:
    """Сервер в терминах панели: то, что мы показываем как состояние парка."""

    uuid: str
    name: str
    address: str
    port: int = 0
    country_code: str = ""
    is_disabled: bool = False
    is_connected: bool = False
    is_online: bool = False
    xray_version: str = ""
    users_online: int = 0
    traffic_used_bytes: int = 0
    status_message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, item: dict[str, Any]) -> RemnaNode:
        return cls(
            uuid=str(_pick(item, "uuid", "id", default="")),
            name=str(_pick(item, "name", "remark", default="без имени")),
            address=str(_pick(item, "address", "host", default="")),
            port=_as_int(_pick(item, "port")),
            country_code=str(_pick(item, "countryCode", default=""))[:2].upper(),
            is_disabled=_as_bool(_pick(item, "isDisabled")),
            is_connected=_as_bool(_pick(item, "isConnected")),
            is_online=_as_bool(_pick(item, "isNodeOnline", "isXrayRunning", "isConnected")),
            xray_version=str(_pick(item, "xrayVersion", default="")),
            users_online=_as_int(_pick(item, "usersOnline")),
            traffic_used_bytes=_as_int(_pick(item, "trafficUsedBytes")),
            status_message=str(_pick(item, "lastStatusMessage", default="")),
            raw=item,
        )

    @property
    def tone(self) -> str:
        """Цвет плашки в админке."""
        if self.is_disabled:
            return "muted"
        if self.is_online:
            return "ok"
        return "bad"

    @property
    def state_label(self) -> str:
        if self.is_disabled:
            return "выключен"
        if self.is_online:
            return "на связи"
        return "недоступен"


@dataclass(frozen=True, slots=True)
class RemnaHost:
    """Точка подключения: адрес, порт и параметры, которые уедут в подписку."""

    uuid: str
    remark: str
    address: str
    port: int = 0
    is_disabled: bool = False
    inbound_uuid: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, item: dict[str, Any]) -> RemnaHost:
        return cls(
            uuid=str(_pick(item, "uuid", "id", default="")),
            remark=str(_pick(item, "remark", "name", default="без имени")),
            address=str(_pick(item, "address", "host", default="")),
            port=_as_int(_pick(item, "port")),
            is_disabled=_as_bool(_pick(item, "isDisabled")),
            inbound_uuid=str(_pick(item, "inboundUuid", "configProfileInboundUuid", default="")),
            raw=item,
        )

    @property
    def connection_config(self) -> dict[str, Any]:
        """Параметры подключения без служебных полей панели.

        Кладём в `Node.config` всё, что панель знает о хосте: какие ключи
        понадобятся при сборке ссылки, зависит от протокола, и терять их
        при импорте нельзя.
        """
        skip = {"uuid", "id", "remark", "name", "address", "host", "port", "isDisabled", "is_disabled"}
        return {key: value for key, value in self.raw.items() if key not in skip and value is not None}


@dataclass(frozen=True, slots=True)
class RemnaSquad:
    """Сквад: набор входов, который выдаётся клиенту. Без него узлов у него не будет."""

    uuid: str
    name: str
    members: int = 0
    inbounds: int = 0

    @classmethod
    def parse(cls, item: dict[str, Any]) -> RemnaSquad:
        # Счётчики панель прячет во вложенный `info`, а не держит в корне.
        info = item.get("info") if isinstance(item.get("info"), dict) else {}
        listed = item.get("inbounds")
        return cls(
            uuid=str(_pick(item, "uuid", "id", default="")),
            name=str(_pick(item, "name", "title", default="без имени")),
            members=_as_int(_pick(item, "membersCount", "usersCount", default=_pick(info, "membersCount"))),
            inbounds=_as_int(
                _pick(item, "inboundsCount", default=_pick(info, "inboundsCount"))
                or (len(listed) if isinstance(listed, list) else 0)
            ),
        )

    @property
    def is_usable(self) -> bool:
        """Сквад без входов создаст клиенту учётку с пустым списком узлов."""
        return self.inbounds > 0


@dataclass(frozen=True, slots=True)
class RemnaUser:
    """Учётка клиента в панели. Нас интересует прежде всего ссылка подписки."""

    uuid: str
    username: str
    subscription_url: str
    status: str = ""
    expire_at: str = ""
    used_traffic_bytes: int = 0
    """Расход по счётчику панели. Именно он показывает, сколько ушло через
    подписку: счётчики самого роутера считают и домашний трафик тоже."""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, item: dict[str, Any]) -> RemnaUser:
        return cls(
            uuid=str(_pick(item, "uuid", "id", default="")),
            username=str(_pick(item, "username", default="")),
            subscription_url=str(_pick(item, "subscriptionUrl", default="")),
            status=str(_pick(item, "status", default="")),
            expire_at=str(_pick(item, "expireAt", default="")),
            used_traffic_bytes=_int_of(
                _pick(item, "usedTrafficBytes", "lifetimeUsedTrafficBytes", default=0)
            ),
            raw=item,
        )


@dataclass(slots=True)
class PanelStatus:
    """Итог обращения к панели для страницы админки."""

    configured: bool = False
    ok: bool = False
    error: str = ""
    missing_keys: list[str] = field(default_factory=list)
    checked_at: dt.datetime | None = None

    users_total: int = 0
    users_online: int = 0
    users_active: int = 0
    nodes_total: int = 0
    nodes_online: int = 0
    hosts_total: int = 0
    version: str = ""
    uptime_sec: int = 0

    @property
    def tone(self) -> str:
        if not self.configured:
            return "muted"
        return "ok" if self.ok else "bad"

    @property
    def label(self) -> str:
        if not self.configured:
            return "не настроена"
        return "связь есть" if self.ok else "нет связи"


class RemnawaveClient:
    """HTTP-клиент панели. Один экземпляр на процесс, соединения переиспользуются."""

    def __init__(self) -> None:
        self._config = settings.remnawave
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        return self._config.is_configured

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._config.token.get_secret_value()}",
            "Accept": "application/json",
        }
        proxy_token = self._config.proxy_token.get_secret_value()
        if proxy_token:
            headers["X-Api-Key"] = proxy_token
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url.rstrip("/"),
                timeout=httpx.Timeout(self._config.timeout_sec),
                headers=self._headers(),
                verify=self._config.verify_tls,
            )
        return self._client

    async def request(self, method: str, path: str, *, json: Any = None) -> Any:
        """Запрос к панели с человеческим текстом ошибки вместо стектрейса."""
        if not self.is_configured:
            raise RemnawaveError(
                "Панель не настроена: " + ", ".join(self._config.missing_keys)
            )
        client = await self._get_client()
        try:
            response = await client.request(method, path, json=json)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise RemnawaveError(f"Панель недоступна: {exc}") from exc

        if response.status_code in (401, 403):
            raise RemnawaveError("Панель отклонила токен (REMNAWAVE_TOKEN)")
        if response.status_code == 404:
            raise RemnawaveError(f"Панель не знает путь {path} — проверьте переменные путей")
        if response.status_code == 409:
            raise UserExistsError(f"Учётка уже есть в панели: {path}")
        if response.status_code >= 400:
            # Панель объясняет отказ в теле: без этого «ответила 400» бесполезно.
            raise RemnawaveError(f"Панель ответила {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise RemnawaveError(f"Ответ панели на {path} — не JSON") from exc

    async def get(self, path: str) -> Any:
        return await self.request("GET", path)

    async def system_stats(self) -> dict[str, Any]:
        payload = unwrap(await self.get(self._config.stats_path))
        return payload if isinstance(payload, dict) else {}

    async def nodes(self) -> list[RemnaNode]:
        return [RemnaNode.parse(item) for item in as_list(await self.get(self._config.nodes_path))]

    async def hosts(self) -> list[RemnaHost]:
        return [RemnaHost.parse(item) for item in as_list(await self.get(self._config.hosts_path))]

    async def squads(self) -> list[RemnaSquad]:
        return [RemnaSquad.parse(item) for item in as_list(await self.get(self._config.squads_path))]

    async def find_user(self, username: str) -> RemnaUser | None:
        """Учётка по имени.

        Ищем перебором списка, а не отдельной ручкой: имя ручки поиска
        в разных версиях панели своё, а список мы и так умеем читать.
        """
        wanted = username.strip().lower()
        for item in as_list(await self.get(self._config.users_path)):
            if str(_pick(item, "username", default="")).strip().lower() == wanted:
                return RemnaUser.parse(item)
        return None

    async def create_user(
        self,
        *,
        username: str,
        expire_at: dt.datetime,
        telegram_id: int | None = None,
        description: str = "",
    ) -> RemnaUser:
        """Заводит клиента в панели и возвращает его ссылку подписки.

        Сквады обязательны: без них панель создаст учётку, но узлов в её
        подписке не будет, и роутер получит пустой список.
        """
        if not self._config.squad_uuids:
            raise RemnawaveError(
                "Не задан REMNAWAVE_SQUAD_UUIDS — без сквада подписка клиента будет пустой"
            )

        payload: dict[str, Any] = {
            "username": username,
            "status": "ACTIVE",
            "trafficLimitBytes": self._config.traffic_limit_bytes,
            "trafficLimitStrategy": "NO_RESET",
            "expireAt": expire_at.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
            "activeInternalSquads": list(self._config.squad_uuids),
        }
        if telegram_id:
            payload["telegramId"] = telegram_id
        if description:
            payload["description"] = description[:200]

        answer = unwrap(await self.request("POST", self._config.users_path, json=payload))
        if not isinstance(answer, dict):
            raise RemnawaveError("Панель ответила на создание учётки не объектом")

        created = RemnaUser.parse(answer)
        if not created.subscription_url:
            raise RemnawaveError("Панель завела учётку, но не вернула ссылку подписки")
        log.info("remnawave.user_created", username=username, uuid=created.uuid)
        return created

    async def update_expiry(self, *, uuid: str, expire_at: dt.datetime) -> RemnaUser:
        """Двигает срок учётки в панели.

        Нужно при продлении: срок ставится один раз при активации, и без этого
        доступ отключился бы по старой дате, хотя клиент оплатил следующий период.
        """
        payload = {
            "uuid": uuid,
            "expireAt": expire_at.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
        }
        answer = unwrap(await self.request("PATCH", self._config.users_path, json=payload))
        if not isinstance(answer, dict):
            raise RemnawaveError("Панель ответила на продление не объектом")
        log.info("remnawave.expiry_updated", uuid=uuid, expire_at=payload["expireAt"])
        return RemnaUser.parse(answer)

    async def probe(self) -> PanelStatus:
        """Состояние связи одним вызовом. Не бросает: страница обязана открыться."""
        status = PanelStatus(
            configured=self.is_configured,
            missing_keys=list(self._config.missing_keys),
            checked_at=dt.datetime.now(dt.UTC),
        )
        if not status.configured:
            return status
        try:
            stats = await self.system_stats()
            nodes = await self.nodes()
            hosts = await self.hosts()
        except RemnawaveError as exc:
            status.error = str(exc)
            log.warning("remnawave.probe_failed", error=str(exc))
            return status

        status.ok = True
        status.nodes_total = len(nodes)
        status.nodes_online = sum(1 for node in nodes if node.is_online and not node.is_disabled)
        status.hosts_total = len(hosts)
        status.version = str(_pick(stats, "version", default=""))
        status.uptime_sec = _as_int(_pick(stats, "uptime", "uptimeSec"))

        users = stats.get("users") if isinstance(stats.get("users"), dict) else stats
        status.users_total = _as_int(_pick(users, "totalUsers", "total", "usersTotal"))
        status.users_online = _as_int(_pick(users, "onlineLastMinute", "usersOnline", "online"))
        counts = users.get("statusCounts") if isinstance(users.get("statusCounts"), dict) else {}
        status.users_active = _as_int(counts.get("ACTIVE"), status.users_total)
        return status

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


_client: RemnawaveClient | None = None


def client() -> RemnawaveClient:
    global _client  # noqa: PLW0603 — один клиент на процесс, как у frp
    if _client is None:
        _client = RemnawaveClient()
    return _client


async def close_client() -> None:
    if _client is not None:
        await _client.aclose()
