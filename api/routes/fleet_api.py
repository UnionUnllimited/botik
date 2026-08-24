"""Парк роутеров наружу — для вкладки «Роутеры» в админке бота.

Бот и его админка живут отдельным процессом на хосте, со своим venv и без
драйвера Postgres, а наша база наружу не опубликована. Поэтому данные отдаются
по HTTP, а не запросом в базу: это единственный способ, которым тот процесс
вообще может нас спросить.

Действия — опрос, активация, продление, привязка клиента — тоже здесь, а не
у них: туннель к роутеру держит наш контейнер `frpc`, и дотянуться до него
может только процесс в нашей сети. Их админка эти ручки вызывает.

Консоль тоже здесь: у нас она разовыми командами, а не сессией, поэтому
переносится обычной ручкой. Не переехало только проксирование панели LuCI —
там переписывание ссылок и куки, это отдельный слой.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.deps import get_session, get_transaction
from api.service_auth import require_token
from core.config import settings
from core.dates import utcnow
from core.enums import DeviceStatus, SubscriptionStatus
from core.models import (
    Device,
    DeviceEvent,
    DomainBuild,
    DomainSource,
    ListKind,
    ManualList,
    ManualListRevision,
    Subscription,
    User,
)
from core.security import normalize_mac
from core.services import (
    activation,
    domain_lists,
    panel_ticket,
    remnawave,
    router_shell,
    settings_service,
)
from core.services import routers as routers_service
from core.services import subscriptions as subscription_service
from core.services.frp import RouterApi

log = structlog.get_logger("api.fleet")

router = APIRouter(prefix="/api/v1/fleet", tags=["fleet"], include_in_schema=False)


SUBSCRIPTION_LABELS = {
    "pending": "оплачена, ждёт роутера",
    "active": "активна",
    "grace": "льготный период",
    "expired": "истекла",
    "cancelled": "отменена",
}
"""Человеческие названия состояний. Переводим здесь, а не в каждом шаблоне:
иначе `active` рано или поздно вылезет оператору — так и вышло."""

DEVICE_LABELS = {
    "new": "на складе",
    "assigned": "отгружен клиенту",
    "active": "работает",
    "revoked": "изъят",
    "blocked": "заблокирован",
}


def _label(value: str, labels: dict[str, str]) -> str:
    return labels.get(value, value)


def _elsewhere(subscription, device: Device) -> bool:
    """Подписка клиента лежит на другом его роутере.

    Не то же, что «не на этом»: у оплаченной, но не активированной подписки
    роутера нет вовсе — она ждёт первого выхода на связь. Между отгрузкой
    и активацией страница писала «оплачена, ждёт роутера · на другом роутере»,
    и оператор шёл искать несуществующий второй роутер.
    """
    return bool(
        subscription
        and subscription.device_id is not None
        and subscription.device_id != device.id
    )


def _matches_link(device: Device, link: str, *, now: dt.datetime) -> bool:
    if not link:
        return True
    online = device.frp_online or device.is_online(
        threshold_min=settings.subscription.heartbeat_offline_min, now=now
    )
    return online is (link == "online")


def _matches_sub(device: Device, sub: str, subs: dict, *, now: dt.datetime) -> bool:
    """Подписка глазами оператора.

    «Нет» и «на другом роутере» — разные беды: в первом случае клиент не платил,
    во втором заплатил, но роутер этот срок не получил. Разводить их важно:
    второй случай — это молча не активировавшийся второй роутер.
    """
    if not sub:
        return True
    subscription = subs.get(device.id)
    if sub == "none":
        return subscription is None
    if subscription is None:
        return False
    here = subscription.device_id == device.id
    if sub == "elsewhere":
        # Именно на другом, а не «не на этом»: у оплаченной, но не
        # активированной подписки роутера нет вовсе (`device_id` пуст),
        # и звать её «на другом роутере» — врать оператору.
        return subscription.device_id is not None and not here
    if sub == "active":
        return here
    if sub == "expiring":
        expires = subscription.expires_at
        return bool(
            here and expires and now <= expires <= now + dt.timedelta(days=EXPIRING_SOON_DAYS)
        )
    return True


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


ROUTERS_PAGE_SIZE = 50
"""Столько же, сколько на складе. Пока роутеров пять, страница не нужна;
на первой партии список без неё станет нечитаемым, а ручка — тяжёлой:
на каждый роутер идёт отдельный запрос за подпиской."""

ROUTERS_PAGE_SIZES = (25, 50, 100, 200)
"""Из чего оператор выбирает размер страницы. Список закрытый: иначе
`per_page=100000` в адресе вернёт нас к тому, ради чего заводилась страница."""

EXPIRING_SOON_DAYS = 7
"""«Истекает» — сколько дней до конца подписки считать поводом позвонить."""

CARD_EVENTS = 10
"""Столько строк журнала показывает карточка. Остальное — на своей странице:
за месяц у роутера их набегают сотни, и карточка становилась лентой."""

EVENTS_PAGE_SIZE = 100
EVENTS_RETENTION_DAYS = 60
"""Дольше журнал не держим. «Показан пароль root» за прошлый квартал не нужен
никому, а таблица растёт с каждой командой и каждым обходом парка."""


@router.get("/routers", dependencies=[Depends(require_token)])
async def list_routers(
    session: AsyncSession = Depends(get_session),
    q: str = Query("", max_length=120),
    link: str = Query("", pattern="^(online|offline)?$"),
    client: str = Query("", pattern="^(with|without)?$"),
    sub: str = Query("", pattern="^(active|none|expiring|elsewhere)?$"),
    state: str = Query("", max_length=16),
    model: str = Query("", max_length=120),
    page: int = Query(1, ge=1),
    per_page: int = Query(ROUTERS_PAGE_SIZE),
) -> dict:
    """Список роутеров с показаниями и владельцем.

    Фильтры считаются в SQL, а не по загруженному списку: иначе страница
    на пятьдесят строк тянула бы весь парк и спрашивала подписку по каждому.

    Связь и подписка — исключения: связь складывается из флага туннеля
    и свежести последнего ответа, а подписка лежит отдельной таблицей и
    спрашивается по клиенту. И то и другое считается в Python, поэтому по ним
    фильтруем после выборки. Счётчики сверху считаем по всему парку — им
    фильтр не нужен, они про парк целиком.
    """
    now = utcnow()
    size = per_page if per_page in ROUTERS_PAGE_SIZES else ROUTERS_PAGE_SIZE

    conditions = []
    text = q.strip()
    if text:
        like = f"%{text}%"
        conditions.append(or_(Device.mac.ilike(like), Device.model.ilike(like)))
    if client == "with":
        conditions.append(Device.user_id.is_not(None))
    elif client == "without":
        conditions.append(Device.user_id.is_(None))
    if state:
        try:
            conditions.append(Device.status == DeviceStatus(state))
        except ValueError:
            # Состояние из чужого адреса: показываем парк целиком, а не пустоту,
            # иначе опечатка в ссылке читается как «роутеров нет».
            log.info("fleet.unknown_state_filter", state=state)
    if model.strip():
        conditions.append(Device.model == model.strip())

    statement = select(Device).options(selectinload(Device.user)).order_by(Device.id.desc())
    if conditions:
        statement = statement.where(*conditions)

    # Фильтры по связи и подписке не ложатся в SQL, поэтому при них берём
    # выборку целиком и режем на страницы уже после. На парке в тысячи это
    # стоило бы дорого, но такие фильтры и нужны, чтобы найти единицы:
    # молчащие роутеры и тех, у кого кончается срок.
    in_python = bool(link or sub)
    if not in_python:
        total = await session.scalar(
            select(func.count()).select_from(statement.subquery())
        ) or 0
        statement = statement.limit(size).offset((page - 1) * size)

    devices = list(await session.scalars(statement))

    # Подписку спрашиваем один раз на роутер и держим здесь: она нужна и
    # фильтру, и строке таблицы, а второй запрос за тем же — лишний круг.
    subs = {
        device.id: (
            await subscription_service.get_current(session, device.user_id)
            if device.user_id
            else None
        )
        for device in devices
    }

    if in_python:
        devices = [
            device
            for device in devices
            if _matches_link(device, link, now=now) and _matches_sub(device, sub, subs, now=now)
        ]
        total = len(devices)
        devices = devices[(page - 1) * size : page * size]

    items = []
    for device in devices:
        online = device.frp_online or device.is_online(
            threshold_min=settings.subscription.heartbeat_offline_min, now=now
        )
        seen = (device.last_heartbeat_at, device.last_poll_at, device.frp_last_seen_at)
        subscription = subs.get(device.id)
        items.append(
            {
                "id": device.id,
                "mac": device.mac,
                "model": device.model or device.board or "",
                "status": str(device.status),
                "online": online,
                "last_seen": _iso(max((value for value in seen if value), default=None)),
                "activated_at": _iso(device.activated_at),
                "wan_ip": device.last_wan_ip or "",
                "clients": (device.clients_wifi or 0) + (device.clients_dhcp or 0),
                "cpu_pct": device.cpu_pct,
                "ram_pct": device.ram_pct,
                "rx_bytes": device.rx_bytes or 0,
                "tx_bytes": device.tx_bytes or 0,
                "uptime_sec": device.uptime_sec or 0,
                "fw_version": device.fw_version or "",
                "visitor_port": device.frp_visitor_port,
                # В списке — телеграм: по нему клиенту пишут. Имя из доставки
                # тёзок не различает и в карточке видно рядом.
                "client": device.user.telegram_name if device.user else "",
                "client_name": device.user.display_name if device.user else "",
                "client_id": device.user_id,
                # Карточку клиента админка открывает по telegram_id: у неё свои
                # пользователи, и наш внутренний номер ей ничего не говорит.
                "client_tg_id": (device.user.tg_id or 0) if device.user else 0,
                "subscription_status": str(subscription.status) if subscription else "",
                "subscription_label": _label(str(subscription.status), SUBSCRIPTION_LABELS)
                if subscription
                else "",
                "status_label": _label(str(device.status), DEVICE_LABELS),
                "subscription_until": _iso(subscription.expires_at) if subscription else None,
                # Именно «на другом», а не «не на этом»: у оплаченной, но не
                # активированной подписки роутера нет вовсе, и строка
                # «оплачена, ждёт роутера · на другом роутере» — противоречие,
                # которое страница показывала всё время между отгрузкой
                # и первым выходом роутера на связь.
                "subscription_elsewhere": _elsewhere(subscription, device),
            }
        )

    # Счётчики сверху — про весь парк, а не про страницу: оператор смотрит
    # на них, чтобы понять состояние хозяйства, и «на связи 4 из 50 на этой
    # странице» ответа на такой вопрос не даёт.
    fleet = list(await session.scalars(select(Device)))
    online_total = sum(
        1
        for device in fleet
        if device.frp_online
        or device.is_online(threshold_min=settings.subscription.heartbeat_offline_min, now=now)
    )
    pages = max(1, (total + size - 1) // size)
    return {
        "generated_at": now.isoformat(),
        "fleet_total": len(fleet),
        "online": online_total,
        "silent": len(fleet) - online_total,
        "no_client": sum(1 for device in fleet if device.user_id is None),
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": size,
        "page_sizes": list(ROUTERS_PAGE_SIZES),
        # Модели для выпадающего списка — из парка, а не списком в коде:
        # они приходят от самих роутеров и новую партию никто не впишет руками.
        "models": sorted({device.model for device in fleet if device.model}),
        "states": [{"value": value, "title": title} for value, title in DEVICE_LABELS.items()],
        # Готовые скрипты — те же, что кнопками в карточке роутера. Здесь они
        # нужны для запуска сразу на отмеченных: набор один, и расходиться
        # этим двум спискам нельзя.
        "quick_commands": [
            {"name": name, "title": title}
            for name, (title, _command) in router_shell.QUICK_COMMANDS.items()
        ],
        "routers": items,
    }


def _device_payload(device, *, now):
    seen = (device.last_heartbeat_at, device.last_poll_at, device.frp_last_seen_at)
    return {
        "id": device.id,
        "mac": device.mac,
        "model": device.model or device.board or "",
        "fw_version": device.fw_version or "",
        "status": str(device.status),
        "status_label": _label(str(device.status), DEVICE_LABELS),
        "online": device.frp_online
        or device.is_online(threshold_min=settings.subscription.heartbeat_offline_min, now=now),
        "last_seen": _iso(max((value for value in seen if value), default=None)),
        "activated_at": _iso(device.activated_at),
        "wan_ip": device.last_wan_ip or "",
        "uptime_sec": device.uptime_sec or 0,
        "clients": (device.clients_wifi or 0) + (device.clients_dhcp or 0),
        "cpu_pct": device.cpu_pct,
        "ram_pct": device.ram_pct,
        "rx_bytes": device.rx_bytes or 0,
        "tx_bytes": device.tx_bytes or 0,
        "visitor_port": device.frp_visitor_port,
    }


@router.get("/routers/{device_id}", dependencies=[Depends(require_token)])
async def router_card(device_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Карточка роутера: показания, владелец, подписка, журнал и срок в панели."""
    device = await session.scalar(
        select(Device).where(Device.id == device_id).options(selectinload(Device.user))
    )
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")

    now = utcnow()
    subscription = (
        await subscription_service.get_current(session, device.user_id) if device.user_id else None
    )
    events = await routers_service.recent_events(session, device_id=device_id, limit=30)

    # Срок ручной активации знает только панель. Предел по времени тот же, что был
    # в нашей карточке: страница не должна ждать чужой сервис.
    panel_expires_at = None
    if settings.remnawave.is_configured:
        try:
            account = await asyncio.wait_for(activation.panel_account_of(device), timeout=3)
            panel_expires_at = activation.panel_expiry_of(account)
        except TimeoutError:
            log.warning("fleet.panel_timeout", device_id=device_id)

    return {
        "router": _device_payload(device, now=now),
        "client": {
            "id": device.user_id,
            "telegram": device.user.telegram_name if device.user else "",
            "name": device.user.display_name if device.user else "",
            "email": (device.user.email or "") if device.user else "",
            "phone": (device.user.phone or "") if device.user else "",
        },
        "subscription": {
            "status": str(subscription.status) if subscription else "",
            "label": _label(str(subscription.status), SUBSCRIPTION_LABELS) if subscription else "",
            "until": _iso(subscription.expires_at) if subscription else None,
            "here": bool(subscription and subscription.device_id == device.id),
            "elsewhere": _elsewhere(subscription, device),
        },
        "panel": {
            "username": activation.manual_username_for(device.mac),
            "until": _iso(panel_expires_at),
            "active": bool(panel_expires_at and panel_expires_at > now),
        },
        # Журнал в карточке — только последние строки. Полный лежит своей
        # страницей: за месяц у роутера их набегают сотни, и карточка
        # превращалась в ленту, где ничего не найти.
        "events": [
            {"at": _iso(event.created_at), "level": event.level, "message": event.message}
            for event in events[:CARD_EVENTS]
        ],
        "quick_commands": [
            {"name": name, "title": title}
            for name, (title, _command) in router_shell.QUICK_COMMANDS.items()
        ],
    }


