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
import datetime as dt
import math
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

import structlog
from sqlalchemy import select

from core.config import settings
from core.dates import ensure_utc, utcnow
from core.db import session_scope
from core.errors import describe
from core.models import Device
from core.notifications import notify_admins
from core.services import settings_service
from core.services.frp import RouterApi, dashboard

log = structlog.get_logger("worker.fleet_watch")

STATE_KEY = "fleet_watch.state"

MIN_FLEET = 3
"""Ниже этого числа «массовое падение» не отличить от двух выключенных
на ночь роутеров. Пока парк меньше, сторож молчит о падениях — но не о frps."""

DROP_SHARE = 0.5
"""Пропала половина и больше — это не клиенты выключили роутеры, а поломка."""

RECOVER_SHARE = 0.8
"""Вернулось столько — считаем, что починилось. Не сто процентов: кто-то
за время простоя выключил роутер по своим причинам, и ждать его нечего."""

CONFIRM_RUNS = 2
"""Сколько кругов подряд поломка должна держаться, чтобы стать тревогой."""

ACCEPT_AFTER = dt.timedelta(hours=24)
"""Сутки «упавшего» состояния без возврата — это уже новая норма: роутеры
сняли, парк уменьшился. Сторож говорит об этом и перестаёт ждать."""

PROBE_LIMIT = 2
"""Сколько роутеров пробуем через туннель. Хватит одного ответившего,
чтобы сказать «визитёр жив»; два — чтобы один молчащий не поднял тревогу."""

PROBE_TIMEOUT_SEC = 20

FRPS = "frps"
FLEET = "fleet"
TUNNELS = "tunnels"


@dataclass(slots=True)
class Observation:
    reachable: bool
    error: str = ""
    online: int = 0
    tunnels_ok: bool | None = None
    """None — проверять было нечего: на frps никого нет или нет портов."""


@dataclass(slots=True)
class State:
    status: str = "ok"
    """ok — всё хорошо; suspect — поломка замечена, ждём подтверждения;
    down — тревога отправлена."""
    reason: str = ""
    baseline: int = 0
    """Сколько было на связи до поломки. Во время поломки не меняется:
    иначе через круг «стало ноль» превратилось бы в «было ноль»."""
    strikes: int = 0
    since: str = ""

    @classmethod
    def load(cls, raw: object) -> State:
        if not isinstance(raw, dict):
            return cls()
        known = {key: raw[key] for key in cls.__slots__ if key in raw}
        try:
            return cls(**known)
        except TypeError:
            return cls()

    def dump(self) -> dict:
        return asdict(self)


def _problem(obs: Observation, baseline: int) -> str:
    """Какая из трёх поломок сейчас видна. Пусто — никакой."""
    if not obs.reachable:
        return FRPS
    dropped = baseline - obs.online
    if (
        baseline >= MIN_FLEET
        and obs.online <= baseline * DROP_SHARE
        and dropped >= MIN_FLEET
    ):
        return FLEET
    if obs.tunnels_ok is False:
        return TUNNELS
    return ""


def _recovered(state: State, obs: Observation) -> bool:
    if not obs.reachable:
        return False
    if state.reason == TUNNELS:
        return obs.tunnels_ok is not False
    need = math.ceil(state.baseline * RECOVER_SHARE)
    return obs.online >= need and obs.tunnels_ok is not False


def _minutes(since: str, now: dt.datetime) -> int:
    try:
        start = ensure_utc(dt.datetime.fromisoformat(since))
    except (TypeError, ValueError):
        return 0
    return max(0, int((now - start).total_seconds() // 60))


def _duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    return f"{hours} ч {rest:02d} мин"


def _frps_where() -> str:
    """Куда смотреть: адрес frps из настроек, а не из памяти оператора —
    за месяц frps переезжал дважды."""
    host = urlparse(settings.frp.dashboard_url).hostname or settings.frp.server_host
    return f"{host}:{settings.frp.server_port}"


def alert_text(reason: str, state: State, obs: Observation) -> str:
    if reason == FRPS:
        return (
            "🚨 frps не отвечает приложению.\n"
            f"Ошибка: {obs.error or 'нет ответа'}\n\n"
            "Роутеры при этом могут работать — мы их просто не видим: стоят "
            "показания, панель роутеров и автоактивация.\n"
            f"Проверьте, жив ли frps и есть ли путь от witty-dragon до {_frps_where()}."
        )
    if reason == FLEET:
        return (
            "🚨 Роутеры массово пропали со связи: "
            f"было {state.baseline}, сейчас {obs.online}.\n\n"
            "frps нам отвечает — значит, рвётся путь от роутеров до него: "
            f"перекрыли направление, имя {settings.frp.server_host} ведёт не туда "
            "или на frps сменился токен.\n"
            "Проверьте с российской машины: открыт ли порт frps снаружи."
        )
    return (
        f"⚠️ Роутеры на связи с frps ({obs.online}), но туннели до них не открываются.\n\n"
        "Беда у нас, в визитёре frpc. Посмотрите на witty-dragon:\n"
        "cd /opt/router-shop && docker compose logs --tail 30 frpc"
    )


def decide(state: State, obs: Observation, now: dt.datetime) -> tuple[State, str]:
    """Новое состояние и текст сообщения (пустой — писать нечего).

    Чистая функция: вся логика сторожа здесь, и её можно проверить
    на каждом переходе, не поднимая ни frps, ни базы.
    """
    if state.status == "down":
        if _recovered(state, obs):
            minutes = _minutes(state.since, now)
            text = (
                f"✅ Парк снова на связи: {obs.online}"
                + (f" из {state.baseline}" if state.baseline else "")
                + f". Простой {_duration(minutes)}."
            )
            return State(baseline=obs.online), text
        if _minutes(state.since, now) >= ACCEPT_AFTER.total_seconds() // 60 and obs.reachable:
            text = (
                f"ℹ️ Сутки на связи {obs.online} из {state.baseline} — считаю это "
                "новой нормой и тревогу снимаю. Если роутеры не снимали, разберитесь."
            )
            return State(baseline=obs.online), text
        return state, ""

    problem = _problem(obs, state.baseline)
    if not problem:
        # Всё хорошо: точка отсчёта — нынешнее число. Растёт вместе с парком
        # и падает, когда клиенты по одному выключают роутеры на ночь.
        return State(baseline=obs.online if obs.reachable else state.baseline), ""

    strikes = state.strikes + 1 if state.status == "suspect" and state.reason == problem else 1
    since = state.since if state.status == "suspect" and state.reason == problem else now.isoformat()
    if strikes < CONFIRM_RUNS:
        return State("suspect", problem, state.baseline, strikes, since), ""
    down = State("down", problem, state.baseline, strikes, since)
    return down, alert_text(problem, down, obs)


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
        if new_state.dump() != state.dump():
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
