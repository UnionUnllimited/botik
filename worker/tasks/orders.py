"""Заказы: напоминания о неоплаченной доставке.

Цену доставки называет оператор после оформления, а клиент оплачивает её
вторым счётом. Между «выставили» и «оплатил» заказ стоит собранный и никуда
не едет — и единственное, что тут можно сделать, это вовремя напомнить.

Решение заказчика от 21 августа 2026: напоминать и ждать. Заказ не отменяем
и деньги за роутер не возвращаем — кому нужно, тот напишет в поддержку.
"""

from __future__ import annotations

import datetime as dt

import structlog
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from core import texts as ru
from core.dates import utcnow
from core.db import session_scope
from core.models import Delivery, Order, User
from core.notifications import send_message

log = structlog.get_logger("worker.orders")

REMIND_AFTER_DAYS = (1, 3, 7)
"""На какой день после выставления счёта напоминать.

Три раза и хватит: дальше это уже не напоминание, а навязчивость, а заказ
всё равно разбирает оператор глазами — он видит его в списке с пометкой."""


async def remind_unpaid_delivery() -> int:
    """Напоминает про неоплаченный счёт на доставку."""
    now = utcnow()
    sent = 0

    async with session_scope() as session:
        rows = await session.scalars(
            select(Delivery)
            .where(
                # Цену назвали, деньги не пришли, и платить есть за что:
                # подаренная доставка помечается оплаченной сразу.
                Delivery.quoted_at.is_not(None),
                Delivery.paid_at.is_(None),
                Delivery.price > 0,
            )
            .options(selectinload(Delivery.order).selectinload(Order.user))
        )
        for delivery in rows:
            waiting = (now - _aware(delivery.quoted_at)).days
            marker = max((day for day in REMIND_AFTER_DAYS if day <= waiting), default=None)
            if marker is None or delivery.reminded_day == marker:
                continue

            order = delivery.order
            user: User | None = order.user if order else None
            if user is None or user.bot_blocked or user.is_blocked:
                # Отметку ставим всё равно: иначе круг будет спотыкаться
                # об этот заказ каждые сутки до скончания века.
                delivery.reminded_day = marker
                continue

            delivered = await send_message(
                user.tg_id,
                ru.DELIVERY_REMINDER.format(
                    number=order.public_number, price=ru.money(delivery.price)
                ),
                session=session,
                kind="delivery_reminder",
            )
            delivery.reminded_day = marker
            if delivered:
                sent += 1

    if sent:
        log.info("orders.delivery_reminded", sent=sent)
    return sent


def _aware(moment: dt.datetime) -> dt.datetime:
    """Время из базы приходит с зоной, но у старых строк её может не быть."""
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)
