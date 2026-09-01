"""Выполнение команд на роутере по SSH через visitor-туннель.

Ключ хоста не проверяется намеренно: мы подключаемся не к «серверу с именем»,
а к локальному порту туннеля, подлинность которого уже подтверждена ключом
STCP на уровне frp. Проверять при этом отпечаток localhost бессмысленно —
он свой у каждого роутера и меняется при перепрошивке.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import asyncssh
import structlog

from core.config import settings
from core.models import Device
from core.security import decrypt_secret, encrypt_secret

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
    # Туннель — первое, о чём спрашивают, когда роутер «на связи», а команды
    # не доходят: связь показывает frps, а работает через frpc на роутере.
    "frp_status": (
        "Туннель: состояние",
        "/etc/init.d/frpc status 2>&1; ps | grep -c '[f]rpc'",
    ),
    "frp_log": ("Туннель: лог", "logread -e frpc 2>/dev/null | tail -n 40"),
    "frp_restart": ("Туннель: перезапустить", "/etc/init.d/frpc restart && sleep 1 && echo ok"),
    # Обновление прошивки вне суточного круга: скрипт на роутере сам идёт
    # за манифестом и ставит образ.
    #
    # Запускаем в фоне и сразу отпускаем сессию. Синхронно нельзя: образ
    # весит 27–54 МБ, а `ssh_timeout_sec` — пятнадцать секунд, и команда
    # обрывалась бы по таймауту на середине закачки. Оператор при этом
    # видел бы отказ у обновления, которое на самом деле идёт.
    "ota_now": (
        "Обновить прошивку",
        "nohup titan_ota.sh now >/tmp/titan_ota.log 2>&1 & echo запущено",
    ),
    # Чем кончилось — отдельной кнопкой: ответ приходит минутами позже,
    # а после успешной установки роутер уходит в перезагрузку и молчит.
    "ota_log": (
        "Обновление: лог",
        "tail -n 40 /tmp/titan_ota.log 2>/dev/null; logread -e titan_ota 2>/dev/null | tail -n 40",
    ),
    "lists": (
        "Списки на роутере",
        "ls -la /etc/*.lst 2>/dev/null; wc -l /etc/*.lst 2>/dev/null",
    ),
    "log_tail": ("Системный лог", "logread | tail -n 60"),
}
"""Готовые команды под кнопками в карточке.

Набор закрытый и в коде, а не в базе: это ровно те вопросы, которые задают
роутеру каждый раз, и вводить их руками — лишний повод опечататься в том,
что уходит на устройство клиента."""


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


def derive_password(mac: str, salt: str) -> str:
    """Пароль root, как его считает прошивка при первом запуске.

    MAC без разделителей в нижнем регистре, к нему приписывается соль,
    от строки берётся sha256 и первые 16 символов шестнадцатеричной записи.
    """
    normalized = mac.replace(":", "").replace("-", "").lower()
    digest = hashlib.sha256(f"{normalized}{salt}".encode()).hexdigest()
    return digest[:16]


def passwords_for(device: Device) -> list[str]:
    """Пароли, которыми пробуем войти, в порядке от точного к общему.

    Их несколько, потому что парк смешанный: часть роутеров ещё со стоковым
    паролем, часть уже перепрошита и считает его из MAC. Раньше выбирался
    ровно один — заданная соль отменяла статический пароль, и стоковый роутер
    не открывался вовсе, хотя пароль от него известен и лежит в настройках.
    """
    candidates: list[str] = []

    if device.ssh_password_enc:
        try:
            candidates.append(decrypt_secret(device.ssh_password_enc, aad=f"ssh:{device.id}"))
        except Exception:  # noqa: BLE001 — ключ шифрования мог смениться
            log.warning("router_shell.device_password_unreadable", device_id=device.id)

    salt = settings.frp.ssh_password_salt.get_secret_value()
    if salt:
        candidates.append(derive_password(device.mac, salt))

    static = settings.frp.ssh_password.get_secret_value()
    if static:
        candidates.append(static)

    # Повторы убираем: одинаковый пароль дважды — это лишний отказ логина
    # в журнале роутера и лишняя секунда ожидания на каждой команде.
    return list(dict.fromkeys(password for password in candidates if password))


def password_for(device: Device) -> str:
    """Первый подходящий пароль. Для показа оператору — там нужен один."""
    candidates = passwords_for(device)
    return candidates[0] if candidates else ""


def store_password(device: Device, password: str) -> None:
    """Сохраняет индивидуальный пароль роутера в зашифрованном виде."""
    device.ssh_password_enc = encrypt_secret(password, aad=f"ssh:{device.id}") if password else None


async def connect(
    device: Device,
    *,
    timeout: float | None = None,  # noqa: ASYNC109 — предел уходит в asyncssh, а не в asyncio.timeout
):
    """Открывает SSH-соединение с роутером, перебирая известные пароли.

    Возвращает соединение — закрывать его вызывающему. Отдельной функцией,
    потому что подбор пароля нужен и разовой команде, и живому терминалу,
    а две копии этого цикла разошлись бы на первой же правке.

    Перебираем только отказ логина: на недоступном туннеле следующий пароль
    ничего не изменит, а лишние попытки удвоят ожидание там, где отвечать
    всё равно некому.
    """
    candidates = passwords_for(device)
    if not candidates:
        raise ShellError("Пароль SSH неизвестен: задайте FRP_SSH_PASSWORD_SALT или пароль роутера вручную")

    port = ssh_port_for(device)
    limit = timeout or settings.frp.ssh_timeout_sec

    denied: Exception | None = None
    for attempt, password in enumerate(candidates, start=1):
        try:
            return await asyncssh.connect(
                settings.frp.visitor_host,
                port=port,
                username=settings.frp.ssh_user,
                password=password,
                known_hosts=None,
                connect_timeout=limit,
                login_timeout=limit,
            )
        except asyncssh.PermissionDenied as exc:
            denied = exc
            log.info(
                "router_shell.password_rejected",
                device_id=device.id,
                mac=device.mac,
                attempt=attempt,
                of=len(candidates),
            )
        except (TimeoutError, asyncssh.Error, OSError) as exc:
            raise ShellError(f"Не удалось подключиться к роутеру: {exc}") from exc

    raise ShellError("Роутер отклонил логин или пароль SSH") from denied


async def run(
    device: Device,
    command: str,
    *,
    timeout: float | None = None,  # noqa: ASYNC109 — предел задаёт вызывающий, сессия своя на команду
) -> CommandResult:
    """Выполняет одну команду и возвращает её вывод."""
    limit = timeout or settings.frp.ssh_timeout_sec
    connection = await connect(device, timeout=limit)
    try:
        result = await connection.run(command, check=False, timeout=limit)
    except (TimeoutError, asyncssh.Error, OSError) as exc:
        raise ShellError(f"Не удалось выполнить команду: {exc}") from exc
    finally:
        connection.close()

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