async def _device_or_404(session: AsyncSession, device_id: int) -> Device:
    device = await session.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return device


def _days(payload: dict) -> int:
    try:
        return max(min(int(payload.get("days", 30)), 3650), 1)
    except (TypeError, ValueError):
        return 30


@router.post("/routers/{device_id}/poll", dependencies=[Depends(require_token)])
async def poll_router(device_id: int, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Снять показания прямо сейчас. Туннель к роутеру есть только у нас."""
    device = await _device_or_404(session, device_id)
    if not device.frp_visitor_port:
        await routers_service.ensure_frp_binding(session, device)

    try:
        payload = await RouterApi(device.frp_visitor_port or 0).stats()
    except Exception as exc:  # noqa: BLE001 — причину показываем оператору как есть
        log.warning("fleet.poll_failed", device_id=device_id, error=str(exc))
        return {"ok": False, "error": "Роутер не ответил: " + str(exc)[:160]}

    stats = routers_service.parse_stats(payload)
    routers_service.apply_stats(device, stats)
    routers_service.record_metrics(session, device, stats)
    return {"ok": True, "router": _device_payload(device, now=utcnow())}


@router.post("/routers/{device_id}/activate", dependencies=[Depends(require_token)])
async def activate_router(
    device_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Ручная активация: учётка в панели по MAC и доставка ссылки роутеру."""
    device = await _device_or_404(session, device_id)
    try:
        until = await activation.activate_manually(session, device=device, days=_days(payload))
    except activation.ActivationError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "until": until.isoformat()}


