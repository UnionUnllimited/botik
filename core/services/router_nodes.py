"""Узлы сервиса доступа на роутере: список, переключение, включение и выключение.

Всё это делает скрипт на самом устройстве, а мы его только зовём и читаем
ответ. Разбирать `uci` отсюда было бы короче на один файл, но разметка
конфигурации своя в каждой версии сервиса и меняется вместе с прошивкой:
сервер, который знает её наизусть, ломается от чужого обновления, и чинится
это выкатом сервера ради строчки, живущей на роутере. Со скриптом договор
один — имена команд и вид ответа, — и держит его та сторона, которая знает
свою конфигурацию.

Список берётся с устройства, а не из панели, по той же причине: в панели
лежит подписка, то есть набор узлов, который клиенту *выдан*; на роутере
лежит то, что он *получил* и чем реально пользуется. Подписка обновилась,
а роутер её ещё не перечитал — и выбор из панельного списка попал бы
в пустоту.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import structlog

from core.models import Device
from core.services import router_shell

log = structlog.get_logger("services.router_nodes")

SCRIPT = "/usr/bin/access_ctl.sh"
"""Скрипт прошивки: единственное, что трогает настройки сервиса доступа.

Рядом с `apply_sub.sh`, который прописывает подписку. Разделены намеренно:
подписку ставим мы при активации, узел выбирает клиент, и смешивать право
записи в одну точку незачем.
"""

_NODE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
"""Идентификатор узла приходит от клиента и уходит в командную строку.

Проверяется здесь по виду, а на роутере — по существованию: скрипт откажет,
если такого узла у него нет. Двух проверок хватает, и ни одна не заменяет
другую — эта не пускает в оболочку постороннее, та не даёт переключиться
в пустоту.
"""


class NodeError(RuntimeError):
    """Понятная причина отказа: текст уходит клиенту в приложение."""


_PREFIXES = ("router_", "router ", "titan_", "titan ")
"""Служебные приставки в названиях узлов.

Названия приходят из подписки и написаны для нас: `Router_Германия` говорит,
что узел роутерный, а не клиентский. Клиенту это не сообщает ничего — он
и так в приложении своего роутера, — а список из семи строк, начинающихся
одинаково, читается тяжелее, чем список из семи стран.
"""

_AUTO = ("titanswitch", "switch", "balancer", "балансер", "авто")
"""Как в подписке зовётся балансер.

Он выбирает узел сам и остаётся единственным путём вернуться к автовыбору:
клиент, разок ткнувший страну, без него остался бы на ней навсегда. Поэтому
не прячем, а называем словом, которое ему что-то говорит.
"""

AUTO_TITLE = "Авто"

_COUNTRIES = {
    "герман": "DE", "german": "DE",
    "финлянд": "FI", "finland": "FI",
    "польш": "PL", "poland": "PL",
    "нидерланд": "NL", "голланд": "NL", "netherland": "NL",
    "эстон": "EE", "estonia": "EE",
    "латв": "LV", "latvia": "LV",
    "литв": "LT", "lithuania": "LT",
    "швец": "SE", "sweden": "SE",
    "норвег": "NO", "norway": "NO",
    "дан": "DK", "denmark": "DK",
    "франц": "FR", "france": "FR",
    "испан": "ES", "spain": "ES",
    "итал": "IT", "italy": "IT",
    "швейцар": "CH", "switzerland": "CH",
    "австр": "AT", "austria": "AT",
    "чех": "CZ", "czech": "CZ",
    "румын": "RO", "romania": "RO",
    "болгар": "BG", "bulgaria": "BG",
    "великобритан": "GB", "англ": "GB", "britain": "GB", "london": "GB",
    "ирланд": "IE", "ireland": "IE",
    "сша": "US", "америк": "US", "usa": "US", "united states": "US",
    "канад": "CA", "canada": "CA",
    "турц": "TR", "turkey": "TR",
    "казахстан": "KZ", "kazakhstan": "KZ",
    "армен": "AM", "armenia": "AM",
    "груз": "GE", "georgia": "GE",
    "япон": "JP", "japan": "JP",
    "сингапур": "SG", "singapore": "SG",
    "гонконг": "HK", "hong kong": "HK",
    "оаэ": "AE", "эмират": "AE", "dubai": "AE",
    "росс": "RU", "russia": "RU",
}
"""По какому куску названия узнаётся страна.

