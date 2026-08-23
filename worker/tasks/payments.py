"""Досмотр платежей: колбэк мог не дойти, ссылка могла протухнуть."""

from __future__ import annotations

import datetime as dt

import structlog
from sqlalchemy import select

from core.db import session_scope
from core.enums import PaymentStatus
from core.models import Payment
from core.services import payments as payment_service
from core.services.notifier import notify_payment_result

log = structlog.get_logger("worker.payments")

MIN_AGE = dt.timedelta(seconds=45)
"""Сколько платёж должен пожить, прежде чем спрашивать о нём провайдера.

Секунды, а не минуты: колбэк PLATEGA настроен на другого бота, и статус мы
узнаём только этим опросом. Каждая лишняя минута здесь — минута, которую
клиент смотрит на «ждёт оплаты» после того, как заплатил.

Совсем без задержки спрашивать незачем: ссылку только что выдали, клиент
ещё не дошёл до страницы оплаты."""

MAX_AGE = dt.timedelta(hours=24)
BATCH = 50


async def sync_pending_payments() -> int:
    """Опрашивает провайдера по всем висящим платежам.

    Нужен на случай потерянного уведомления: у PLATEGA три попытки с
    интервалом 5 минут, после этого колбэк теряется навсегда.
    """
    now = dt.datetime.now(dt.UTC)
    applied = 0
    async with session_scope() as session:
        pending = list(
            await session.scalars(
                select(Payment)
                .where(
                    Payment.status == PaymentStatus.PENDING,
                    Payment.provider_payment_id.is_not(None),
                    Payment.created_at < now - MIN_AGE,
                    Payment.created_at > now - MAX_AGE,
                )
                .order_by(Payment.id)
                .limit(BATCH)
            )
        )
        for payment in pending:
            try:
                changed = await payment_service.sync_pending_payment(session, payment)
            except Exception as exc:  # noqa: BLE001 — один платёж не должен ронять задачу
                log.warning("payments.sync_failed", payment_id=payment.id, error=str(exc))
                continue
            if changed and payment.status is PaymentStatus.SUCCEEDED:
                applied += 1
                await session.flush()
                await notify_payment_result(session, payment)

    if applied:
        log.info("payments.synced", confirmed=applied)
    return applied


async def expire_payments() -> int:
    async with session_scope() as session:
        count = await payment_service.expire_stale_payments(session)
    if count:
        log.info("payments.expired", count=count)
    return count