@router.post("/routers/{device_id}/extend", dependencies=[Depends(require_token)])
async def extend_router(
    device_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    device = await _device_or_404(session, device_id)
    try:
        until = await activation.extend_manually(session, device=device, days=_days(payload))
    except activation.ActivationError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "until": until.isoformat()}


@router.post("/routers/{device_id}/bind", dependencies=[Depends(require_token)])
async def bind_router(
    device_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Привязка клиента по почте, телефону, @username или id."""
    device = await _device_or_404(session, device_id)
    query = str(payload.get("client", "")).strip()
    user = await routers_service.find_user(session, query) if query else None
    if user is None:
        return {"ok": False, "error": "Клиент не найден: " + query[:60]}

    device.user_id = user.id
    if device.status in (DeviceStatus.NEW, DeviceStatus.REVOKED):
        # REVOKED тоже: иначе заново привязанный роутер остаётся отвязанным
        # и кабинет клиента его прячет.
        device.status = DeviceStatus.ASSIGNED
    routers_service.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="info",
        message="Привязан клиент " + user.display_name,
    )
    return {"ok": True, "client": user.display_name}


@router.post("/routers/{device_id}/unbind", dependencies=[Depends(require_token)])
async def unbind_router(device_id: int, session: AsyncSession = Depends(get_transaction)) -> dict:
    device = await _device_or_404(session, device_id)
    _unbind(session, device)
    return {"ok": True}


@router.post("/routers/{device_id}/reset", dependencies=[Depends(require_token)])
async def reset_router(device_id: int, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Возвращает роутер на склад: как будто его только завели.

    «Отвязать клиента» для этого мало: он снимает владельца, но оставляет
    отметку об активации и привязку к заказу, а автоактивация начинается
    с проверки «уже активирован — не трогать». Повторно пройти путь
    «заказ → отгрузка → роутер вышел на связь → подписка» после отвязки
    было нельзя, и проверить его целиком — тоже.

    Подписка, лежавшая на этом роутере, возвращается в ожидание активации:
    клиент за неё заплатил, и сжигать её вместе со сбросом железа нельзя.
    Срок пойдёт заново с того момента, когда роутер снова выйдет на связь.
    """
    device = await _device_or_404(session, device_id)

    subscription = await session.scalar(
        select(Subscription).where(Subscription.device_id == device.id)
    )
    returned = False
    if subscription is not None:
        subscription.device_id = None
        subscription.status = SubscriptionStatus.PENDING
        subscription.started_at = None
        subscription.expires_at = None
        subscription.grace_until = None
        subscription.last_reminder_day = None
        subscription.pending_expires_at = utcnow() + dt.timedelta(
            days=settings.subscription.activation_deadline_days
        )
        returned = True

    was_order = device.order_id
    device.user_id = None
    device.order_id = None
    device.activated_at = None
    device.status = DeviceStatus.NEW

    routers_service.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="warning",
        message="Сброшен на склад"
        + (f", заказ {was_order} отвязан" if was_order else "")
        + (", подписка возвращена в ожидание активации" if returned else ""),
    )
    log.info("fleet.device_reset", device_id=device.id, mac=device.mac, subscription=returned)
    # Учётку в панели не трогаем: удаления у клиента панели нет, а гасить её
    # молча — значит оставить оператора гадать, почему роутер не работает.
    # Она видна в карточке и убирается там же.
    return {"ok": True, "subscription_returned": returned}


def _unbind(session: AsyncSession, device: Device) -> None:
    device.user_id = None
    device.status = DeviceStatus.NEW if device.activated_at is None else DeviceStatus.REVOKED
    routers_service.add_event(
        session, device_id=device.id, mac=device.mac, level="warning", message="Клиент отвязан"
    )


BULK_LIMITS = {
    # Записи в базе — мгновенные, тут предел только про здравый смысл.
    "status": 200,
    "unbind": 200,
    # Поход к роутеру по туннелю: пятнадцать секунд на молчащий. Идут
    # одновременно, поэтому сорок штук укладываются примерно в минуту.
    "poll": 40,
    "reboot": 40,
    # Активация — это учётка в панели, ссылка по SSH и запись срока, до минуты
    # на роутер, и всё это последовательно: параллелить многошаговую работу
    # с одной сессией базы нельзя.
    "activate": 5,
    # Своя команда: тот же путь, что у консоли в карточке, только на многих.
    "command": 40,
    # Готовый скрипт — та же команда, но из закрытого набора: опечататься
    # в том, что уходит на роутер клиента, здесь нечем.
    "quick": 40,
}
"""Свой предел на действие, а не общий.

Общий в двести упирался в таймаут админки: перезагрузка десяти молчащих
роутеров занимает больше, чем она готова ждать. Оператор видел «приложение
не ответило», роутеры при этом перезагружались, и второй щелчок перезагружал
их снова."""

BULK_CONCURRENCY = 8
"""Сколько роутеров опрашиваем одновременно. Это ожидание сети, а не работа,
и последовательный обход сорока молчащих — десять минут вместо минуты.
Предел нужен, чтобы не открыть сорок SSH-сессий разом через один туннель."""

