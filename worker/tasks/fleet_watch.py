"""Сторож парка: весь парк пропал разом — сказать сразу, а не утром.

4 октября 2026 со связи ушли все роутеры одновременно. Узнали мы об этом
случайно, а потом час искали, где рвётся: в визитёре, в файрволе, в frps или
на пути до него. Утренняя сводка тут не помощник — она про отдельные роутеры
и приходит раз в сутки.

Здесь — одно событие с высокой ценой, и сообщение о нём сразу говорит, где
искать. Поломок три, и выглядят они снаружи одинаково — «роутеры молчат»:

* **frps не отвечает нам.** Рвётся путь от witty-dragon до frps, или frps
  лежит. Роутеры при этом могут работать — мы их просто не видим.
* **frps отвечает, а роутеров на нём нет.** Рвётся путь от роутеров до frps:
  перекрыли направление, имя ведёт не туда, на frps другой токен.
* **Роутеры на frps есть, а туннели до них не открываются.** Беда у нас,
  в визитёре frpc.

Тревога — когда поломка держится два круга подряд: разовый сбой опроса
не повод будить оператора. Сообщение одно на происшествие и одно — когда
всё вернулось. Состояние хранится в настройках, а не в памяти: перезапуск
воркера посреди простоя иначе прислал бы ту же тревогу второй раз.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select

from core.config import settings
from core.dates import utcnow
from core.db import session_scope
from core.errors import describe
from core.models import Device
from core.notifications import notify_admins
from core.services import settings_service
from core.services.fleet_watch import STATE_KEY, Observation, State, decide
from core.services.frp import RouterApi, dashboard

log = structlog.get_logger("worker.fleet_watch")

PROBE_LIMIT = 2
"""Сколько роутеров пробуем через туннель. Хватит одного ответившего,
чтобы сказать «визитёр жив»; два — чтобы один молчащий не поднял тревогу."""

PROBE_TIMEOUT_SEC = 20

async def _probe_tunnels(online_macs: set[str]) -> bool | None:
    """Ответит ли хоть один роутер через свой туннель."""
    if not online_macs:
        return None
    async with session_scope() as session:
        ports = list(
            await session.scalars(
                select(Device.frp_visitor_port)
                .where(Device.mac.in_(online_macs), Device.frp_visitor_port.is_not(None))
                .order_by(Device.last_poll_at.desc().nulls_last())
                .limit(PROBE_LIMIT)
            )
        )
    if not ports:
        return None
    for port in ports:
        try:
            await asyncio.wait_for(RouterApi(port).stats(), timeout=PROBE_TIMEOUT_SEC)
            return True
        except Exception as exc:  # noqa: BLE001 — молчание одного роутера — это ещё не ответ
            log.debug("fleet_watch.probe_failed", port=port, error=describe(exc))
    return False


async def observe() -> Observation:
    try:
        online = await dashboard().online_routers()
    except Exception as exc:  # noqa: BLE001 — недоступный frps и есть то, что мы ловим
        return Observation(reachable=False, error=describe(exc))
    return Observation(
        reachable=True,
        online=len(online),
        tunnels_ok=await _probe_tunnels(set(online)),
    )


async def watch_fleet() -> str:
    """Один круг сторожа. Возвращает состояние — для журнала и тестов."""
    if not settings.frp.is_configured:
        return "off"

    obs = await observe()
    now = utcnow()
    async with session_scope() as session:
        state = State.load(await settings_service.get_setting(session, STATE_KEY))
        new_state, text = decide(state, obs, now)
        new_state.checked_at = now.isoformat()
        await settings_service.set_setting(session, STATE_KEY, new_state.dump())
        if text:
            log.error("fleet_watch.alert", reason=new_state.reason or state.reason, online=obs.online)
            await notify_admins(text, session=session, kind="fleet_watch")
    log.info(
        "fleet_watch.checked",
        status=new_state.status,
        reachable=obs.reachable,
        online=obs.online,
        tunnels_ok=obs.tunnels_ok,
    )
    return new_state.status
