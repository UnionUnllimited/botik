"""Роутеры в рабочем чате: карточка, кнопки и постановка в очередь.

Заказы уже приходят в топики рабочего чата, и вся работа с ними — кнопками
с телефона. Роутеры теперь так же: один топик «Роутеры» в том же чате, в нём
карточка, когда роутеру нужна рука, и короткое сообщение, когда он вернулся.

Повода два — те же, что в утренней сводке и на главной, и считает их тот же
`monitoring.collect`: роутер у клиента молчит больше суток; роутер на связи,
а оплаченная подписка на нём так и не включилась.

**Текст и кнопки собираются только здесь.** Их рисуют первое сообщение
и обновление после каждого нажатия — разъехавшись, они показали бы одно,
а делали другое. Карточка описывает состояние роутера сейчас, а не повод,
по которому её прислали: нажал «Проверить снова» — увидел, что он вернулся.

Отправлять сами мы не умеем — токен у бота. Карточка ложится в очередь
`notifications`, бот шлёт её в топик и возвращает номер созданного топика.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.dates import ensure_utc, to_display, utcnow
from core.enums import OrderStatus
from core.models import Device, Notification, Order, User
from core.notifications import notify_admins
from core.services import monitoring, order_topics, settings_service
from core.services import subscriptions as subscription_service
from core.texts import ORDER_STATUS_TITLES

log = structlog.get_logger("services.router_topics")

KIND = "router_topic"
THREAD_SETTING = "routers.topic_thread_id"
TOPIC_TITLE = "Роутеры"
CALLBACK_PREFIX = "rtr"
"""`rtr:<роутер>:<действие>`. Telegram даёт на callback 64 байта —
действия короткие, как у заказов."""

SILENT = "silent"
STUCK = "stuck"

SHIPPED = (OrderStatus.SHIPPED, OrderStatus.DELIVERED)


def callback(device_id: int, action: str) -> str:
    return f"{CALLBACK_PREFIX}:{device_id}:{action}"


def _ago(moment: dt.datetime | None, now: dt.datetime) -> str:
    if moment is None:
        return "никогда"
    minutes = max(0, int((now - ensure_utc(moment)).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} мин"
    if minutes < 1440:
        return f"{minutes // 60} ч"
    days, hours = divmod(minutes // 60, 24)
    return f"{days} дн {hours} ч" if hours else f"{days} дн"


def is_online(device: Device, now: dt.datetime) -> bool:
    return device.frp_online or device.is_online(
        threshold_min=settings.subscription.heartbeat_offline_min, now=now
    )


async def _order(session: AsyncSession, device: Device) -> Order | None:
    return await session.get(Order, device.order_id) if device.order_id else None


def headline(device: Device, order: Order | None, now: dt.datetime) -> tuple[str, str]:
    """Что с роутером сейчас — одной строкой, и вид повода (или пусто)."""
    if not is_online(device, now):
        return f"📡 <b>{device.mac}</b> молчит {_ago(monitoring.last_seen(device), now)}", SILENT
    if device.activated_at is None and order is not None and order.status in SHIPPED:
        return f"⚠️ <b>{device.mac}</b> на связи, а подписка не включилась", STUCK
    return f"✅ <b>{device.mac}</b> на связи, всё в порядке", ""


async def card_text(session: AsyncSession, device: Device, *, now: dt.datetime | None = None) -> str:
    now = now or utcnow()
    order = await _order(session, device)
    head, _kind = headline(device, order, now)
    user = await session.get(User, device.user_id) if device.user_id else None
    lines = [head, ""]
    if user is not None:
        name = user.display_name if user.display_name != user.telegram_name else ""
        lines.append(f"Клиент: {user.telegram_name}{' · ' + name if name else ''}")
        if user.phone:
            lines.append(f"Телефон: {user.phone}")
    else:
        lines.append("Клиент: не привязан")
    if order is not None:
        status = ORDER_STATUS_TITLES.get(str(order.status), str(order.status))
        lines.append(f"Заказ: {order.public_number} · {status}")

    subscription = await subscription_service.get_for_device(session, device.id)
    if subscription is not None and subscription.expires_at:
        lines.append(f"Подписка: до {to_display(subscription.expires_at).strftime('%d.%m.%Y')}")
    elif device.activated_at is None:
        lines.append("Подписка: не включена")

    model = device.model or device.board or ""
    tail = " · ".join(part for part in (model, device.last_wan_ip or "") if part)
    if tail:
        lines.append(tail)
    seen = monitoring.last_seen(device)
    if seen is not None:
        lines.append(f"На связи последний раз: {to_display(seen).strftime('%d.%m %H:%M')}")
    return "\n".join(lines)


async def card_buttons(
    session: AsyncSession, device: Device, *, now: dt.datetime | None = None
) -> list[dict]:
    """Кнопки — только те, что сейчас имеют смысл.

    Молчащему роутеру туннель не откроется: «Перезагрузить» ответило бы
    отказом, а с телефона такой отказ читается как поломка. Ему — «Проверить
    снова» и «Написать клиенту»: чаще всего роутер просто выключили.
    """
    now = now or utcnow()
    order = await _order(session, device)
    _head, kind = headline(device, order, now)
    online = is_online(device, now)
    buttons: list[dict[str, str]] = []
    if kind == STUCK:
        buttons.append({"text": "▶ Попробовать активировать", "data": callback(device.id, "act")})
    if online:
        buttons.append({"text": "↻ Перезагрузить", "data": callback(device.id, "reboot")})
    user = await session.get(User, device.user_id) if device.user_id else None
    if user is not None and user.tg_id:
        buttons.append({"text": "✉ Написать клиенту", "data": callback(device.id, "dm")})
    buttons.append({"text": "⟳ Проверить снова", "data": callback(device.id, "card")})
    return buttons


async def card(session: AsyncSession, device: Device) -> dict[str, Any]:
    """Карточка целиком: для первого сообщения и для обновления по кнопке."""
    now = utcnow()
    user = await session.get(User, device.user_id) if device.user_id else None
    return {
        "text": await card_text(session, device, now=now),
        "buttons": await card_buttons(session, device, now=now),
        "client_tg_id": (user.tg_id or 0) if user is not None else 0,
    }


async def thread_id(session: AsyncSession) -> int:
    raw = await settings_service.get_str(session, THREAD_SETTING)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


async def remember_topic(session: AsyncSession, thread: int) -> None:
    """Номер топика, который завёл бот. Только если его ещё нет: второй
    топик «Роутеры» разнёс бы тревоги по двум веткам."""
    if thread and not await thread_id(session):
        await settings_service.set_setting(session, THREAD_SETTING, int(thread))
        log.info("router_topic.remembered", thread_id=thread)


async def _ensure_topic(session: AsyncSession, chat: int) -> int:
    """Номер топика. Ноль — топика ещё нет, и заводящее сообщение в пути.

    Тревоги до появления номера не шлём: каждая с названием завела бы свой
    топик. Шлём одно вступительное с названием — бот заведёт топик и вернёт
    номер, а тревоги уйдут на следующем круге.
    """
    thread = await thread_id(session)
    if thread:
        return thread
    pending = await session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.kind == KIND, Notification.sent_at.is_(None), Notification.topic_title != "")
    )
    if not pending:
        session.add(
            Notification(
                tg_id=0,
                text=(
                    "Сюда приходят роутеры, которым нужна рука: молчат у клиента больше суток "
                    "или на связи, а подписка не включилась. Когда роутер вернётся — "
                    "придёт короткое сообщение."
                ),
                buttons=[],
                kind=KIND,
                chat_id=chat,
                thread_id=None,
                topic_title=TOPIC_TITLE,
            )
        )
        log.info("router_topic.creating")
    return 0


async def push(session: AsyncSession, device: Device, *, note: str = "") -> Notification | None:
    """Карточка роутера в топик. None — не ушла: чат не задан или топик
    ещё заводится; тогда повод останется неотмеченным и уйдёт следующим кругом."""
    chat = await order_topics.chat_id(session)
    if not chat:
        return None
    thread = await _ensure_topic(session, chat)
    if not thread:
        return None
    body = await card(session, device)
    message = Notification(
        tg_id=body["client_tg_id"],
        text=f"{note}\n\n{body['text']}" if note else body["text"],
        buttons=body["buttons"],
        kind=KIND,
        chat_id=chat,
        thread_id=thread,
        topic_title="",
    )
    session.add(message)
    log.info("router_topic.queued", device_id=device.id)
    return message


async def post(
    session: AsyncSession, text: str, buttons: list[dict], *, client_tg_id: int = 0
) -> bool:
    """Своё сообщение с кнопками в топик «Роутеры»: просьба о сайте, отзыв.

    Топика ещё нет — заводим, а само сообщение уходит в общую ленту того же
    чата: ждать круга, как у тревог, тут нельзя — это разовая просьба,
    второго повода прислать её не будет. Кнопки работают и там: обработчик
    узнаёт их по префиксу, а не по топику. Чат не задан — владельцу в личку,
    уже без кнопок. Запись в админке есть в любом случае."""
    chat = await order_topics.chat_id(session)
    if not chat:
        await notify_admins(text, session=session, kind=KIND)
        return False
    thread = await _ensure_topic(session, chat)
    session.add(
        Notification(
            tg_id=client_tg_id,
            text=text,
            buttons=buttons,
            kind=KIND,
            chat_id=chat,
            thread_id=thread or None,
            topic_title="",
        )
    )
    return bool(thread)


async def say(session: AsyncSession, text: str) -> bool:
    """Короткое сообщение в топик, без кнопок: «вернулся», «включилась»."""
    chat = await order_topics.chat_id(session)
    thread = await thread_id(session) if chat else 0
    if not chat or not thread:
        return False
    session.add(
        Notification(
            tg_id=0, text=text, buttons=[], kind=KIND, chat_id=chat, thread_id=thread, topic_title=""
        )
    )
    return True
