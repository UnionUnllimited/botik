"""Уведомления клиенту о событиях заказа и оплаты.

Тексты и кнопки берутся из `core`, а не из пакета бота: бот сменный, а оплату
подтверждать надо в любом случае. Раньше зависимость шла в `bot.texts`,
и удаление бота обрушило бы вебхук оплаты вместе с ним.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core import texts as ru
from core import tg_buttons
from core.enums import OrderStatus
from core.models import Order, Payment, User
from core.notifications import notify_admins, send_message
from core.services import delivery as delivery_service
from core.services import settings_service

log = structlog.get_logger("services.notifier")


async def notify_payment_result(session: AsyncSession, payment: Payment) -> None:
    """Сообщение клиенту об успешной оплате и алерт в админ-канал."""
    user = await session.get(User, payment.user_id)
    if user is None:
        return

    order: Order | None = None
    if payment.order_id:
        order = await session.scalar(
            select(Order).where(Order.id == payment.order_id).options(selectinload(Order.items))
        )

    shipping_days = await settings_service.get_str(session, "order.shipping_days")
    has_device = bool(order and any(item.product_id for item in order.items))

    await send_message(
        user.tg_id,
        ru.payment_success(
            number=order.public_number if order else f"#{payment.id}",
            total=ru.money(payment.amount),
            shipping_days=shipping_days,
            has_device=has_device,
        ),
        session=session,
    )
    await notify_admins(
        ru.ADMIN_PAYMENT_OK.format(
            number=order.public_number if order else f"платёж #{payment.id}",
            total=ru.money(payment.amount),
            customer=user.display_name,
        )
    )


async def notify_order_status(
    session: AsyncSession,
    order: Order,
    *,
    reason: str | None = None,
) -> bool:
    """Сообщение клиенту при смене статуса заказа админом."""
    template = ru.ORDER_STATUS_TEXTS.get(order.status)
    if template is None:
        return False

    user = await session.get(User, order.user_id)
    if user is None:
        return False

    text = template.format(number=order.public_number, reason=reason or "")
    markup = None

    if order.status is OrderStatus.SHIPPED and order.delivery is not None:
        track = order.delivery.tracking_number or ""
        if track:
            text += "\n\n" + ru.TRACK_INFO.format(track=track)
            url = order.delivery.tracking_url or delivery_service.tracking_url(order.delivery.method, track)
            markup = tg_buttons.tracking(url)

    return await send_message(user.tg_id, text.strip(), reply_markup=markup, session=session)


async def notify_amount_mismatch(payment: Payment, received: str) -> None:
    await notify_admins(
        ru.ADMIN_PAYMENT_MISMATCH.format(
            payment_id=payment.id,
            expected=ru.money(payment.amount),
            received=received,
        )
    )
