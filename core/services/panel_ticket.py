"""Разовый билет на веб-панель роутера — для админки бота.

Панель отдаёт наш процесс: туннель к роутеру держит контейнер `frpc` в нашей
docker-сети, и дотянуться до него может только он. Перенести проксирование
к ним нельзя физически, поэтому переносится вход: их админка нажимает кнопку,
мы выдаём одноразовую ссылку, браузер оператора уходит на панель. Вход в нашу
админку для этого больше не нужен — она удаляется, а панель остаётся.

Почему билет, а не сразу кука: ссылку выдаёт их процесс по общему токену,
и если бы он ставил куку сам, секрет ушёл бы в браузер. Билет живёт две минуты,
гасится при первом предъявлении и меняется на короткую сессию панели.

Порт туннеля запоминается в момент выдачи — так же, как это делает наша админка.
Порт может смениться при перепривязке, тогда панель открывается заново.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass

import structlog
from itsdangerous import BadSignature, URLSafeSerializer

from core.config import settings
from core.redis_client import get_redis

log = structlog.get_logger("services.panel_ticket")

COOKIE = "rs_panel"
TICKET_TTL_SEC = 120
"""Столько живёт билет: он нужен ровно на один переход из админки в браузер."""
SESSION_TTL_SEC = 30 * 60
"""Столько живёт открытая панель. Дальше оператор нажимает кнопку заново."""


@dataclass(slots=True)
class PanelTarget:
    device_id: int
    port: int
    mac: str


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(settings.security.secret_key.get_secret_value(), salt="router-panel")


def _ticket_key(ticket_id: str) -> str:
    return settings.redis.key("panel_ticket", ticket_id)


def _session_key(session_id: str) -> str:
    return settings.redis.key("panel_session", session_id)


async def issue(*, device_id: int, port: int, mac: str) -> str:
    """Выдаёт билет. Возвращает значение для параметра `ticket` в ссылке."""
    ticket_id = secrets.token_urlsafe(32)
    target = PanelTarget(device_id=device_id, port=port, mac=mac)
    await get_redis().set(_ticket_key(ticket_id), json.dumps(asdict(target)), ex=TICKET_TTL_SEC)
    log.info("panel.ticket_issued", device_id=device_id, mac=mac)
    return _serializer().dumps(ticket_id)


async def redeem(raw_ticket: str) -> tuple[str, PanelTarget] | None:
    """Гасит билет и заводит сессию панели. Возвращает значение куки и цель."""
    if not raw_ticket:
        return None
    try:
        ticket_id = _serializer().loads(raw_ticket)
    except BadSignature:
        log.warning("panel.ticket_bad_signature")
        return None

    # GETDEL: два одновременных перехода по одной ссылке не откроют две сессии.
    stored = await get_redis().getdel(_ticket_key(ticket_id))
    if stored is None:
        return None
    try:
        target = PanelTarget(**json.loads(stored))
    except (TypeError, ValueError):
        return None

    session_id = secrets.token_urlsafe(32)
    await get_redis().set(_session_key(session_id), json.dumps(asdict(target)), ex=SESSION_TTL_SEC)
    log.info("panel.opened", device_id=target.device_id, mac=target.mac)
    return _serializer().dumps(session_id), target


async def load(raw_cookie: str | None) -> PanelTarget | None:
    """Цель по куке панели. Пусто — сессии нет или она истекла."""
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
        return PanelTarget(**json.loads(stored))
    except (TypeError, ValueError):
        return None


async def close(raw_cookie: str | None) -> None:
    if not raw_cookie:
        return
    try:
        session_id = _serializer().loads(raw_cookie)
    except BadSignature:
        return
    await get_redis().delete(_session_key(session_id))
