"""Wi-Fi роутера: какие сети раздаёт, с каким паролем, и смена пароля.

Самый частый вопрос после «не открывается сайт» — «какой у меня пароль
от Wi-Fi»: наклейку с коробки потеряли, гость просит подключиться. Раньше
ответ был один — открыть панель роутера из домашней сети, а это значит
вспомнить адрес и пароль от самой панели. Теперь пароль виден в боте
и в приложении, и там же его можно сменить.

Читаем `uci` напрямую, без скрипта на роутере (как у узлов): раздел
`wireless` — стандартная часть OpenWrt, его разметка не менялась годами
и одинакова на любой прошивке.

**Пароль клиента в командную строку не попадает.** Он уходит роутеру на вход
(`stdin`) и читается `read`: оболочка не толкует его как текст команды,
и в журнал команд он не пишется. Проверка вида ниже — второй рубеж, а не
единственный.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

from core.models import Device
from core.services import router_shell

log = structlog.get_logger("services.router_wifi")

READ_COMMAND = "uci -X show wireless 2>/dev/null"
"""`-X` — настоящие имена разделов (`cfg033579`), а не `@wifi-iface[0]`:
по порядковому номеру сменить пароль можно не той сети, если порядок
поменялся между чтением и записью."""

_SECTION = re.compile(r"^[A-Za-z0-9_]{1,64}$")
"""Имя раздела уходит в командную строку смены пароля. Берём его из ответа
роутера, но проверяем всё равно: отвечает устройство у клиента дома."""

_LINE = re.compile(r"^wireless\.([A-Za-z0-9_]+)(?:\.([A-Za-z0-9_]+))?=(.*)$")

PASSWORD_MIN = 8
PASSWORD_MAX = 63
"""Пределы WPA2: короче восьми роутер не примет, длиннее 63 — уже не пароль,
а ключ в шестнадцатеричном виде."""

_PASSWORD = re.compile(r"^[A-Za-z0-9!#$%&()*+,\-./:;<=>?@\[\]^_{|}~]+$")
"""Латиница, цифры и знаки — без пробела, кавычек и обратной черты.

Пробелы WPA допускает, но пароль с пробелом на конце человек не наберёт
на телевизоре никогда — и не поймёт почему. Кавычки и обратная черта
ломают ввод на половине устройств и в настройках роутера экранируются
по-разному. Кириллицы WPA не знает вовсе."""

TOO_SIMPLE = frozenset(
    {
        "12345678",
        "123456789",
        "1234567890",
        "87654321",
        "11111111",
        "00000000",
        "88888888",
        "12341234",
        "password",
        "password1",
        "qwertyui",
        "qwerty123",
        "qwertyuiop",
        "iloveyou",
        "11223344",
        "1q2w3e4r",
        "abcd1234",
    }
)
"""Пароли, которые подбирают первыми. Не полный словарь, а самые частые:
отказ на них ничего не стоит, а сосед на чужом канале — стоит."""

_OPEN = frozenset({"none", "owe", ""})
"""Шифрование без пароля. Такую сеть пароль не касается: открытая гостевая
сеть сделана открытой намеренно, и закрывать её молча нельзя."""


class WifiError(RuntimeError):
    """Понятная причина отказа: текст уходит клиенту."""


@dataclass(frozen=True, slots=True)
class Network:
    id: str
    ssid: str
    band: str
    password: str = field(repr=False)
    open: bool

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "ssid": self.ssid,
            "band": self.band,
            "password": self.password,
            "open": self.open,
        }


def check_password(value: str) -> str:
    """Текст отказа или пусто. Тот же текст видит клиент — и в боте,
    и в приложении, — поэтому он про то, что исправить, а не про правило."""
    if not value:
        return "Пришлите новый пароль."
    if len(value) < PASSWORD_MIN:
        return f"Короче {PASSWORD_MIN} символов роутер не примет."
    if len(value) > PASSWORD_MAX:
        return f"Слишком длинный: не больше {PASSWORD_MAX} символов."
    # `fullmatch`, а не `match` с `$`: `$` пропускает перевод строки в конце,
    # и «1234567» с ним прошёл бы как восемь символов, а на роутер лёг семью.
    if not _PASSWORD.fullmatch(value):
        return "Только латинские буквы, цифры и знаки — без пробелов, кавычек и кириллицы."
    if value.lower() in TOO_SIMPLE or len(set(value)) < 3:
        return "Слишком простой — такие подбирают первыми. Добавьте букв или знаков."
    return ""


def _unquote(raw: str) -> str:
    """`'a'\\''b'` → `a'b`: так `uci show` пишет значение с кавычкой внутри."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("'\\''", "'")
    return text