REBOOT_COMMAND = "sleep 2 && reboot"
"""Перезагрузка идёт по SSH через туннель — другого пути к роутеру у нас нет.

`sleep 2` перед ней нужен, чтобы SSH успел закрыть сессию: без задержки
роутер уходит в перезагрузку прямо в разговоре, клиент получает обрыв
и считает команду неудавшейся, хотя она как раз сработала."""


async def _over_the_tunnel(
    session: AsyncSession, devices: list[Device], work
) -> list[tuple[Device, str]]:
    """Гоняет `work(device)` по роутерам одновременно и возвращает отказы.

    Одновременно, потому что это ожидание сети: молчащий роутер держит
    пятнадцать секунд, и сорок таких подряд — десять минут, за которые
    админка успевает бросить запрос.

    Туннели поднимаем до, а записи в журнал делаем после — всё это работа
    с сессией базы, а её нельзя трогать из нескольких задач сразу.
    """
    for device in devices:
        if not device.frp_visitor_port:
            await routers_service.ensure_frp_binding(session, device)
    await session.flush()

    guard = asyncio.Semaphore(BULK_CONCURRENCY)

    async def one(device: Device):
        async with guard:
            try:
                return device, "", await work(device)
            except Exception as exc:  # noqa: BLE001 — причину показываем как есть
                log.warning("fleet.bulk_device_failed", device_id=device.id, error=str(exc))
                return device, str(exc)[:120] or "не ответил", None

    return list(await asyncio.gather(*(one(device) for device in devices)))


