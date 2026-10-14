"""Кто подключён к домашней сети клиента.

Вопрос «а кто это у меня сидит» задают, когда интернет стал медленнее,
и раньше ответить на него можно было только из панели роутера. Теперь
список виден в боте и в приложении — по именам, которые устройства сами
называют роутеру, с отметкой «сейчас в сети».

Три источника одним запросом: аренды DHCP (имя и адрес), таблица соседей
(кто отвечал только что) и список подключённых по Wi-Fi. Ни один не полон
в одиночку: аренда живёт сутки после ухода телефона, соседи не знают имён,
а Wi-Fi не видит кабеля.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.models import Device
from core.services import router_shell

COMMAND = (
    "cat /tmp/dhcp.leases 2>/dev/null; echo '#--'; "
    "ip -4 neigh show 2>/dev/null; echo '#--'; "
    "for i in $(ls /sys/class/net 2>/dev/null); do "
    "[ -d /sys/class/net/$i/wireless ] || [ -d /sys/class/net/$i/phy80211 ] || continue; "
    "iw dev $i station dump 2>/dev/null | grep -i '^station'; "
    "iwinfo $i assoclist 2>/dev/null | grep -iE '^([0-9a-f]{2}:){5}'; "
    "done"
)
"""Всё, что выводится, — с самого роутера, и ничего от клиента в команду
не попадает. `iw` и `iwinfo` оба: на одной прошивке есть один, на другой
другой, а пустой ответ одного не значит, что по Wi-Fi никого нет."""

_MAC = re.compile(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})", re.IGNORECASE)

NAME_LIMIT = 64

ACTIVE_STATES = frozenset({"REACHABLE", "DELAY", "PROBE", "PERMANENT"})
"""Состояния соседа «отвечал только что». STALE — давно не отвечал: телефон
мог уйти из дома час назад, а запись ещё живёт."""

PHONE = (
    "iphone",
    "android",
    "galaxy",
    "redmi",
    "xiaomi",
    "huawei",
    "honor",
    "pixel",
    "oneplus",
    "realme",
    "poco",
    "oppo",
    "vivo",
    "nokia",
    "phone",
    "ipad",
    "tablet",
)
COMPUTER = (
    "desktop",
    "laptop",
    "macbook",
    "imac",
    "-pc",
    "pc-",
    "notebook",
    "windows",
    "thinkpad",
    "ноутбук",
    "компьютер",
    "mac-mini",
    "surface",
)
TV = (
    "tv",
    "bravia",
    "tizen",
    "webos",
    "chromecast",
    "firetv",
    "kinopoisk",
    "yandex",
    "alice",
    "roku",
    "mibox",
)
"""По чему узнаётся вид устройства. Угадываем по имени, которое оно само
сообщило роутеру, — значок только помогает взгляду, а имя остаётся рядом."""


@dataclass(frozen=True, slots=True)
class HomeDevice:
    mac: str
    ip: str
    name: str
    active: bool
    wifi: bool | None
    kind: str

    def as_dict(self) -> dict:
        return {
            "mac": self.mac,
            "ip": self.ip,
            "name": self.name,
            "active": self.active,
            "wifi": self.wifi,
            "kind": self.kind,
        }


def kind_of(name: str) -> str:
    lowered = name.lower()
    if any(word in lowered for word in TV):
        return "tv"
    if any(word in lowered for word in PHONE):
        return "phone"
    if any(word in lowered for word in COMPUTER):
        return "computer"
    return "other"


def _sections(payload: str) -> list[str]:
    parts = payload.split("#--")
    return [*parts, "", "", ""][:3]


def _ip_key(ip: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in ip.split("."))
    except ValueError:
        return (999,)


def parse(payload: str) -> list[HomeDevice]:
    leases_raw, neigh_raw, wifi_raw = _sections(payload)

    leases: dict[str, tuple[str, str]] = {}
    for line in leases_raw.splitlines():
        parts = line.split()
        if len(parts) < 4 or not _MAC.fullmatch(parts[1]):
            continue
        # Имя называет само устройство — гость в Wi-Fi может прислать любое.
        # Обрезаем: длинное уронило бы экран бота о предел длины сообщения.
        name = "" if parts[3] == "*" else parts[3][:NAME_LIMIT]
        leases[parts[1].upper()] = (parts[2][:45], name)

    neighbours: dict[str, tuple[str, bool]] = {}
    for line in neigh_raw.splitlines():
        parts = line.split()
        if "lladdr" not in parts or "dev" not in parts:
            continue
        mac = parts[parts.index("lladdr") + 1].upper() if parts.index("lladdr") + 1 < len(parts) else ""
        dev = parts[parts.index("dev") + 1] if parts.index("dev") + 1 < len(parts) else ""
        if not _MAC.fullmatch(mac):
            continue
        # Соседи на внешнем порту — это провайдер, а не домашние устройства.
        # Домашняя сеть — мост (`br-lan`, `br-guest`) или то, что брало аренду.
        if not dev.startswith("br-") and mac not in leases:
            continue
        neighbours[mac] = (parts[0][:45], parts[-1].upper() in ACTIVE_STATES)

    wifi_known = bool(wifi_raw.strip())
    on_wifi = {match.upper() for match in _MAC.findall(wifi_raw)}

    devices: list[HomeDevice] = []
    for mac in set(leases) | set(neighbours):
        ip, name = leases.get(mac, ("", ""))
        neigh_ip, reachable = neighbours.get(mac, ("", False))
        devices.append(
            HomeDevice(
                mac=mac,
                ip=ip or neigh_ip,
                name=name,
                active=reachable or mac in on_wifi,
                wifi=(mac in on_wifi) if wifi_known else None,
                kind=kind_of(name),
            )
        )
    # Сначала те, кто в сети сейчас, внутри — по адресу: так список не
    # прыгает между обновлениями, и своё устройство находится на том же месте.
    devices.sort(key=lambda item: (not item.active, _ip_key(item.ip)))
    return devices


async def read(device: Device) -> list[HomeDevice]:
    result = await router_shell.run(device, COMMAND)
    return parse(result.stdout)
