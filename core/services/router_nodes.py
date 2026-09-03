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


@dataclass(frozen=True, slots=True)
class Node:
    """Узел, как его видит клиент."""

    id: str
    name: str


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
        nodes.append(Node(id=node_id, name=str(item.get("name") or "").strip() or node_id))

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
