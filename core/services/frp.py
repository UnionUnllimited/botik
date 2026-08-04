"""Работа с frp: дашборд frps и HTTP-доступ к роутерам через visitor-туннели.

Схема такая: роутер держит обратный туннель к frps на российской площадке.
Дашборд frps знает, кто сейчас на связи. Контейнер frpc рядом с нами работает
в режиме visitor и открывает локальный порт на каждый роутер — через него мы
зовём HTTP-API самого роутера, не имея к нему прямого доступа.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from core.config import settings
from core.security import normalize_mac

log = structlog.get_logger("services.frp")

_HEX = re.compile(r"^[0-9a-f]{12}$")


class FrpError(RuntimeError):
    """frps недоступен или ответил неожиданным образом."""


@dataclass(frozen=True, slots=True)
class FrpProxy:
    """Прокси в терминах frps: одна запись на туннель роутера."""

    name: str
    status: str
    mac: str
    kind: str
    """luci — веб-панель роутера, ssh — терминал."""
    traffic_in: int = 0
    traffic_out: int = 0
    last_start: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_online(self) -> bool:
        return self.status.lower() == "online"


def mac_from_proxy_name(name: str) -> str:
    """`luciA0B1C2D3E4F5` -> `A0:B1:C2:D3:E4:F5`. Пустая строка, если имя чужое."""
    lowered = name.strip().lower()
    for prefix in (settings.frp.luci_prefix, settings.frp.ssh_prefix):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix) :]
            break
    hex_only = lowered.replace(":", "").replace("-", "").replace("_", "")
    if not _HEX.match(hex_only):
        return ""
    return normalize_mac(hex_only)


def proxy_kind(name: str) -> str:
    lowered = name.strip().lower()
    if lowered.startswith(settings.frp.luci_prefix):
        return "luci"
    if lowered.startswith(settings.frp.ssh_prefix):
        return "ssh"
    return "unknown"


def proxy_names_for(mac: str) -> tuple[str, str]:
    """Имена прокси роутера по его MAC — так их называет прошивка."""
    hex_mac = mac.replace(":", "").lower()
    return f"{settings.frp.luci_prefix}{hex_mac}", f"{settings.frp.ssh_prefix}{hex_mac}"


class FrpsDashboard:
    """Клиент HTTP-API дашборда frps."""

    def __init__(self) -> None:
        self._config = settings.frp
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        return self._config.is_configured

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            password = self._config.dashboard_password.get_secret_value()
            credentials = f"{self._config.dashboard_user}:{password}"
            token = base64.b64encode(credentials.encode()).decode("ascii")
            self._client = httpx.AsyncClient(
                base_url=self._config.dashboard_url.rstrip("/"),
                timeout=httpx.Timeout(self._config.dashboard_timeout_sec),
                headers={"Authorization": f"Basic {token}"},
                verify=True,
            )
        return self._client

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    async def _get(self, path: str) -> dict[str, Any]:
        client = await self._get_client()
        response = await client.get(path)
        if response.status_code == 401:
            raise FrpError("frps: неверные логин или пароль дашборда")
        if response.status_code >= 400:
            raise FrpError(f"frps вернул {response.status_code} на {path}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise FrpError("frps: ответ не является JSON") from exc
        return payload if isinstance(payload, dict) else {}

    async def server_info(self) -> dict[str, Any]:
        return await self._get("/api/serverinfo")

    async def proxies(self, kind: str = "stcp") -> list[FrpProxy]:
        """Список туннелей. Роутеры публикуются через stcp."""
        payload = await self._get(f"/api/proxy/{kind}")
        result: list[FrpProxy] = []
        for item in payload.get("proxies") or []:
            name = str(item.get("name") or item.get("proxy_name") or "")
            if not name:
                continue
            mac = mac_from_proxy_name(name)
            if not mac:
                continue
            result.append(
                FrpProxy(
                    name=name,
                    status=str(item.get("status", "")),
                    mac=mac,
                    kind=proxy_kind(name),
                    traffic_in=int(item.get("today_traffic_in") or 0),
                    traffic_out=int(item.get("today_traffic_out") or 0),
                    last_start=item.get("last_start_time"),
                    raw=item,
                )
            )
        return result

    async def online_routers(self) -> dict[str, FrpProxy]:
        """MAC -> прокси веб-панели для тех, кто сейчас на связи."""
        return {
            proxy.mac: proxy for proxy in await self.proxies() if proxy.kind == "luci" and proxy.is_online
        }

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


_dashboard: FrpsDashboard | None = None


def dashboard() -> FrpsDashboard:
    global _dashboard  # noqa: PLW0603 — один клиент на процесс
    if _dashboard is None:
        _dashboard = FrpsDashboard()
    return _dashboard


async def close_dashboard() -> None:
    if _dashboard is not None:
        await _dashboard.aclose()


class RouterApi:
    """HTTP-клиент к самому роутеру через visitor-порт."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._config = settings.frp

    @property
    def base_url(self) -> str:
        return f"http://{self._config.visitor_host}:{self.port}"

    async def _request(self, path: str, *, method: str = "GET", **kwargs: Any) -> httpx.Response:
        """Сначала HTTP, затем HTTPS.

        Часть прошивок отдаёт панель только по HTTPS с самоподписанным
        сертификатом, поэтому проверка сертификата на втором заходе выключена:
        мы уже внутри туннеля к конкретному роутеру, подлинность которого
        подтверждена ключом STCP, а имя в сертификате — localhost.
        """
        host = self._config.visitor_host
        timeout = httpx.Timeout(self._config.router_http_timeout_sec)
        last_error: Exception | None = None

        for scheme, verify in (("http", True), ("https", False)):
            try:
                async with httpx.AsyncClient(
                    base_url=f"{scheme}://{host}:{self.port}", timeout=timeout, verify=verify
                ) as client:
                    return await client.request(method, path, **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc

        raise FrpError(f"Роутер недоступен через туннель: {last_error}")

    async def stats(self) -> dict[str, Any]:
        """Снимок состояния роутера: загрузка, память, клиенты, трафик."""
        response = await self._request(self._config.stats_path)
        if response.status_code >= 400:
            raise FrpError(f"Роутер ответил {response.status_code} на {self._config.stats_path}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise FrpError("Роутер вернул не JSON") from exc
        if not isinstance(payload, dict):
            raise FrpError("Неожиданный формат ответа роутера")
        return payload

    async def call(self, path: str, *, method: str = "GET", **kwargs: Any) -> dict[str, Any]:
        """Произвольный вызов к API роутера — для команд и выгрузки логов."""
        response = await self._request(path, method=method, **kwargs)
        if response.status_code >= 400:
            raise FrpError(f"Роутер ответил {response.status_code} на {path}")
        try:
            payload = response.json()
        except ValueError:
            return {"raw": response.text[:20000]}
        return payload if isinstance(payload, dict) else {"result": payload}
