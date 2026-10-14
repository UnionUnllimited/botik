"""«Не открывается сайт»: проверка и просьба добавить.

Самое частое обращение в поддержку, и почти всегда ответ на него один из
пяти. Роутер не на связи; подписка закончилась; клиент сам выключил доступ
переключателем; сайт уже идёт через защищённый канал и дело не в списках;
сайта в списках нет. Первые четыре клиент решает сам за минуту, если ему
сказать, какой из них, — а последний решает только оператор.

Проверка отвечает кодом, а не текстом: текст свой у бота и у приложения,
и у бота его правят в админке. Просьба становится записью (`SiteRequest`)
и сообщением в топике «Роутеры» с кнопкой «Добавить в свой список» — одно
нажатие оператора, и клиенту уходит «добавили».
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import ipaddress
import re
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core import texts
from core.dates import ensure_utc, utcnow
from core.models import Device, ListKind, SiteRequest, User
from core.notifications import send_message
from core.services import list_lookup, router_nodes, router_shell, router_topics
from core.services import subscriptions as subscription_service

log = structlog.get_logger("services.site_help")

OFFLINE = "router_offline"
NO_SUBSCRIPTION = "no_subscription"
SERVICE_OFF = "service_off"
COVERED = "covered"
LOCAL = "local"
DIRECT = "direct"
UNKNOWN = "unknown"

NEW = "new"
ADDED = "added"
DECLINED = "declined"
DONE = "done"
STATUS_TITLES = {NEW: "ждёт", ADDED: "добавлен", DECLINED: "не добавляем", DONE: "разобрались"}

CALLBACK_PREFIX = "site"

SERVICE_READ_TIMEOUT_SEC = 12
"""Сколько ждать ответа роутера о переключателе. Это лишь одна из причин;
не дождались — проверяем дальше, не задерживая ответ клиенту."""

LOCAL_ZONES = (
    "ru",
    "su",
    "xn--p1ai",
    "xn--p1acf",
    "xn--80adxhks",
    "xn--d1acj3b",
    "moscow",
    "tatar",
)
"""Российские зоны: .ru, .su, .рф, .рус, .москва, .дети и городские.
Такие сайты идут напрямую всегда, и если не открываются — дело не в нас,
а добавлять их в списки бессмысленно и вредно: пойдут через зарубежный
узел и начнут спрашивать «вы робот?»."""

_DOMAIN = re.compile(r"^(?=.{3,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")

NOTE_LIMIT = 500


class BadSiteError(ValueError):
    """Прислали не адрес сайта."""


def clean(query: str) -> tuple[str, str]:
    """Вид и чистое значение. Кириллический домен — в punycode: так он
    записан в списках и так его ищет роутер."""
    kind, value = list_lookup.normalize(query)
    if kind == ListKind.PROXY_IP:
        if "/" in value:
            raise BadSiteError("Пришлите адрес сайта, а не сеть.")
        # Адрес домашней сети, петли или служебный в общий список попасть
        # не должен: одно нажатие «Добавить» увело бы через туннель локальную
        # сеть на всех роутерах парка.
        if not ipaddress.ip_address(value).is_global:
            raise BadSiteError("Это адрес внутренней сети, а не сайта.")
        return kind, value
    value = value.removeprefix("www.") if value.count(".") > 1 else value
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise BadSiteError("Не похоже на адрес сайта.") from exc
    if not _DOMAIN.match(value):
        raise BadSiteError("Не похоже на адрес сайта.")
    return kind, value


def shown(value: str) -> str:
    """Домен так, как его узнает человек: `кинопоиск.рф`, а не `xn--…`."""
    try:
        return value.encode("ascii").decode("idna")
    except UnicodeError:
        return value


def is_local(value: str) -> bool:
    return value.rsplit(".", 1)[-1] in LOCAL_ZONES


async def has_access(session: AsyncSession, device: Device, now: dt.datetime) -> bool:
    """Оплачен ли доступ сейчас. Ничего не знаем — считаем, что да: сказать
    клиенту «подписка закончилась», когда она есть, хуже, чем промолчать."""
    live = await subscription_service.get_for_device(session, device.id)
    if live is not None:
        return live.expires_at is None or ensure_utc(live.expires_at) > now
    panel = ensure_utc(device.panel_expires_at) if device.panel_expires_at else None
    if panel is not None and panel > now:
        return True
    latest = await subscription_service.device_subscription(session, device.id)
    return latest is None and panel is None


async def _service_enabled(device: Device) -> bool | None:
    try:
        state = await asyncio.wait_for(router_nodes.read(device), timeout=SERVICE_READ_TIMEOUT_SEC)
    except (router_nodes.NodeError, router_shell.ShellError, TimeoutError):
        return None
    return state.enabled


async def pending(session: AsyncSession, user_id: int, value: str) -> SiteRequest | None:
    return await session.scalar(
        select(SiteRequest).where(
            SiteRequest.user_id == user_id, SiteRequest.value == value, SiteRequest.status == NEW
        )
    )


async def check(
    session: AsyncSession,
    device: Device,
    query: str,
    *,
    now: dt.datetime | None = None,
    read_service: bool = True,
) -> dict[str, Any]:
    """Почему сайт может не открываться — самым вероятным ответом.

    Порядок — от того, что ломает всё, к частному: при выключенном роутере
    не важно, есть ли сайт в списках."""
    now = now or utcnow()
    kind, value = clean(query)
    answer: dict[str, Any] = {"ok": True, "kind": kind, "value": value, "site": shown(value)}

    if not router_topics.is_online(device, now):
        return {**answer, "verdict": OFFLINE}
    if not await has_access(session, device, now):
        return {**answer, "verdict": NO_SUBSCRIPTION}
    if read_service and await _service_enabled(device) is False:
        return {**answer, "verdict": SERVICE_OFF}

    found = await list_lookup.lookup(session, value)
    waiting = await pending(session, device.user_id, value) if device.user_id else None
    answer["requested"] = waiting is not None
    if found.get("covered"):
        entries = [item["entry"] for item in found.get("covered_by") or []]
        return {**answer, "verdict": COVERED, "covered_by": entries[:3]}
    if kind == ListKind.PROXY_DOMAIN and is_local(value):
        return {**answer, "verdict": LOCAL}
    if not found.get("list_size"):
        return {**answer, "verdict": UNKNOWN}
    return {**answer, "verdict": DIRECT}


def _client_line(user: User | None) -> str:
    if user is None:
        return "Клиент: неизвестен"
    name = user.display_name if user.display_name != user.telegram_name else ""
    return f"Клиент: {html.escape(user.telegram_name)}{' · ' + html.escape(name) if name else ''}"


def operator_text(request: SiteRequest, user: User | None, device: Device | None) -> str:
    site = html.escape(shown(request.value))
    if request.covered:
        head = (
            f"🌐 <b>Не открывается сайт</b>\n<code>{site}</code> — уже в списках, "
            "но у клиента не открывается. Дело не в маршруте: стоит написать клиенту."
        )
    else:
        head = (
            f"🌐 <b>Просят добавить сайт</b>\n<code>{site}</code> — сейчас идёт напрямую, в списках его нет."
        )
    lines = [head, "", _client_line(user)]
    if device is not None:
        lines.append(f"Роутер: {device.mac}")
    if request.note:
        lines.append(f"Комментарий: «{html.escape(request.note)}»")
    return "\n".join(lines)


def operator_buttons(request: SiteRequest) -> list[dict]:
    buttons: list[dict] = []
    if request.status == NEW:
        if request.covered:
            buttons.append({"text": "✓ Разобрались", "data": f"{CALLBACK_PREFIX}:{request.id}:done"})
        else:
            buttons.append(
                {"text": "✓ Добавить в свой список", "data": f"{CALLBACK_PREFIX}:{request.id}:add"}
            )
            buttons.append({"text": "✕ Не добавлять", "data": f"{CALLBACK_PREFIX}:{request.id}:no"})
    if request.device_id:
        buttons.append(
            {"text": "✉ Написать клиенту", "data": router_topics.callback(request.device_id, "dm")}
        )
    return buttons


async def create(
    session: AsyncSession, user: User, device: Device, query: str, *, note: str = ""
) -> tuple[SiteRequest, bool]:
    """Просьба клиента. Второй раз ту же не заводим: возвращаем ждущую —
    клиент нажал «передать» дважды, оператор не должен видеть две."""
    kind, value = clean(query)
    waiting = await pending(session, user.id, value)
    if waiting is not None:
        return waiting, False

    found = await list_lookup.lookup(session, value)
    request = SiteRequest(
        user_id=user.id,
        device_id=device.id,
        kind=kind,
        value=value,
        covered=bool(found.get("covered")),
        # В одну строку: перевод строки в заметке нарисовал бы оператору
        # поддельную строку «Роутер: …» под настоящими.
        note=" ".join((note or "").split())[:NOTE_LIMIT],
        status=NEW,
    )
    # Двойное нажатие «передать» приходит двумя запросами разом, и проверка
    # выше пропускает оба. Второй упрётся в уникальный индекс ждущих просьб
    # (`uq_site_requests_pending`) — тогда отдаём ту, что успела первой.
    try:
        async with session.begin_nested():
            session.add(request)
            await session.flush()
    except IntegrityError:
        waiting = await pending(session, user.id, value)
        if waiting is not None:
            return waiting, False
        raise
    await router_topics.post(
        session, operator_text(request, user, device), operator_buttons(request), client_tg_id=user.tg_id or 0
    )
    log.info("site_help.requested", request_id=request.id, value=value, covered=request.covered)
    return request, True


async def resolve(session: AsyncSession, request: SiteRequest, action: str, *, author: str = "") -> str:
    """Решение оператора. Возвращает текст отказа или пусто.

    Повторное нажатие — не ошибка, а «уже»: кнопку в топике могли нажать
    двое, и второму незачем видеть красное.

    Двое могут нажать и одновременно. Поэтому просьба сперва «забирается»
    условным UPDATE: второй запрос дождётся первого, не найдёт строки
    в статусе «ждёт» и выйдет молча — иначе клиенту ушло бы два ответа."""
    statuses = {"add": ADDED, "no": DECLINED, "done": DONE}
    if action not in statuses:
        return "Неизвестное действие."
    if request.status != NEW:
        return ""

    claimed = await session.execute(
        update(SiteRequest)
        .where(SiteRequest.id == request.id, SiteRequest.status == NEW)
        .values(status=statuses[action], resolved_at=utcnow(), resolved_by=(author or "")[:120])
        .execution_options(synchronize_session=False)
    )
    await session.refresh(request)
    if claimed.rowcount == 0:
        return ""

    site = html.escape(shown(request.value))
    client_text = ""
    if action == "add":
        error = await list_lookup.add_to_own(
            session, request.kind, request.value, author=author or "просьба клиента"
        )
        if error:
            # Не легло в список — просьба снова ждёт: решение не принято.
            request.status, request.resolved_at, request.resolved_by = NEW, None, ""
            return error
        client_text = texts.SITE_ADDED.format(site=site)
    elif action == "no":
        client_text = texts.SITE_DECLINED.format(site=site)

    user = await session.get(User, request.user_id)
    if client_text and user is not None and user.tg_id:
        await send_message(user.tg_id, client_text, session=session, kind="site_request")
    log.info("site_help.resolved", request_id=request.id, status=request.status, author=author)
    return ""


def resolution_note(request: SiteRequest) -> str:
    """Строка под сообщением в топике после решения."""
    title = {
        ADDED: "✅ Добавлен в свой список, клиенту написали",
        DECLINED: "✕ Не добавляем, клиенту написали",
        DONE: "✓ Разобрались",
    }.get(request.status, "")
    who = f" · {html.escape(request.resolved_by)}" if request.resolved_by else ""
    return f"{title}{who}" if title else ""


async def recent(session: AsyncSession, *, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
    """Для страницы в админке: сначала ждущие, потом разобранные."""
    query = select(SiteRequest).order_by(SiteRequest.id.desc()).limit(limit)
    if status:
        query = query.where(SiteRequest.status == status)
    rows = list(await session.scalars(query))
    users = (
        {
            user.id: user
            for user in await session.scalars(select(User).where(User.id.in_({row.user_id for row in rows})))
        }
        if rows
        else {}
    )
    devices = (
        {
            device.id: device
            for device in await session.scalars(
                select(Device).where(Device.id.in_({row.device_id for row in rows if row.device_id}))
            )
        }
        if rows
        else {}
    )
    items = []
    for row in rows:
        user = users.get(row.user_id)
        device = devices.get(row.device_id) if row.device_id else None
        items.append(
            {
                "id": row.id,
                "value": row.value,
                "site": shown(row.value),
                "kind": row.kind,
                "covered": row.covered,
                "note": row.note,
                "status": row.status,
                "status_title": STATUS_TITLES.get(row.status, row.status),
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
                "resolved_by": row.resolved_by,
                "client": user.telegram_name if user else "",
                "client_tg_id": (user.tg_id or 0) if user else 0,
                "device_id": row.device_id or 0,
                "mac": device.mac if device else "",
            }
        )
    items.sort(key=lambda item: item["status"] != NEW)
    return items
