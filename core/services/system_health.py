"""Состояние системы одним экраном: что живо, что нет и куда смотреть.

Когда «что-то сломалось», до сих пор приходилось ходить по SSH на три машины
и гадать, какое звено: 4 октября час ушёл на то, чтобы понять, что рвётся
путь до frps, а не визитёр и не файрвол. Здесь каждое звено проверено
и названо, у сломанного — подсказка, что делать.

Проверки идут разом и каждая со своим пределом по времени: зависшая панель
не должна держать страницу, которую открывают как раз тогда, когда всё висит.
Ничего не меняет и не бросает — страница обязана открыться.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.dates import ensure_utc, utcnow
from core.db import check_database, session_scope
from core.enums import PaymentStatus
from core.errors import describe
from core.models import Notification, PartnerCallback, Payment
from core.notifications import OUTBOX_MAX_ATTEMPTS, PARTNER_MAX_ATTEMPTS
from core.redis_client import check_redis
from core.services import fleet_watch, object_storage, remnawave, settings_service
from core.services.frp import dashboard

log = structlog.get_logger("services.system_health")

OK, WARN, FAIL, OFF = "ok", "warn", "fail", "off"

CHECK_TIMEOUT_SEC = 6

WORKER_HEALTH_URL = "http://worker:8082/readyz"
"""Воркер в той же сети compose, имя службы — `worker`."""

WATCH_STALE_MIN = 6
"""Сторож парка ходит раз в две минуты. Не смотрел дольше шести — значит,
планировщик воркера встал, даже если сам процесс отвечает."""

QUEUE_WARN_MIN = 5
QUEUE_FAIL_MIN = 20
"""Пороги очередей — те же, что у сторожа сообщений: пять минут — задержка,
двадцать — остановка."""

PAYMENT_STUCK_MIN = 30


@dataclass(slots=True)
class Check:
    key: str
    title: str
    status: str
    detail: str = ""
    hint: str = ""


def _ago(moment: dt.datetime | None, now: dt.datetime) -> str:
    if moment is None:
        return "никогда"
    minutes = max(0, int((now - ensure_utc(moment)).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} мин назад"
    if minutes < 1440:
        return f"{minutes // 60} ч назад"
    return f"{minutes // 1440} дн назад"


async def _database() -> Check:
    if await check_database(timeout=3):
        return Check("database", "База данных", OK, "отвечает")
    return Check("database", "База данных", FAIL, "не отвечает",
                 "docker compose ps postgres — и журнал: docker compose logs --tail 50 postgres")


async def _redis() -> Check:
    if await check_redis(timeout=2):
        return Check("redis", "Redis", OK, "отвечает")
    return Check("redis", "Redis", FAIL, "не отвечает",
                 "Без него не работают кэш настроек и ограничения частоты.")


async def _worker(session: AsyncSession, now: dt.datetime) -> Check:
    title = "Воркер и планировщик"
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            answer = await client.get(WORKER_HEALTH_URL)
        alive = answer.status_code == 200
    except httpx.HTTPError as exc:
        return Check("worker", title, FAIL, f"не отвечает: {describe(exc)}",
                     "Без воркера стоят опрос парка, платежи, напоминания и сводка. "
                     "cd /opt/router-shop && docker compose logs --tail 50 worker")
    state = fleet_watch.State.load(await settings_service.get_setting(session, fleet_watch.STATE_KEY))
    try:
        checked = ensure_utc(dt.datetime.fromisoformat(state.checked_at)) if state.checked_at else None
    except ValueError:
        checked = None
    if not alive:
        return Check("worker", title, FAIL, f"отвечает {answer.status_code}",
                     "Воркер жив, но не видит базу или Redis.")
    if checked is None or now - checked > dt.timedelta(minutes=WATCH_STALE_MIN):
        return Check("worker", title, WARN, f"процесс жив, но сторож парка смотрел {_ago(checked, now)}",
                     "Похоже, встал планировщик. Перезапуск: docker compose restart worker")
    return Check("worker", title, OK, f"задачи идут, последняя проверка парка {_ago(checked, now)}")


async def _frps() -> Check:
    title = "frps — сервер туннелей"
    if not settings.frp.is_configured:
        return Check("frps", title, OFF, "не настроен")
    try:
        online = await dashboard().online_routers()
    except Exception as exc:  # noqa: BLE001 — недоступный frps и есть то, что показываем
        return Check("frps", title, FAIL, f"не отвечает: {describe(exc)}",
                     f"Проверьте, жив ли frps и есть ли путь от witty-dragon до "
                     f"{settings.frp.server_host}:{settings.frp.server_port}.")
    return Check("frps", title, OK, f"отвечает, на нём роутеров: {len(online)}")


async def _fleet(session: AsyncSession) -> Check:
    """Последнее решение сторожа парка: он же видит и визитёр, и массовое падение."""
    title = "Парк и туннели"
    state = fleet_watch.State.load(await settings_service.get_setting(session, fleet_watch.STATE_KEY))
    if state.status == "down":
        texts = {
            fleet_watch.FRPS: "frps не отвечает",
            fleet_watch.FLEET: f"роутеры массово пропали: было {state.baseline}",
            fleet_watch.TUNNELS: "туннели до роутеров не открываются — визитёр frpc",
        }
        return Check("fleet", title, FAIL, texts.get(state.reason, state.reason),
                     "Подробности — в сообщении сторожа в рабочем чате.")
    if state.status == "suspect":
        return Check("fleet", title, WARN, "сторож заметил сбой и проверяет ещё раз")
    if not state.checked_at:
        return Check("fleet", title, WARN, "сторож ещё не смотрел — воркер обновлён недавно?")
    return Check("fleet", title, OK, f"на связи {state.baseline}, туннели открываются")


async def _panel() -> Check:
    title = "Панель Remnawave"
    status = await remnawave.client().probe()
    if not status.configured:
        return Check("panel", title, OFF, "не настроена")
    if not status.ok:
        return Check("panel", title, FAIL, status.error or "не отвечает",
                     "Без панели не включаются и не продлеваются подписки.")
    detail = f"узлов на связи {status.nodes_online} из {status.nodes_total}"
    if status.nodes_total and status.nodes_online < status.nodes_total:
        return Check("panel", title, WARN, detail, "Часть узлов не на связи — раздел Remnawave.")
    return Check("panel", title, OK, detail)


async def _payments(session: AsyncSession, now: dt.datetime) -> Check:
    title = "Платежи PLATEGA"
    if not (settings.platega.merchant_id and settings.platega.secret.get_secret_value()):
        return Check("payments", title, OFF, "не настроены")
    last = await session.scalar(
        select(func.max(Payment.paid_at)).where(Payment.status == PaymentStatus.SUCCEEDED)
    )
    stuck = await session.scalar(
        select(func.count())
        .select_from(Payment)
        .where(
            Payment.status == PaymentStatus.PENDING,
            Payment.created_at < now - dt.timedelta(minutes=PAYMENT_STUCK_MIN),
            Payment.created_at > now - dt.timedelta(days=1),
        )
    ) or 0
    detail = f"последняя оплата {_ago(last, now)}"
    if stuck:
        return Check("payments", title, WARN, f"{detail}; висят без ответа: {stuck}",
                     "Обычно это брошенные ссылки — гаснут сами. Много разом — проверьте кабинет PLATEGA.")
    return Check("payments", title, OK, detail)


async def _storage(session: AsyncSession) -> Check:
    title = "Хранилище S3"
    storage = await object_storage.current(session)
    if not storage.configured:
        return Check("storage", title, OFF, "не настроено — прошивки и списки раздаются с сервера")
    return Check("storage", title, OK, f"бакет {storage.bucket}")


async def _queue(session: AsyncSession, now: dt.datetime, *, partner: bool) -> Check:
    if partner:
        key, title = "partner_callbacks", "Оплаты подписки → бот"
        waiting = select(PartnerCallback.created_at).where(
            PartnerCallback.delivered_at.is_(None), PartnerCallback.attempts < PARTNER_MAX_ATTEMPTS
        )
        hint = "Люди заплатили за подписку и ждут включения. Бот не ходит за очередью колбэков."
    else:
        key, title = "outbox", "Сообщения клиентам"
        waiting = select(Notification.created_at).where(
            Notification.sent_at.is_(None), Notification.attempts < OUTBOX_MAX_ATTEMPTS
        )
        hint = "Бот не забирает очередь: systemctl status router-bot на witty-dragon."
    rows = waiting.subquery()
    pending = await session.scalar(select(func.count()).select_from(rows)) or 0
    oldest = await session.scalar(select(func.min(rows.c.created_at)))
    age = int((now - ensure_utc(oldest)).total_seconds() // 60) if oldest else 0
    if not pending:
        return Check(key, title, OK, "очередь пуста")
    detail = f"в очереди {pending}, старшее ждёт {age} мин"
    if age >= QUEUE_FAIL_MIN:
        return Check(key, title, FAIL, detail, hint)
    if age >= QUEUE_WARN_MIN:
        return Check(key, title, WARN, detail, hint)
    return Check(key, title, OK, detail)


async def _guard(key: str, title: str, job: Callable[[], Awaitable[Check]]) -> Check:
    """Предел по времени и страховка: упавшая проверка — красная строка,
    а не упавшая страница."""
    try:
        return await asyncio.wait_for(job(), timeout=CHECK_TIMEOUT_SEC)
    except TimeoutError:
        return Check(key, title, FAIL, f"не ответило за {CHECK_TIMEOUT_SEC} с")
    except Exception as exc:  # noqa: BLE001 — страница состояния открывается всегда
        log.warning("system_health.check_failed", key=key, error=describe(exc))
        return Check(key, title, FAIL, describe(exc))


def _with_session(job: Callable[[AsyncSession], Awaitable[Check]]) -> Callable[[], Awaitable[Check]]:
    """Своя сессия на проверку: они идут разом, а одна сессия не терпит
    параллельных запросов."""

    async def run() -> Check:
        async with session_scope() as session:
            return await job(session)

    return run


async def collect(now: dt.datetime | None = None) -> list[dict]:
    now = now or utcnow()
    jobs = [
        ("database", "База данных", _database),
        ("redis", "Redis", _redis),
        ("worker", "Воркер и планировщик", _with_session(lambda s: _worker(s, now))),
        ("frps", "frps — сервер туннелей", _frps),
        ("fleet", "Парк и туннели", _with_session(_fleet)),
        ("panel", "Панель Remnawave", _panel),
        ("payments", "Платежи PLATEGA", _with_session(lambda s: _payments(s, now))),
        ("outbox", "Сообщения клиентам", _with_session(lambda s: _queue(s, now, partner=False))),
        ("partner_callbacks", "Оплаты подписки → бот",
         _with_session(lambda s: _queue(s, now, partner=True))),
        ("storage", "Хранилище S3", _with_session(_storage)),
    ]
    checks = await asyncio.gather(*(_guard(key, title, job) for key, title, job in jobs))
    return [asdict(check) for check in checks]


def overall(checks: list[dict]) -> str:
    """Одно слово на всю систему — для заголовка страницы."""
    states = {check["status"] for check in checks}
    if FAIL in states:
        return FAIL
    if WARN in states:
        return WARN
    return OK
