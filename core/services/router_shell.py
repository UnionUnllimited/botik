"""Выполнение команд на роутере по SSH через visitor-туннель.

Ключ хоста не проверяется намеренно: мы подключаемся не к «серверу с именем»,
а к локальному порту туннеля, подлинность которого уже подтверждена ключом
STCP на уровне frp. Проверять при этом отпечаток localhost бессмысленно —
он свой у каждого роутера и меняется при перепрошивке.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncssh
import structlog

from core.config import settings
from core.models import Device
from core.security import decrypt_secret

log = structlog.get_logger("services.router_shell")

# Готовые команды: их же собирала прошлая панель.
LOG_SOURCES: dict[str, tuple[str, str]] = {
    "system": ("Система", "logread | tail -n {lines}"),
    "kernel": ("Ядро", "dmesg | tail -n {lines}"),
    "service": ("Сервис доступа", "logread | grep -i passwall | tail -n {lines}"),
    "tunnel": ("Туннель frpc", "logread | grep -i frpc | tail -n {lines}"),
    "bypass": ("Обход блокировок", "logread | grep -i nfqws | tail -n {lines}"),
}

QUICK_COMMANDS: dict[str, tuple[str, str]] = {
    "uptime": ("Аптайм и нагрузка", "uptime; cat /proc/loadavg"),
    "memory": ("Память и диск", "free; df -h"),
    "network": ("Сеть", "ip -4 addr; ip route"),
    "clients": ("Клиенты в сети", "cat /tmp/dhcp.leases 2>/dev/null | tail -n 40"),
    "service_status": ("Статус сервиса доступа", "uci show passwall.@global[0] 2>/dev/null | head -n 20"),
    "processes": ("Процессы", "ps | head -n 30"),
}


class ShellError(RuntimeError):
    """Не удалось подключиться к роутеру или выполнить команду."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: str
    exit_status: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        parts = [self.stdout.rstrip()]
        if self.stderr.strip():
            parts.append(f"[stderr]\n{self.stderr.rstrip()}")
        return "\n".join(part for part in parts if part) or "(пустой ответ)"

    @property
    def ok(self) -> bool:
        return self.exit_status == 0


def ssh_port_for(device: Device) -> int:
    """Порт SSH-туннеля: порт панели плюс смещение из настроек."""
    if not device.frp_visitor_port:
        raise ShellError("Для роутера ещё не выделен туннель")
    return device.frp_visitor_port + settings.frp.ssh_visitor_offset


def password_for(device: Device) -> str:
    """Пароль устройства, если задан индивидуально, иначе общий из настроек."""
    if device.secret_enc:
        try:
            return decrypt_secret(device.secret_enc, aad=f"ssh:{device.id}")
        except Exception:  # noqa: BLE001 — поле могло хранить не пароль SSH
            log.debug("router_shell.device_password_unreadable", device_id=device.id)
    return settings.frp.ssh_password.get_secret_value()


async def run(
    device: Device,
    command: str,
    *,
    timeout: float | None = None,  # noqa: ASYNC109 — предел задаёт вызывающий, сессия своя на команду
) -> CommandResult:
    """Выполняет одну команду и возвращает её вывод."""
    password = password_for(device)
    if not password:
        raise ShellError("Не задан пароль SSH: заполните FRP_SSH_PASSWORD")

    port = ssh_port_for(device)
    limit = timeout or settings.frp.ssh_timeout_sec

    try:
        async with asyncssh.connect(
            settings.frp.visitor_host,
            port=port,
            username=settings.frp.ssh_user,
            password=password,
            known_hosts=None,
            connect_timeout=limit,
            login_timeout=limit,
        ) as connection:
            result = await connection.run(command, check=False, timeout=limit)
    except asyncssh.PermissionDenied as exc:
        raise ShellError("Роутер отклонил логин или пароль SSH") from exc
    except (TimeoutError, asyncssh.Error, OSError) as exc:
        raise ShellError(f"Не удалось подключиться к роутеру: {exc}") from exc

    log.info(
        "router_shell.command",
        device_id=device.id,
        mac=device.mac,
        exit_status=result.exit_status,
        command=command[:120],
    )
    return CommandResult(
        command=command,
        exit_status=result.exit_status if result.exit_status is not None else -1,
        stdout=str(result.stdout or "")[:60000],
        stderr=str(result.stderr or "")[:8000],
    )


async def read_log(device: Device, source: str, *, lines: int = 200) -> CommandResult:
    """Забирает журнал роутера по одной из известных категорий."""
    entry = LOG_SOURCES.get(source)
    if entry is None:
        raise ShellError(f"Неизвестный журнал: {source}")
    _, template = entry
    return await run(device, template.format(lines=max(min(lines, 1000), 10)))


async def run_quick(device: Device, name: str) -> CommandResult:
    entry = QUICK_COMMANDS.get(name)
    if entry is None:
        raise ShellError(f"Неизвестная команда: {name}")
    return await run(device, entry[1])
