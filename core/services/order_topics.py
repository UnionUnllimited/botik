"""Заказ в топике рабочего чата: карточка, кнопки и постановка в очередь.

Оператор работает с телефона. Веб-админка там открывается, но это форма,
в которую надо попасть пальцем, а потом ещё вернуться в список. Топик на заказ
решает это иначе: заказ приходит сам, вся работа — кнопками, ввод — обычным
ответом в чат.

Отправлять мы по-прежнему не умеем — токен только у бота. Поэтому карточка
кладётся в ту же очередь `notifications`, которую он забирает раз в десять
секунд, а он создаёт топик, шлёт сообщение и присылает номер топика обратно.

**Текст и кнопки собираются только здесь.** Их рисуют два места — первое
сообщение в топике и обновление после каждого нажатия, — и разъехавшись,
они показали бы оператору одну картину, а сделали другое.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.enums import OrderStatus
from core.models import Device, Notification, Order
from core.services import settings_service
from core.texts import DELIVERY_METHOD_TITLES, ORDER_STATUS_TITLES

log = structlog.get_logger("services.order_topics")

CHAT_SETTING = "orders.topic_chat_id"
"""Рабочий чат с топиками. Пусто — топики выключены целиком: заказы просто
не уезжают в Telegram, всё остальное работает как работало."""

KIND = "order_topic"

CALLBACK_PREFIX = "ord"
"""`ord:<заказ>:<действие>`. Telegram даёт на callback 64 байта, поэтому
действия короткие: длинное имя обрежется молча, и кнопка перестанет работать."""


def callback(order_id: int, action: str) -> str:
    return f"{CALLBACK_PREFIX}:{order_id}:{action}"


def _money(value: Decimal | None) -> str:
    return f"{Decimal(value or 0):.2f}".rstrip("0").rstrip(".") or "0"


def topic_title(order: Order) -> str:
    """Название топика: номер и город. По ним заказ ищут в списке топиков.

    Имя клиента не берём: тёзок в списке из сотни топиков не различить,
    а номер уникален. Город отвечает на «а это далеко?» без открытия.
    """
    city = order.customer_city or (order.delivery.city if order.delivery else "")
    return f"{order.public_number}{' · ' + city if city else ''}"[:128]


def card_text(order: Order) -> str:
    """Карточка заказа для топика. HTML — им бот и шлёт остальное."""
    status = ORDER_STATUS_TITLES.get(str(order.status), str(order.status))
    lines = [
        f"<b>{order.public_number}</b> · {status}",
        "",
        f"Покупатель: {order.customer_name or '—'}",
        f"Телефон: {order.customer_phone or '—'}",
    ]
    if order.user is not None and order.user.telegram_name:
        lines.append(f"Телеграм: {order.user.telegram_name}")

    lines.append("")
    for item in order.items:
        lines.append(f"• {item.title} — {_money(item.total_price)} ₽")
    if order.discount_total:
        lines.append(f"Скидка: −{_money(order.discount_total)} ₽")
    lines.append(f"<b>Итого: {_money(order.total)} ₽</b>")
    lines.append("Оплачен" if order.paid_at else "Не оплачен")

    delivery = order.delivery
    if delivery is not None:
        lines.append("")
        carrier = DELIVERY_METHOD_TITLES.get(str(delivery.method), str(delivery.method))
        lines.append(f"Доставка: {carrier}")
        if delivery.address:
            lines.append(f"Куда: {delivery.address}")
        # «Не названа» и «ноль» — разные вещи: доставку можно подарить,
        # и отличает их отметка, а не сумма.
        if delivery.quoted_at is None:
            lines.append("Цена доставки: <b>не названа</b>")
        else:
            paid = "оплачена" if delivery.paid_at else "ждёт оплаты"
            lines.append(f"Цена доставки: {_money(delivery.price)} ₽ — {paid}")

    lines.append("")
    track = order.delivery.tracking_number if order.delivery else ""
    lines.append(f"Трек-номер: {track or '—'}")
    if order.admin_note:
        lines.append(f"Заметка: {order.admin_note}")
    return "\n".join(lines)


def card_buttons(order: Order, *, has_device: bool = False) -> list[dict[str, str]]:
    """Кнопки под карточкой. Только то, что на этом шаге имеет смысл.

    Кнопка, которая ответит «сейчас нельзя», хуже отсутствующей: с телефона
    её нажимают вслепую, и отказ читается как поломка.
    """
    buttons: list[dict[str, str]] = []
    if order.paid_at and not has_device:
        buttons.append({"text": "◈ Привязать роутер", "data": callback(order.id, "mac")})
    # Трек-номеру негде лежать без доставки: он колонка в ней. Кнопка без
    # доставки отвечала бы «негде хранить» — с телефона её жмут вслепую,
    # и такой отказ читается как поломка.
    if order.delivery is not None and order.status not in (
        OrderStatus.CANCELLED,
        OrderStatus.REFUNDED,
    ):
        buttons.append({"text": "▤ Трек-номер", "data": callback(order.id, "track")})
    if order.delivery is not None and order.delivery.paid_at is None:
        buttons.append({"text": "₽ Цена доставки", "data": callback(order.id, "dlv")})
    buttons.append({"text": "↻ Статус", "data": callback(order.id, "status")})
    buttons.append({"text": "✎ Заметка", "data": callback(order.id, "note")})
    if order.user is not None and order.user.tg_id:
        # Сообщение уходит **от бота**, а не из личного аккаунта оператора.
        # Клиент разговаривает с ботом: письмо от незнакомого человека он
        # в лучшем случае не узнает, а оператор при этом светит свой аккаунт
        # каждому покупателю.
        buttons.append({"text": "✉ Написать клиенту", "data": callback(order.id, "dm")})
    buttons.append({"text": "⟳ Обновить", "data": callback(order.id, "card")})
    return buttons


async def has_device(session: AsyncSession, order: Order) -> bool:
    """Привязан ли к заказу роутер. Запросом, а не связью: устройство
    и заказ живут порознь, и роутер при удалении заказа возвращается на склад."""
    found = await session.scalar(
        select(func.count()).select_from(Device).where(Device.order_id == order.id)
    )
    return bool(found)


async def card(session: AsyncSession, order: Order) -> dict[str, Any]:
    """Готовая карточка: текст и кнопки. Один источник на оба места, где она
    рисуется, — первое сообщение в топике и обновление после нажатия."""
    return {
        "text": card_text(order),
        "buttons": card_buttons(order, has_device=await has_device(session, order)),
        # Кому писать, если оператор нажмёт «Написать клиенту». Номер, а не
        # @логин: логин клиент меняет, а бот шлёт по номеру.
        "client_tg_id": (order.user.tg_id or 0) if order.user else 0,
    }


async def chat_id(session: AsyncSession) -> int:
    """Куда слать. Ноль — возможность выключена."""
    raw = await settings_service.get_str(session, CHAT_SETTING)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


async def push(session: AsyncSession, order: Order, *, note: str = "") -> Notification | None:
    """Кладёт карточку заказа в очередь. Топик заведёт бот, если его ещё нет.

    `note` — строка сверху: «оплачен», «клиент отменил». Без неё в чате
    появляется одинаковая карточка, и понять, что изменилось, нельзя.

    Молча ничего не делает, если чат не задан: топики — возможность,
    а не обязанность, и заказ должен создаваться и без них.
    """
    target = await chat_id(session)
    if not target:
        return None

    body = card_text(order)
    buttons = card_buttons(order, has_device=await has_device(session, order))
    message = Notification(
        # Топик — не переписка с клиентом, но колонка обязательная: кладём
        # владельца заказа, по нему потом видно, чей это топик.
        tg_id=(order.user.tg_id or 0) if order.user else 0,
        text=f"{note}\n\n{body}" if note else body,
        buttons=buttons,
        kind=KIND,
        chat_id=target,
        thread_id=order.tg_topic_id,
        # Название нужно, только пока топика нет: создавать второй по тому же
        # заказу нельзя — переписка разъедется на две ветки.
        topic_title="" if order.tg_topic_id else topic_title(order),
        order_id=order.id,
    )
    session.add(message)
    log.info("order_topic.queued", order_id=order.id, thread_id=order.tg_topic_id)
    return message