@router.post("/routers/bulk", dependencies=[Depends(require_token)])
async def bulk_routers(payload: dict, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Одно действие над несколькими роутерами: опрос, состояние, отвязка.

    Каждый роутер обрабатывается отдельно, и отказ одного не отменяет
    остальных: половина парка молчит всегда, и оператор, отметивший тридцать
    строк, должен получить двадцать восемь сделанных и список из двух, а не
    ошибку на всё разом.
    """
    action = str(payload.get("action", "")).strip()
    raw_ids = payload.get("ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return {"ok": False, "error": "Не выбрано ни одного роутера."}

    target: DeviceStatus | None = None
    if action == "status":
        try:
            target = DeviceStatus(str(payload.get("status", "")).strip())
        except ValueError:
            return {"ok": False, "error": "Неизвестное состояние."}
    elif action not in BULK_LIMITS:
        return {"ok": False, "error": "Неизвестное действие."}

    limit = BULK_LIMITS[action]
    if len(raw_ids) > limit:
        return {
            "ok": False,
            "error": (
                f"За раз так можно обработать не больше {limit} роутеров — "
                "иначе страница отвалится по времени раньше, чем работа кончится."
            ),
        }

    ids = [int(value) for value in raw_ids if str(value).strip().isdigit()]
    devices = list(await session.scalars(select(Device).where(Device.id.in_(ids))))

    days = _days(payload)
    done, failed = 0, []
    outputs: list[dict] = []

    command = str(payload.get("command", "")).strip()
    if action == "command":
        if not command:
            return {"ok": False, "error": "Пустая команда."}
        if any(bad in command.lower() for bad in FORBIDDEN_COMMANDS):
            log.warning("fleet.bulk_forbidden", command=command[:120], asked=len(raw_ids))
            return {"ok": False, "error": "Эта команда запрещена из веб-консоли."}

    # Готовый скрипт называется именем из набора, а не текстом: так на роутеры
    # уходит ровно то, что написано в коде, и проверять его на запрещённое
    # не нужно — набор закрытый.
    quick_name = str(payload.get("quick", "")).strip()
    quick_title = ""
    if action == "quick":
        entry = router_shell.QUICK_COMMANDS.get(quick_name)
        if entry is None:
            return {"ok": False, "error": "Неизвестный скрипт."}
        quick_title, command = entry

    if action in {"poll", "reboot", "command", "quick"}:
        if action == "reboot":

            async def work(device: Device):
                return await router_shell.run(device, REBOOT_COMMAND)
        elif action in {"command", "quick"}:

            async def work(device: Device):
                return await router_shell.run(device, command)
        else:

            async def work(device: Device):
                return await RouterApi(device.frp_visitor_port or 0).stats()

        for device, error, result in await _over_the_tunnel(session, devices, work):
            if error:
                failed.append(f"{device.mac}: {error}")
                continue
            # Записи в базу — после обхода и по одной: сессия одна на запрос.
            if action == "poll":
                stats = routers_service.parse_stats(result)
                routers_service.apply_stats(device, stats)
                routers_service.record_metrics(session, device, stats)
            elif action in {"command", "quick"}:
                # Вывод показываем целиком: ради него команду и запускали.
                outputs.append(
                    {"mac": device.mac, "ok": result.ok, "output": result.output[:4000]}
                )
                # В журнале устройства — название скрипта, а не его текст:
                # через полгода «Туннель: перезапустить» скажет больше, чем
                # строка с путями к init-скриптам.
                routers_service.add_event(
                    session,
                    device_id=device.id,
                    mac=device.mac,
                    level="info",
                    message=(
                        f"Скрипт из админки: {quick_title}"
                        if action == "quick"
                        else "Команда из админки: " + command[:120]
                    ),
                )
            else:
                routers_service.add_event(
                    session,
                    device_id=device.id,
                    mac=device.mac,
                    level="warning",
                    message="Перезагрузка из админки",
                )
            done += 1
    else:
        for device in devices:
            try:
                if action == "status":
                    if device.status is not target:
                        was = str(device.status)
                        device.status = target
                        routers_service.add_event(
                            session,
                            device_id=device.id,
                            mac=device.mac,
                            level="warning" if target is DeviceStatus.BLOCKED else "info",
                            message=f"Состояние: {was} → {target}",
                        )
                elif action == "unbind":
                    _unbind(session, device)
                elif action == "activate":
                    await activation.activate_manually(session, device=device, days=days)
            except activation.ActivationError as exc:
                failed.append(f"{device.mac}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 — причину показываем как есть
                log.warning("fleet.bulk_failed", device_id=device.id, action=action, error=str(exc))
                failed.append(f"{device.mac}: {str(exc)[:120]}")
                continue
            done += 1

    missing = len(ids) - len(devices)
    log.info("fleet.bulk", action=action, asked=len(ids), done=done, failed=len(failed))
    return {
        "ok": True,
        "done": done,
        "failed": failed,
        "missing": missing,
        "outputs": outputs,
    }


# --- Роутеры клиента ---------------------------------------------------------
#
# Та же привязка, что в карточке роутера и в заказе, но со стороны человека:
# оператор открывает клиента и видит, какое железо за ним числится. Ключ —
# `tg_id`: у бота свои пользователи, у нас свои, общее между ними только он.


CLIENTS_LIMIT = 200
"""Столько клиентов уходит в выпадающий список привязки. Дальше выбирать
глазами всё равно нельзя — там понадобится поиск, а не длинный список."""


@router.get("/clients", dependencies=[Depends(require_token)])
async def clients_list(session: AsyncSession = Depends(get_session)) -> dict:
    """Клиенты для выбора при привязке роутера.

    Раньше оператор вводил почту, телефон или id строкой и узнавал об опечатке
    только по отказу. Список из базы такой возможности не оставляет.
    """
    users = list(
        await session.scalars(select(User).order_by(User.id.desc()).limit(CLIENTS_LIMIT))
    )
    return {
        "total": len(users),
        "limit": CLIENTS_LIMIT,
        "clients": [
            {
                # Привязка ищет клиента по этому значению: tg_id понятнее
                # внутреннего номера и совпадает с тем, что видно в их админке.
                "value": str(user.tg_id or user.id),
                "tg_id": user.tg_id,
                "name": user.display_name,
                "username": user.username or "",
                "phone": user.phone or "",
            }
            for user in users
        ],
    }


@router.get("/clients/routers", dependencies=[Depends(require_token)])
async def routers_of_clients(tg_ids: str = "", session: AsyncSession = Depends(get_session)) -> dict:
    """MAC-адреса по списку клиентов — колонка «Роутер» в их списке клиентов.

    Одним запросом на страницу, а не по клиенту на строку: иначе список
    из тридцати человек стоил бы тридцати обращений по сети.
    """
    wanted = [int(part) for part in tg_ids.split(",") if part.strip().lstrip("-").isdigit()]
    if not wanted:
        return {"routers": {}}

    rows = await session.execute(
        select(User.id, User.tg_id, Device.id, Device.mac)
        .join(Device, Device.user_id == User.id)
        .where(User.tg_id.in_(wanted))
        .order_by(Device.id.desc())
    )
    found: dict[str, list[dict]] = {}
    owners: dict[str, int] = {}
    for user_id, tg_id, device_id, mac in rows:
        found.setdefault(str(tg_id), []).append({"id": device_id, "mac": mac})
        owners[str(tg_id)] = user_id

    # Подписка идёт той же картой: в их списке колонка «Подписка» читает их
    # таблицу, где у клиента роутера ничего нет и быть не может.
    subscriptions: dict[str, dict] = {}
    for tg_id, user_id in owners.items():
        current = await subscription_service.get_current(session, user_id)
        if current is not None:
            subscriptions[tg_id] = {
                "status": str(current.status),
                "label": _label(str(current.status), SUBSCRIPTION_LABELS),
                "until": _iso(current.expires_at),
            }

    return {
        "routers": found,
        "subscriptions": subscriptions,
        "traffic": await _panel_traffic(
            {tg_id: [item["mac"] for item in items] for tg_id, items in found.items()}
        ),
    }


async def _panel_traffic(macs_by_client: dict[str, list[str]]) -> dict[str, dict]:
    """Расход и последний выход в сеть по учёткам панели.

    Одним запросом на всю страницу: список учёток панель отдаёт целиком,
    и спрашивать её по клиенту на строку значило бы тридцать запросов
    на один список.

    Учётка ищется тремя способами, потому что заводится она двумя путями:
    клиентская активация даёт `tg{id}_{mac}` и проставляет telegram_id, ручная
    из карточки — имя из MAC и без него. Искать только по первому значит
    не находить всё, что активировано руками, а это половина парка.

    Служба аналитики бота, которая наполняла его собственные колонки трафика,
    на сервере не установлена вовсе — цифры берутся отсюда.
    """
    if not macs_by_client or not settings.remnawave.is_configured:
        return {}
    try:
        accounts = await asyncio.wait_for(remnawave.client().users(), timeout=5)
    except (TimeoutError, remnawave.RemnawaveError) as exc:
        log.warning("fleet.panel_traffic_failed", error=str(exc))
        return {}

    by_manual_name = {
        activation.manual_username_for(mac): tg_id
        for tg_id, macs in macs_by_client.items()
        for mac in macs
    }

    found: dict[str, dict] = {}
    for account in accounts:
        owner = str(account.telegram_id or "")
        if owner not in macs_by_client:
            owner = ""
        if not owner and account.username.startswith("tg"):
            candidate = account.username[2:].split("_", 1)[0]
            owner = candidate if candidate in macs_by_client else ""
        if not owner:
            owner = by_manual_name.get(account.username, "")
        if not owner:
            continue

        current = found.setdefault(owner, {"used_bytes": 0, "online_at": ""})
        current["used_bytes"] += account.used_traffic_bytes
        if account.online_at > current["online_at"]:
            current["online_at"] = account.online_at
    return found


def _client_router_row(device: Device, *, now) -> dict:
    """Показания те же, что в карточке роутера: оператору, который открыл
    клиента по жалобе, они и нужны — а не только MAC."""
    seen = (device.last_heartbeat_at, device.last_poll_at, device.frp_last_seen_at)
    return {
        "id": device.id,
        "mac": device.mac,
        "model": device.model or device.board or "",
        "fw_version": device.fw_version or "",
        "status": str(device.status),
        "online": device.frp_online
        or device.is_online(threshold_min=settings.subscription.heartbeat_offline_min, now=now),
        "last_seen": _iso(max((value for value in seen if value), default=None)),
        "activated_at": _iso(device.activated_at),
        "clients": (device.clients_wifi or 0) + (device.clients_dhcp or 0),
        "uptime_sec": device.uptime_sec or 0,
        "rx_bytes": device.rx_bytes or 0,
        "tx_bytes": device.tx_bytes or 0,
        "wan_ip": device.last_wan_ip or "",
    }


@router.get("/clients/{tg_id}/routers", dependencies=[Depends(require_token)])
async def client_routers(tg_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Роутеры клиента и список свободных — из него оператор выбирает при привязке."""
    now = utcnow()
    user = await session.scalar(select(User).where(User.tg_id == tg_id))
    devices = (
        list(
            await session.scalars(
                select(Device).where(Device.user_id == user.id).order_by(Device.id.desc())
            )
        )
        if user is not None
        else []
    )
    free = list(
        await session.scalars(
            select(Device)
            .where(Device.user_id.is_(None))
            .order_by(Device.id.desc())
            .limit(FREE_DEVICES_LIMIT)
        )
    )
    current = await subscription_service.get_current(session, user.id) if user is not None else None

    # Расход по счётчику панели: он показывает, сколько ушло через подписку,
    # а счётчики роутера считают и домашний трафик тоже. Ждать панель долго
    # нельзя — карточка клиента не должна открываться по пять секунд.
    panel_used = None
    if devices and settings.remnawave.is_configured:
        try:
            account = await asyncio.wait_for(activation.panel_account_of(devices[0]), timeout=3)
            panel_used = account.used_traffic_bytes if account else None
        except TimeoutError:
            log.warning("fleet.panel_timeout", tg_id=tg_id)

    return {
        "has_client": user is not None,
        "routers": [_client_router_row(device, now=now) for device in devices],
        "free": [{"mac": device.mac, "model": device.model or device.board or ""} for device in free],
        "subscription": {
            "status": str(current.status) if current else "",
            "label": _label(str(current.status), SUBSCRIPTION_LABELS) if current else "",
            "until": _iso(current.expires_at) if current else None,
        },
        "panel_used_bytes": panel_used,
    }


FREE_DEVICES_LIMIT = 50
"""Подсказка для поля ввода, а не полный склад: список там и так есть."""


@router.post("/clients/{tg_id}/routers", dependencies=[Depends(require_token)])
async def bind_client_router(
    tg_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Привязка роутера к клиенту по MAC."""
    mac = normalize_mac(str(payload.get("mac", "")))
    if not mac:
        return {"ok": False, "error": "Некорректный MAC. Формат: A0:B1:C2:D3:E4:F5"}

    user = await session.scalar(select(User).where(User.tg_id == tg_id))
    if user is None:
        # Оператор привязывает осознанно, и отказ «клиента нет у нас» ему ничего
        # не объясняет: у бота-то клиент есть. Заводим строку и продолжаем.
        user = User(tg_id=tg_id, username=str(payload.get("username", "")).strip()[:64] or None)
        session.add(user)
        await session.flush()

    device, _ = await routers_service.get_or_create_by_mac(
        session, mac, model=str(payload.get("model", "")).strip()[:64]
    )
    if device.user_id and device.user_id != user.id:
        return {"ok": False, "error": f"Роутер {mac} уже привязан к другому клиенту."}

    device.user_id = user.id
    if device.status in (DeviceStatus.NEW, DeviceStatus.REVOKED):
        device.status = DeviceStatus.ASSIGNED
    routers_service.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="info",
        message="Привязан клиент " + user.display_name,
    )
    log.info("fleet.client_router_bound", tg_id=tg_id, mac=mac)
    return {"ok": True, "mac": mac}


@router.post("/clients/{tg_id}/routers/{device_id}/unbind", dependencies=[Depends(require_token)])
async def unbind_client_router(
    tg_id: int, device_id: int, session: AsyncSession = Depends(get_transaction)
) -> dict:
    device = await _device_or_404(session, device_id)
    user = await session.scalar(select(User).where(User.tg_id == tg_id))
    if user is None or device.user_id != user.id:
        return {"ok": False, "error": "Этот роутер числится не за этим клиентом."}

    device.user_id = None
    device.status = DeviceStatus.NEW if device.activated_at is None else DeviceStatus.REVOKED
    routers_service.add_event(
        session, device_id=device.id, mac=device.mac, level="warning", message="Клиент отвязан"
    )
    return {"ok": True}


@router.get("/settings", dependencies=[Depends(require_token)])
async def fleet_settings_read(session: AsyncSession = Depends(get_session)) -> dict:
    """Настройка автоактивации ровно одна — включена она или нет.

    Числа дней тут больше нет: активация выдаёт срок, который клиент оплатил
    при покупке, а не одинаковый для всех. Настройка существовала, хранилась
    и показывалась оператору, но в активации не читалась ни разу — то есть
    обещала не то, что происходило.
    """
    return {"auto_enabled": await settings_service.get_bool(session, "activation.auto_enabled")}


@router.post("/settings", dependencies=[Depends(require_token)])
async def fleet_settings_save(
    payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    await settings_service.set_setting(
        session, "activation.auto_enabled", bool(payload.get("auto_enabled"))
    )
    log.info("fleet.settings_saved", auto_enabled=bool(payload.get("auto_enabled")))
    return {"ok": True}


STOCK_PAGE_SIZE = 50


SHIPPED_DEVICE_STATUSES = (DeviceStatus.ASSIGNED, DeviceStatus.ACTIVE, DeviceStatus.REVOKED)
"""Уже не на полке: привязан к клиенту, работает у него или у него же изъят.
На складе таким делать нечего — склад отвечает на вопрос «что можно отгрузить»."""


@router.get("/devices", dependencies=[Depends(require_token)])
async def stock_list(
    q: str = "",
    page: int = 1,
    show_all: bool = Query(default=False, alias="all"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Склад устройств: те же роутеры, но до отгрузки.

    Отдельно от `/routers` не по прихоти: там показания и туннели, здесь —
    MAC, серийник, статус и заметка кладовщика, поиск и постранично, потому
    что коробок бывают сотни, а на связи из них — единицы.

    Отгруженные не показываются: роутер, уехавший к клиенту, со склада ушёл,
    и держать его в списке — значит каждый раз глазами отделять то, что можно
    положить в коробку, от того, что уже в пути. Найти его по-прежнему можно:
    поиск и `all=1` показывают всё.
    """
    page = max(page, 1)
    query = select(Device).options(selectinload(Device.user))
    counter = select(func.count()).select_from(Device)

    text = q.strip()
    if not show_all and not text:
        # При поиске отбор не применяем: ищут обычно как раз отгруженный,
        # чтобы понять, у кого он и что с ним.
        query = query.where(Device.status.notin_(SHIPPED_DEVICE_STATUSES))
        counter = counter.where(Device.status.notin_(SHIPPED_DEVICE_STATUSES))

    if text:
        mac = normalize_mac(text)
        pattern = f"%{text}%"
        condition = or_(
            Device.mac == mac if mac else Device.mac.ilike(pattern),
            Device.model.ilike(pattern),
            Device.serial.ilike(pattern),
        )
        query = query.where(condition)
        counter = counter.where(condition)

    total = await session.scalar(counter) or 0
    devices = list(
        await session.scalars(
            query.order_by(Device.id.desc())
            .limit(STOCK_PAGE_SIZE)
            .offset((page - 1) * STOCK_PAGE_SIZE)
        )
    )
    return {
        "total": total,
        "page": page,
        "pages": max((total + STOCK_PAGE_SIZE - 1) // STOCK_PAGE_SIZE, 1),
        "statuses": [str(status) for status in DeviceStatus],
        "devices": [
            {
                "id": device.id,
                "mac": device.mac,
                "model": device.model or device.board or "",
                "serial": device.serial or "",
                "status": str(device.status),
                "client": device.user.display_name if device.user else "",
                "activated_at": _iso(device.activated_at),
                "created_at": _iso(device.created_at),
                "note": device.admin_note or "",
            }
            for device in devices
        ],
    }


@router.post("/devices", dependencies=[Depends(require_token)])
async def stock_add(payload: dict, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Заведение устройства на складе до отгрузки."""
    mac = normalize_mac(str(payload.get("mac", "")))
    if not mac:
        return {"ok": False, "error": "Некорректный MAC. Формат: A0:B1:C2:D3:E4:F5"}
    exists = await session.scalar(select(Device).where(Device.mac == mac))
    if exists is not None:
        return {"ok": False, "error": f"MAC {mac} уже заведён."}

    device = Device(
        mac=mac,
        model=str(payload.get("model", "")).strip()[:64],
        serial=str(payload.get("serial", "")).strip()[:64] or None,
        status=DeviceStatus.NEW,
    )
    session.add(device)
    await session.flush()
    routers_service.add_event(
        session, device_id=device.id, mac=mac, level="info", message="Заведён на складе"
    )
    log.info("fleet.device_created", device_id=device.id, mac=mac)
    return {"ok": True, "id": device.id, "mac": mac}


@router.post("/routers/{device_id}/status", dependencies=[Depends(require_token)])
async def change_status(
    device_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    device = await _device_or_404(session, device_id)
    try:
        target = DeviceStatus(str(payload.get("status", "")).strip())
    except ValueError:
        return {"ok": False, "error": "Неизвестное состояние."}

    if device.status is target:
        return {"ok": True, "status": str(target)}
    was = str(device.status)
    device.status = target
    routers_service.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="warning" if target is DeviceStatus.BLOCKED else "info",
        message=f"Состояние: {was} → {target}",
    )
    log.info("fleet.device_status", device_id=device.id, was=was, now=str(target))
    return {"ok": True, "status": str(target)}


@router.post("/routers/{device_id}/note", dependencies=[Depends(require_token)])
async def save_note(
    device_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    device = await _device_or_404(session, device_id)
    device.admin_note = str(payload.get("note", "")).strip()[:2000] or None
    return {"ok": True}


@router.post("/routers/{device_id}/panel-ticket", dependencies=[Depends(require_token)])
async def panel_ticket_for(
    device_id: int, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Разовая ссылка на веб-панель роутера.

    Саму панель отдаём мы: туннель держит наш контейнер `frpc`, и проксировать
    её из их процесса физически нечем. Переносится не прокси, а вход — по этой
    ссылке оператор попадает на панель без входа в нашу админку.
    """
    device = await _device_or_404(session, device_id)
    if not device.frp_visitor_port:
        await routers_service.ensure_frp_binding(session, device)
    if not device.frp_visitor_port:
        return {"ok": False, "error": "Туннель к роутеру не выделен."}

    ticket = await panel_ticket.issue(
        device_id=device.id, port=device.frp_visitor_port, mac=device.mac
    )
    routers_service.add_event(
        session, device_id=device.id, mac=device.mac, level="info", message="Открыта веб-панель"
    )
    base = settings.api.admin_base_url.rstrip("/")
    return {"ok": True, "url": f"{base}/panel/open?ticket={ticket}"}


@router.post("/routers/{device_id}/ssh-password", dependencies=[Depends(require_token)])
async def ssh_password_for(
    device_id: int, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Пароль root на роутере — оператору, по кнопке.

    Считаем его мы: пароль выводится из MAC и соли `FRP_SSH_PASSWORD_SALT`,
    а соль есть только в нашем окружении. Их админка вывести его не может
    даже зная MAC — потому это ручка, а не поле в карточке.

    POST, а не GET, ровно по этой причине: пароль не должен оседать в истории
    браузера, в журнале прокси и в `Referer` соседних запросов.

    Показ пишется в журнал устройства. Пароль выводится из MAC, а тот написан
    на корпусе и виден в эфире — единственное, чем схема держится, это что
    её никто не проверял. Пока она не заменена на ключи, знать, кто и когда
    смотрел, — это всё, что у нас есть.
    """
    device = await _device_or_404(session, device_id)
    routers_service.add_event(
        session, device_id=device.id, mac=device.mac, level="info", message="Показан пароль root"
    )
    return {"ok": True, "password": router_shell.password_for(device)}


FORBIDDEN_COMMANDS = ("mkfs", "firstboot", "rm -rf /", "> /dev/mtd", "dd if=", "sysupgrade")
"""Перепрошивка и форматирование из веб-консоли — верный способ получить кирпич
у клиента на другом конце страны. Список тот же, что был в нашей админке."""


@router.post("/routers/{device_id}/console", dependencies=[Depends(require_token)])
async def console(
    device_id: int, payload: dict, session: AsyncSession = Depends(get_session)
) -> dict:
    """Разовая команда на роутере по SSH через туннель.

    Именно разовая, а не сессия: интерактивной консоли у нас никогда не было,
    и делать её ради переезда незачем. Читать журналы можно тем же способом —
    `logread`, `dmesg`.
    """
    device = await _device_or_404(session, device_id)
    command = str(payload.get("command", "")).strip()
    if not command:
        return {"ok": False, "error": "Пустая команда."}
    if any(bad in command.lower() for bad in FORBIDDEN_COMMANDS):
        log.warning("fleet.console_forbidden", device_id=device_id, command=command[:120])
        return {"ok": False, "error": "Эта команда запрещена из веб-консоли."}

    try:
        result = await router_shell.run(device, command)
    except router_shell.ShellError as exc:
        return {"ok": False, "error": str(exc)}

    routers_service.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="info",
        message="Консоль: " + command[:120],
    )
    await session.commit()
    return {"ok": result.ok, "output": result.output[:20000], "command": command}


@router.post("/routers/{device_id}/quick/{name}", dependencies=[Depends(require_token)])
async def quick_command(
    device_id: int, name: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """Готовый вопрос роутеру: аптайм, память, туннель, лог.

    Кнопкой, а не строкой в консоли: это те же полдюжины команд каждый раз,
    и набирать их руками — лишний повод опечататься в том, что уходит
    на устройство клиента.
    """
    device = await _device_or_404(session, device_id)
    try:
        result = await router_shell.run_quick(device, name)
    except router_shell.ShellError as exc:
        return {"ok": False, "error": str(exc)}

    title = router_shell.QUICK_COMMANDS[name][0]
    routers_service.add_event(
        session, device_id=device.id, mac=device.mac, level="info", message="Запрос: " + title
    )
    await session.commit()
    return {"ok": result.ok, "output": result.output[:20000], "command": title}


@router.get("/routers/{device_id}/events", dependencies=[Depends(require_token)])
async def router_events(
    device_id: int,
    page: int = Query(1, ge=1),
    level: str = Query("", max_length=16),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Полный журнал устройства своей страницей.

    В карточке остались последние строки: за месяц их набегают сотни, и
    карточка превращалась в ленту, где не найти ни привязку клиента, ни отказ
    активации — то, ради чего в журнал и заходят.
    """
    device = await _device_or_404(session, device_id)
    query = select(DeviceEvent).where(DeviceEvent.device_id == device.id)
    if level:
        query = query.where(DeviceEvent.level == level)

    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await session.scalars(
        query.order_by(DeviceEvent.id.desc())
        .limit(EVENTS_PAGE_SIZE)
        .offset((page - 1) * EVENTS_PAGE_SIZE)
    )
    return {
        "device": {"id": device.id, "mac": device.mac, "model": device.model or ""},
        "events": [
            {
                "at": _iso(row.created_at),
                "level": row.level,
                "message": row.message,
                "payload": row.payload or {},
            }
            for row in rows
        ],
        "levels": sorted(
            set(
                await session.scalars(
                    select(DeviceEvent.level).where(DeviceEvent.device_id == device.id).distinct()
                )
            )
        ),
        "total": total,
        "page": page,
        "pages": max(1, (total + EVENTS_PAGE_SIZE - 1) // EVENTS_PAGE_SIZE),
        "retention_days": EVENTS_RETENTION_DAYS,
    }


@router.post("/routers/{device_id}/events/clear", dependencies=[Depends(require_token)])
async def clear_router_events(
    device_id: int, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Чистит журнал устройства целиком — по кнопке оператора."""
    device = await _device_or_404(session, device_id)
    result = await session.execute(
        delete(DeviceEvent).where(DeviceEvent.device_id == device.id)
    )
    count = result.rowcount or 0
    routers_service.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="warning",
        message=f"Журнал очищен, удалено записей: {count}",
    )
    log.info("fleet.events_cleared", device_id=device.id, count=count)
    return {"ok": True, "count": count}


# ── Списки доменов ────────────────────────────────────────────────────────────


@router.get("/lists", dependencies=[Depends(require_token)])
async def lists_state(session: AsyncSession = Depends(get_session)) -> dict:
    """Состояние страницы списков: источники, свой список, итог прошлой сборки."""
    sources = (
        await session.scalars(select(DomainSource).order_by(DomainSource.sort_order, DomainSource.id))
    ).all()
    manual = {row.kind: row for row in (await session.scalars(select(ManualList))).all()}
    last = await session.scalar(select(DomainBuild).order_by(DomainBuild.id.desc()).limit(1))
    base = settings.api.public_base_url.rstrip("/")
    return {
        "sources": [
            {
                "id": s.id,
                "url": s.url,
                "title": s.title,
                "kind": s.kind,
                "is_enabled": s.is_enabled,
                "last_lines": s.last_lines,
                "last_error": s.last_error,
                "last_ok_at": s.last_ok_at.isoformat() if s.last_ok_at else None,
            }
            for s in sources
        ],
        "manual": {
            kind: {
                "body": row.body if row else "",
                "updated_by": row.updated_by if row else "",
                "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
            }
            for kind, row in ((k, manual.get(k)) for k in ("domain", "ip"))
        },
        "config": {
            key: ("задан" if value else "")
            if key in domain_lists.SECRET_KEYS
            else value
            for key, value in (await domain_lists.config(session)).items()
        },
        "poll_intervals": list(domain_lists.POLL_INTERVALS),
        # Каждый список отдельной записью: домены и подсети — разные файлы
        # по разным адресам, и на странице их надо видеть порознь, а не одной
        # строкой «доменов N, подсетей M».
        "files": [
            {
                "kind": kind,
                "title": title,
                "name": domain_lists.FILE_NAMES[kind],
                "url": f"{base}/lists/{domain_lists.FILE_NAMES[kind]}",
                "lines": len(domain_lists.read_list(kind).splitlines()),
            }
            for kind, title in (("domain", "Домены"), ("ip", "Подсети IPv4"))
        ],
        "last_build": (
            {
                "domains": last.domains,
                "ips": last.ips,
                "failed_sources": last.failed_sources,
                "finished_at": last.finished_at.isoformat() if last.finished_at else None,
                "error": last.error,
                "uploaded": last.uploaded,
            }
            if last
            else None
        ),
    }


@router.post("/lists/sources", dependencies=[Depends(require_token)])
async def source_add(payload: dict, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Добавляет источник. Адрес уникален — тот же список дважды не нужен."""
    url = str(payload.get("url") or "").strip()
    kind = str(payload.get("kind") or "domain").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "Адрес должен начинаться с http:// или https://"}
    if kind not in ListKind.ALL:
        return {"ok": False, "error": "Неизвестный вид списка."}
    if await session.scalar(select(DomainSource.id).where(DomainSource.url == url)):
        return {"ok": False, "error": "Такой источник уже заведён."}
    session.add(
        DomainSource(url=url, title=str(payload.get("title") or "").strip()[:120], kind=kind, sort_order=500)
    )
    return {"ok": True}


@router.post("/lists/sources/{source_id}/toggle", dependencies=[Depends(require_token)])
async def source_toggle(source_id: int, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Включает или выключает источник. Выключенный остаётся в таблице."""
    source = await session.get(DomainSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Источник не найден")
    source.is_enabled = not source.is_enabled
    return {"ok": True, "is_enabled": source.is_enabled}


@router.post("/lists/sources/{source_id}/delete", dependencies=[Depends(require_token)])
async def source_delete(source_id: int, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Удаляет источник совсем — для заведённых по ошибке.

    Обычный способ убрать категорию — выключить: адрес `.lst` через месяц
    не вспомнить, а включают их сезонно.
    """
    source = await session.get(DomainSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Источник не найден")
    await session.delete(source)
    return {"ok": True}


@router.post("/lists/manual/{kind}", dependencies=[Depends(require_token)])
async def manual_save(kind: str, payload: dict, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Сохраняет свой список целиком и запоминает, кто правил.

    Чистка тут не делается намеренно: оператор должен видеть в поле ровно то,
    что вставил, включая строки, которые сборка потом отбросит. Причёсывать
    ввод на сохранении — верный способ незаметно потерять его правку.
    """
    if kind not in ListKind.ALL:
        raise HTTPException(status_code=404, detail="Неизвестный вид списка")
    body = str(payload.get("body") or "")
    added, removed = await domain_lists.save_manual(
        session, kind, body, author=str(payload.get("author") or "")
    )
    values = domain_lists.CLEANERS[kind](body)
    return {"ok": True, "accepted": len(values), "added": added, "removed": removed}


@router.post("/lists/config", dependencies=[Depends(require_token)])
async def lists_config_save(payload: dict, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Интервал круга и доступ к хранилищу. Пустой секрет не затирает прежний."""
    await domain_lists.save_config(session, {k: str(v) for k, v in payload.items()})
    return {"ok": True}


@router.get("/lists/manual/{kind}/history", dependencies=[Depends(require_token)])
async def manual_history(kind: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Когда список меняли, кто и насколько.

    Тело прежней версии наружу не отдаём списком — оно нужно только при
    откате, и таскать сотню килобайт ради таблицы из трёх колонок незачем.
    """
    if kind not in ListKind.ALL:
        raise HTTPException(status_code=404, detail="Неизвестный вид списка")
    return {
        "revisions": [
            {
                "id": item.id,
                "author": item.author,
                "added": item.added,
                "removed": item.removed,
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in await domain_lists.revisions(session, kind)
        ]
    }


@router.post("/lists/manual/{kind}/restore/{revision_id}", dependencies=[Depends(require_token)])
async def manual_restore(
    kind: str, revision_id: int, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Возвращает список к прежней версии.

    Сам откат тоже попадает в историю — иначе «вернул и снова сломал»
    не отследить, а именно так и выглядит вечер, когда что-то пошло не так.
    """
    if kind not in ListKind.ALL:
        raise HTTPException(status_code=404, detail="Неизвестный вид списка")
    revision = await session.get(ManualListRevision, revision_id)
    if revision is None or revision.kind != kind:
        raise HTTPException(status_code=404, detail="Версия не найдена")
    await domain_lists.save_manual(
        session, kind, revision.body, author=f"откат к версии {revision.id}"
    )
    return {"ok": True}


@router.post("/lists/manual/{kind}/import", dependencies=[Depends(require_token)])
async def manual_import(kind: str, payload: dict) -> dict:
    """Тянет свой список из файла по ссылке — для переезда с GitHub.

    Ничего не сохраняет: отдаёт причёсанное содержимое, чтобы оператор увидел
    его в поле и решил, заменять целиком или дописать. Молча подменить чужим
    файлом то, что человек правил руками, нельзя.
    """
    if kind not in ListKind.ALL:
        raise HTTPException(status_code=404, detail="Неизвестный вид списка")
    body, error = await domain_lists.import_from_url(str(payload.get("url") or "").strip(), kind)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "body": body, "lines": len(body.splitlines())}


@router.post("/lists/build", dependencies=[Depends(require_token)])
async def lists_build(session: AsyncSession = Depends(get_transaction)) -> dict:
    """Пересобирает списки сейчас, не дожидаясь круга по расписанию.

    `force=True`: кнопку жмут как раз тогда, когда хотят увидеть результат,
    а метки версий источников про это ничего не знают — с ними круг ответил бы
    «ничего не изменилось» и не собрал ничего.
    """
    try:
        record = await domain_lists.build(session, force=True)
    except Exception as exc:  # noqa: BLE001 — оператор должен увидеть причину, а не 500
        log.error("fleet.lists_build_failed", error=str(exc))
        return {"ok": False, "error": f"Сборка не удалась: {exc}"[:255]}
    return {
        "ok": True,
        "domains": record.domains,
        "ips": record.ips,
        "failed_sources": record.failed_sources,
        "skipped": record.skipped,
    }