def _band(radio: dict[str, str]) -> str:
    band = radio.get("band", "").lower()
    if band in ("2g", "5g", "6g", "60g"):
        return {"2g": "2,4 ГГц", "5g": "5 ГГц", "6g": "6 ГГц", "60g": "60 ГГц"}[band]
    hwmode = radio.get("hwmode", "").lower()
    if hwmode.endswith("a") or hwmode.endswith("ac"):
        return "5 ГГц"
    if hwmode:
        return "2,4 ГГц"
    try:
        channel = int(radio.get("channel", "") or 0)
    except ValueError:
        channel = 0
    if channel > 14:
        return "5 ГГц"
    return "2,4 ГГц" if channel else ""


def parse(payload: str) -> list[Network]:
    """Сети, которые роутер раздаёт: точки доступа на включённых радио."""
    sections: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for line in payload.splitlines():
        found = _LINE.match(line.strip())
        if not found:
            continue
        name, option, raw = found.groups()
        if option is None:
            sections.setdefault(name, {})["="] = raw.strip()
            if name not in order:
                order.append(name)
            continue
        sections.setdefault(name, {})[option] = _unquote(raw)

    networks: list[Network] = []
    for name in order:
        values = sections[name]
        if values.get("=") != "wifi-iface" or not _SECTION.match(name):
            continue
        if values.get("mode", "ap") != "ap" or values.get("disabled") == "1":
            continue
        radio = sections.get(values.get("device", ""), {})
        if radio.get("disabled") == "1":
            continue
        encryption = values.get("encryption", "").split("+", 1)[0].lower()
        networks.append(
            Network(
                id=name,
                ssid=values.get("ssid", ""),
                band=_band(radio),
                password="" if encryption in _OPEN else values.get("key", ""),
                open=encryption in _OPEN,
            )
        )
    return networks


async def read(device: Device) -> list[Network]:
    result = await router_shell.run(device, READ_COMMAND)
    networks = parse(result.stdout)
    if not networks:
        raise WifiError("На роутере не нашлось ни одной включённой сети Wi-Fi.")
    return networks


def change_command(sections: list[str]) -> str:
    """Команда смены пароля. Сам пароль в неё не входит — его читает `read`."""
    names = " ".join(sections)
    # Каждая запись проверяется: не легла хоть одна — не сохраняем вовсе.
    # Иначе 2,4 ГГц остался бы со старым паролем, 5 ГГц — с новым, а клиенту
    # пришло бы «пароль сменён».
    return (
        f'read -r k; good=1; for s in {names}; do uci set "wireless.$s.key=$k" || good=0; done; '
        '[ "$good" = 1 ] && uci commit wireless && { wifi reload >/dev/null 2>&1; echo ok; }'
    )


async def change_password(device: Device, password: str) -> list[Network]:
    """Меняет пароль у всех закрытых сетей роутера и возвращает их после правки.

    Один пароль на все сети: у роутера с двумя диапазонами это одна домашняя
    сеть в глазах клиента, и разные пароли на 2,4 и 5 ГГц — это ровно та
    путаница, из-за которой телевизор «не видит Wi-Fi».
    """
    error = check_password(password)
    if error:
        raise WifiError(error)
    networks = await read(device)
    targets = [network.id for network in networks if not network.open and _SECTION.match(network.id)]
    if not targets:
        raise WifiError("На роутере нет сетей с паролем — менять нечего.")

    result = await router_shell.run(device, change_command(targets), stdin=password + "\n")
    if result.stdout.strip().splitlines()[-1:] != ["ok"]:
        log.warning("router_wifi.change_failed", device_id=device.id, exit_status=result.exit_status)
        raise WifiError("Роутер не сохранил новый пароль. Попробуйте ещё раз через минуту.")
    log.info("router_wifi.password_changed", device_id=device.id, mac=device.mac, networks=len(targets))
    return [
        Network(n.id, n.ssid, n.band, password if n.id in targets else n.password, n.open) for n in networks
    ]