Совпадение по началу слова, а не целиком: в подписке пишут и «Германия»,
и «Германия 2», и «Germany DE-1». Список неполный намеренно — незнакомая
страна остаётся без флага и читается ровно так же, как раньше, а гадать
по двум буквам значит однажды показать клиенту чужой флаг.
"""


def flag(name: str) -> str:
    """Флаг страны по названию узла. Не узнали — пустая строка."""
    lowered = name.lower()
    for needle, code in _COUNTRIES.items():
        if needle in lowered:
            return "".join(chr(0x1F1E6 + ord(letter) - ord("A")) for letter in code)
    return ""


def is_auto(raw: str) -> bool:
    """Балансер это или обычный узел."""
    lowered = (raw or "").lower()
    return any(needle in lowered for needle in _AUTO)


def display_name(raw: str, node_id: str) -> str:
    """Название узла так, как его стоит показать клиенту."""
    name = (raw or "").strip()
    lowered = name.lower()

    if is_auto(name):
        return AUTO_TITLE

    for prefix in _PREFIXES:
        if lowered.startswith(prefix):
            name = name[len(prefix):].strip()
            break

    # Приставку сняли, а под ней пусто — узел назван одной приставкой.
    # Лучше идентификатор, чем пустая строка в списке.
    return name.lstrip("_-— ").strip() or node_id


@dataclass(frozen=True, slots=True)
class Node:
    """Узел, как его видит клиент."""

    id: str
    name: str
    flag: str = ""
    auto: bool = False


@dataclass(frozen=True, slots=True)
class State:
    """Что сейчас настроено на роутере."""

    nodes: list[Node]
    current: str
    enabled: bool


def parse(payload: str) -> State:
    """Разбирает ответ скрипта.

    Незнакомые поля пропускаем молча: прошивка на парке обновляется не в один
    день, и новый ответ обязан читаться старым сервером, а старый — новым.
    """
    # Пустой ответ — это не «узлов нет»: скрипт обязан печатать состояние
    # всегда, и молчание означает, что его на устройстве не оказалось.
    # Прочитав молчание как пустой список, приложение показало бы клиенту
    # включённый сервис без единого узла — картину, которой не бывает.
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise NodeError("Роутер ответил неразборчиво") from exc
    if not isinstance(data, dict):
        raise NodeError("Роутер ответил неразборчиво")

    error = str(data.get("error") or "")
    if error:
        raise NodeError(_ERRORS.get(error, "Роутер отказал в настройке"))

    nodes: list[Node] = []
    for item in data.get("nodes") or []:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or "")
        if not node_id:
            continue
        raw = str(item.get("name") or "")
        title = display_name(raw, node_id)
        nodes.append(Node(id=node_id, name=title, flag=flag(title), auto=is_auto(raw)))

    # Балансер наверх: это возврат к автовыбору, и искать его в середине
    # списка стран человеку не приходится. Порядок остальных — как в
    # конфигурации: его задаёт подписка, и переставлять его нам незачем.
    nodes.sort(key=lambda node: not node.auto)

    return State(
        nodes=nodes,
        current=str(data.get("current") or ""),
        # Отсутствие поля — не «выключено»: старая прошивка его не пишет,
        # а сервис у неё работает. Молчание читаем как «включено».
        enabled=bool(data.get("enabled", True)),
    )


_ERRORS = {
    "unknown_node": "Этого узла на роутере больше нет — обновите список",
    "busy": "Роутер уже применяет предыдущую настройку",
    "no_nodes": "На роутере пока нет узлов: подписка ещё не прочитана",
    "no_service": "На этом роутере сервис доступа не настроен",
    "commit_failed": "Роутер не смог сохранить настройку",
    # Наша ошибка, а не клиентская: скрипт не понял, что мы ему передали.
    # Клиенту всё равно нужен текст, а разбираться по нему нам — в журнале.
    "bad_usage": "Роутер не понял команду",
}
"""Коды скрипта человеческим языком. Незнакомый код — общий отказ: прошивка
может научиться новым раньше, чем сервер о них узнает."""


async def _call(device: Device, *args: str) -> State:
    """Зовёт скрипт и разбирает ответ.

    Ненулевой код возврата разбираем всё равно: скрипт кладёт причину в тот же
    ответ, и «узел не найден» ценнее, чем «команда не выполнилась».
    """
    command = " ".join([SCRIPT, *args])
    result = await router_shell.run(device, command)
    if not result.stdout.strip() and not result.ok:
        raise NodeError("Роутер не ответил на запрос настроек")
    return parse(result.stdout)


async def read(device: Device) -> State:
    """Какие узлы есть у роутера и какой из них выбран."""
    return await _call(device, "list")


async def select(device: Device, node_id: str) -> State:
    """Переключает активный узел и возвращает состояние после правки."""
    if not _NODE_ID.match(node_id or ""):
        raise NodeError("Неизвестный узел")
    state = await _call(device, "use", node_id)
    log.info("router_nodes.selected", device_id=device.id, mac=device.mac, node=node_id)
    return state


async def set_enabled(device: Device, enabled: bool) -> State:
    """Включает или выключает сервис доступа целиком.

    Российские сайты и так идут напрямую, поэтому выключать нужно редко — но
    нужно: бывает сервис, который спотыкается именно о зарубежный маршрут,
    и клиент без этой кнопки идёт в поддержку.
    """
    state = await _call(device, "on" if enabled else "off")
    log.info("router_nodes.toggled", device_id=device.id, mac=device.mac, enabled=enabled)
    return state
