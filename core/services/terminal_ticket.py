"""Разовый билет на живой терминал роутера — для админки бота.

Устроен так же, как билет на веб-панель (`panel_ticket`), и по той же причине:
SSH к роутеру идёт через туннель, который держит наш контейнер, дотянуться
до устройства может только процесс в нашей сети, а вход в админку — не наш.
Их админка просит билет по общему токену, браузер меняет его на короткую
сессию, и уже она открывает вебсокет.

Отдельным модулем, а не полем в билете панели: у панели цель — порт HTTP,
у терминала — устройство, у сессий разное время жизни, и складывать их
в одну запись значит однажды открыть панель там, где ждали терминал.

Сессия короче панельной намеренно. Панель можно оставить открытой на полчаса
и ничего не сломать, а терминал — это root на устройстве клиента: забытая
вкладка не должна оставаться живой дверью.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass

import structlog
from itsdangerous import BadSignature, URLSafeSerializer

from core.config import settings
from core.redis_client import get_redis

log = structlog.get_logger("services.terminal_ticket")

COOKIE = "rs_term"
TICKET_TTL_SEC = 120
"""Столько живёт билет: он нужен ровно на один переход из админки в браузер."""
SESSION_TTL_SEC = 10 * 60
"""Столько живёт открытый терминал. Дальше оператор нажимает кнопку заново."""


@dataclass(slots=True)
class TerminalTarget:
    device_id: int
    mac: str


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(
        settings.security.secret_key.get_secret_value(), salt="router-terminal"
    )


def _ticket_key(ticket_id: str) -> str:
    return settings.redis.key("terminal_ticket", ticket_id)


def _session_key(session_id: str) -> str:
    return settings.redis.key("terminal_session", session_id)


async def issue(*, device_id: int, mac: str) -> str:
    """Выдаёт билет. Возвращает значение для параметра `ticket` в ссылке."""
    ticket_id = secrets.token_urlsafe(32)
    target = TerminalTarget(device_id=device_id, mac=mac)
    await get_redis().set(_ticket_key(ticket_id), json.dumps(asdict(target)), ex=TICKET_TTL_SEC)
    log.info("terminal.ticket_issued", device_id=device_id, mac=mac)
    return _serializer().dumps(ticket_id)


async def redeem(raw_ticket: str) -> tuple[str, TerminalTarget] | None:
    """Гасит билет и заводит сессию терминала. Возвращает значение куки и цель."""
    if not raw_ticket:
        return None
    try:
        ticket_id = _serializer().loads(raw_ticket)
    except BadSignature:
        log.warning("terminal.ticket_bad_signature")
        return None

    # GETDEL: два одновременных перехода по одной ссылке не откроют две сессии.
    stored = await get_redis().getdel(_ticket_key(ticket_id))
    if stored is None:
        return None
    try:
        target = TerminalTarget(**json.loads(stored))
    except (TypeError, ValueError):
        return None

    session_id = secrets.token_urlsafe(32)
    await get_redis().set(_session_key(session_id), json.dumps(asdict(target)), ex=SESSION_TTL_SEC)
    log.info("terminal.opened", device_id=target.device_id, mac=target.mac)
    return _serializer().dumps(session_id), target


async def load(raw_cookie: str | None) -> TerminalTarget | None:
    """Цель по куке терминала. Пусто — сессии нет или она истекла."""
    if not raw_cookie:
        return None
    try:
        session_id = _serializer().loads(raw_cookie)
    except BadSignature:
        return None
    stored = await get_redis().get(_session_key(session_id))
    if stored is None:
        return None
    try:
        return TerminalTarget(**json.loads(stored))
    except (TypeError, ValueError):
        return None
