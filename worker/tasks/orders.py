"""Заказы: напоминания о неоплаченной доставке и уборка брошенных.

Цену доставки называет оператор после оформления, а клиент оплачивает её
вторым счётом. Между «выставили» и «оплатил» заказ стоит собранный и никуда
не едет — и единственное, что тут можно сделать, это вовремя напомнить.

Решение заказчика от 21 августа 2026: напоминать и ждать. Заказ не отменяем
и деньги за роутер не возвращаем — кому нужно, тот напишет в поддержку.
Это про доставку: роутер там уже оплачен, и отменять нечего.

Заказ, за который не заплатили вовсе, — случай обратный: он держит роутер
на витрине и не может быть оплачен. Такой убираем, см. ниже.
"""

from __future__ import annotations

import datetime as dt

import structlog
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from core import texts as ru
from core.config import settings
from core.dates import ensure_utc, utcnow
from core.db import session_scope
from core.enums import OrderStatus, PaymentStatus
from core.models import Delivery, Order, Payment, User
from core.notifications import send_message
from core.services import order_topics
from core.services import orders as order_service
from core.services import promo as promo_service

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


ABANDONED_BATCH = 200
"""За круг убираем столько. Круг частый, а очередь брошенных заказов
длинная бывает ровно один раз — когда задачу включили впервые."""

# Платежи, при которых заказ отменять нельзя.
#
# Первые три — заказ ещё могут оплатить или уже оплатили.
#
# `FAILED` здесь не «карта не прошла»: этот статус ставится ровно в одном
# месте — когда деньги от провайдера пришли, а сумма оказалась меньше
# выставленной. Платёж не зачислен, оператор уже позван, и деньги лежат
# у провайдера. Закрыть такой заказ молча значило бы убрать его из виду
# как раз тогда, когда с ним нужно разбираться.
_KEEPS_THE_ORDER = (
    PaymentStatus.PENDING,
    PaymentStatus.WAITING_FOR_CAPTURE,
    PaymentStatus.SUCCEEDED,
    PaymentStatus.FAILED,
)


async def cancel_abandoned_orders() -> int:
    """Заказ, за который так и не заплатили, возвращает роутер на полку.

    Остаток считается по живым заказам — списывать его нельзя, иначе забытый
    возврат тихо съедает склад. Но и брошенная корзина держала роутер вечно:
    ссылка на оплату гасла, заказ оставался «ждёт оплаты», и витрина писала
    «нет в наличии», пока роутеры лежали на складе.

    Оплатить такой заказ клиент уже не может: новой ссылки к старому заказу
    не выдаётся, а старая мертва. Держать за ним роутер не за что.

    Клиенту не пишем. Сообщение «мы отменили ваш заказ» через несколько
    часов после того, как он сам передумал, — новость ни о чём; а тому, кто
    не передумал, оно приходит ровно тогда, когда он уже оформил заново.
    Оператору карточка в топике уходит: ему это видеть нужно.
    """
    now = utcnow()
    cutoff = now - dt.timedelta(hours=max(settings.order.abandoned_after_hours, 1))
    cancelled = 0

    async with session_scope() as session:
        live = select(Payment.id).where(
            Payment.order_id == Order.id,
            Payment.status.in_(_KEEPS_THE_ORDER),
        )
        rows = list(
            await session.scalars(
                select(Order)
                .where(
                    Order.status.in_((OrderStatus.NEW, OrderStatus.AWAITING_PAYMENT)),
                    # Наложенный платёж ждёт не ссылки, а перевозчика: деньги
                    # приходят при вручении, и «не оплачен» тут нормальное
                    # состояние на всю дорогу до клиента.
                    Order.is_cod.is_(False),
                    Order.created_at < cutoff,
                    ~live.exists(),
                )
                .options(
                    selectinload(Order.user),
                    selectinload(Order.delivery),
                    # Карточка в топике перебирает строки заказа: без них
                    # `push` полез бы в базу за ними по ходу и упал.
                    selectinload(Order.items),
                )
                .order_by(Order.id)
                .limit(ABANDONED_BATCH)
            )
        )

        for order in rows:
            order_service.set_status(
                order,
                OrderStatus.CANCELLED,
                reason="Не оплачен: срок платёжной ссылки истёк",
            )
            # Промокод возвращается клиенту вместе с роутером: он им не
            # воспользовался.
            await promo_service.release_usage(session, order_id=order.id)
            note = "↻ Отменён автоматически: не оплачен"
            # Роутер к неоплаченному заказу привязывают редко — но если
            # оператор успел, он остаётся за клиентом и после отмены.
            note += await order_topics.router_still_running(session, order)
            await order_topics.push(session, order, note=note)
            cancelled += 1
            log.info(
                "order.abandoned_cancelled",
                order_id=order.id,
                number=order.public_number,
                age_hours=round(
                    (now - ensure_utc(order.created_at)).total_seconds() / 3600, 1
                )
                if order.created_at
                else None,
            )

    if cancelled:
        log.info("orders.abandoned_cancelled", count=cancelled)
    return cancelled
