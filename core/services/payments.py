"""Оркестрация платежей: создание, разбор уведомлений, проведение по бизнесу.

Идемпотентность обеспечивается тремя механизмами сразу, потому что PLATEGA
не поддерживает её на своей стороне:
  * `payments.idempotency_key` — уникальный индекс на нашу попытку оплаты;
  * `payments.provider_payment_id` — уникальный индекс на транзакцию провайдера,
    поэтому повторный колбэк попадает в ту же строку;
  * `payments.processed_at` — отметка, что бизнес-эффект уже применён;
    блокировка строки `FOR UPDATE` не даёт двум колбэкам пройти одновременно.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.config import settings
from core.dates import utcnow
from core.enums import (
    OrderItemType,
    OrderStatus,
    PaymentProviderName,
    PaymentPurpose,
    PaymentStatus,
    ReferralStatus,
    SubscriptionEventType,
)
from core.metrics import payments_total
from core.models import Order, Payment, Plan, Referral, Subscription, User
from core.payments import PaymentProvider, PaymentRequest, get_provider
from core.services import subscriptions as subscription_service

log = structlog.get_logger("services.payments")

PAYLOAD_PREFIX = "rs"


class PaymentError(Exception):
    """Ошибка оплаты, текст которой показывается клиенту."""


def build_payload(payment_id: int) -> str:
    """Маркер, который провайдер вернёт в колбэке (своего externalId у него нет)."""
    return f"{PAYLOAD_PREFIX}:{payment_id}"


def parse_payload(payload: str | None) -> int | None:
    if not payload or ":" not in payload:
        return None
    prefix, _, raw_id = payload.partition(":")
    if prefix != PAYLOAD_PREFIX or not raw_id.isdigit():
        return None
    return int(raw_id)


def receipt_items(order: Order | None, plan: Plan | None) -> list[dict[str, Any]]:
    """Состав чека по 54-ФЗ.

    PLATEGA не принимает чек в API, поэтому состав сохраняется в `payments.receipt`
    и передаётся в фискализацию отдельно (см. docs/decisions.md).
    """
    items: list[dict[str, Any]] = []
    if order is not None:
        for item in order.items:
            if item.item_type is OrderItemType.DELIVERY and item.total_price <= 0:
                continue
            items.append(
                {
                    "description": item.title[:128],
                    "quantity": item.quantity,
                    "amount": str(item.total_price),
                    "currency": order.currency,
                    "vat_code": str(item.vat_code),
                    "payment_subject": (
                        "commodity" if item.item_type is OrderItemType.PRODUCT else "service"
                    ),
                }
            )
    elif plan is not None:
        items.append(
            {
                "description": f"Подписка: {plan.title}"[:128],
                "quantity": 1,
                "amount": str(plan.price),
                "currency": settings.app.currency,
                "vat_code": str(plan.vat_code),
                "payment_subject": "service",
            }
        )
    return items


async def start_payment(
    session: AsyncSession,
    *,
    user: User,
    provider_name: PaymentProviderName,
    amount: Decimal,
    purpose: PaymentPurpose,
    description: str,
    order: Order | None = None,
    plan: Plan | None = None,
    subscription: Subscription | None = None,
    method: str | None = None,
    return_url: str | None = None,
    fail_url: str | None = None,
) -> Payment:
    """Создаёт платёж у провайдера и строку в `payments`."""
    if amount <= 0:
        raise PaymentError("Сумма платежа должна быть больше нуля")

    provider: PaymentProvider = get_provider(provider_name)
    if not provider.is_configured:
        raise PaymentError("Способ оплаты временно недоступен")

    payment = Payment(
        user_id=user.id,
        order_id=order.id if order else None,
        plan_id=plan.id if plan else None,
        subscription_id=subscription.id if subscription else None,
        provider=provider_name,
        purpose=purpose,
        status=PaymentStatus.PENDING,
        idempotency_key=uuid.uuid4().hex,
        amount=amount,
        currency=order.currency if order else settings.app.currency,
        description=description[:255],
        receipt={"items": receipt_items(order, plan)},
    )
    session.add(payment)
    await session.flush()

    bot_link = f"https://t.me/{settings.app.bot_username.lstrip('@')}"
    request = PaymentRequest(
        payment_id=payment.id,
        amount=amount,
        currency=payment.currency,
        description=payment.description,
        return_url=return_url or f"{bot_link}?start=order_{order.id}" if order else return_url or bot_link,
        fail_url=fail_url or bot_link,
        payload=build_payload(payment.id),
        user_tg_id=user.tg_id,
        user_name=f"@{user.username}" if user.username else user.display_name,
        method=method,
        receipt_items=payment.receipt.get("items", []),
    )

    result = await provider.create_payment(request)
    payment.provider_payment_id = result.provider_payment_id
    payment.confirmation_url = result.confirmation_url
    payment.expires_at = result.expires_at
    payment.status = result.status
    payment.raw_request = {"request": request.payload, "response": result.raw}
    payments_total.labels(provider=str(provider_name), status="created").inc()

    if order is not None and order.status is OrderStatus.NEW:
        order.status = OrderStatus.AWAITING_PAYMENT

    log.info(
        "payment.started",
        payment_id=payment.id,
        provider=str(provider_name),
        order_id=order.id if order else None,
        amount=str(amount),
    )
    return payment


async def _lock_payment(session: AsyncSession, payment_id: int) -> Payment | None:
    """Блокировка строки: два одновременных колбэка не проведут платёж дважды."""
    return await session.scalar(select(Payment).where(Payment.id == payment_id).with_for_update())


async def find_payment_for_webhook(
    session: AsyncSession,
    *,
    provider_payment_id: str,
    payload: str | None,
) -> Payment | None:
    payment = await session.scalar(select(Payment).where(Payment.provider_payment_id == provider_payment_id))
    if payment is not None:
        return await _lock_payment(session, payment.id)

    payment_id = parse_payload(payload)
    if payment_id is None:
        return None
    payment = await _lock_payment(session, payment_id)
    if payment is not None and payment.provider_payment_id is None:
        payment.provider_payment_id = provider_payment_id
    return payment


async def apply_status(
    session: AsyncSession,
    payment: Payment,
    *,
    status: PaymentStatus,
    raw: dict[str, Any] | None = None,
    amount: Decimal | None = None,
) -> bool:
    """Применяет новый статус платежа. True — бизнес-эффект был применён сейчас.

    Повторный вызов с тем же успешным статусом ничего не делает: защита от
    двойного продления при ретраях провайдера.
    """
    if raw is not None:
        payment.raw_webhook = raw

    if payment.processed_at is not None and payment.status is PaymentStatus.SUCCEEDED:
        log.info("payment.duplicate_notification", payment_id=payment.id, status=str(status))
        payments_total.labels(provider=str(payment.provider), status="duplicate").inc()
        return False

    if status is PaymentStatus.SUCCEEDED:
        if amount is not None and amount != payment.amount:
            # Сумма не сошлась — не зачисляем и зовём людей.
            payment.status = PaymentStatus.FAILED
            payment.error_message = f"Сумма уведомления {amount} не совпадает с {payment.amount}"
            log.error(
                "payment.amount_mismatch",
                payment_id=payment.id,
                expected=str(payment.amount),
                received=str(amount),
            )
            payments_total.labels(provider=str(payment.provider), status="amount_mismatch").inc()
            return False

        payment.status = PaymentStatus.SUCCEEDED
        payment.paid_at = payment.paid_at or utcnow()
        await _apply_success(session, payment)
        payment.processed_at = utcnow()
        payments_total.labels(provider=str(payment.provider), status="succeeded").inc()
        return True

    payment.status = status
    if status is PaymentStatus.REFUNDED:
        payment.refunded_at = utcnow()
        payment.refunded_amount = payment.amount
    payments_total.labels(provider=str(payment.provider), status=str(status)).inc()
    log.info("payment.status_updated", payment_id=payment.id, status=str(status))
    return False


async def _apply_success(session: AsyncSession, payment: Payment) -> None:
    """Бизнес-эффект успешной оплаты: заказ, подписка, реферальный бонус."""
    order: Order | None = None
    if payment.order_id:
        order = await session.scalar(
            select(Order).where(Order.id == payment.order_id).options(selectinload(Order.items))
        )
    if order is not None and order.status in (OrderStatus.NEW, OrderStatus.AWAITING_PAYMENT):
        order.status = OrderStatus.PAID
        order.paid_at = payment.paid_at

    plan = await _resolve_plan(session, payment, order)
    if plan is not None:
        await _grant_subscription(session, payment=payment, plan=plan, order=order)

    await _reward_referrer(session, payment=payment, order=order)

    log.info(
        "payment.succeeded",
        payment_id=payment.id,
        order_id=payment.order_id,
        amount=str(payment.amount),
    )


async def _resolve_plan(session: AsyncSession, payment: Payment, order: Order | None) -> Plan | None:
    if payment.plan_id:
        return await session.get(Plan, payment.plan_id)
    if order is None:
        return None
    for item in order.items:
        if item.item_type is OrderItemType.PLAN and item.plan_id:
            return await session.get(Plan, item.plan_id)
    return None


async def _grant_subscription(
    session: AsyncSession,
    *,
    payment: Payment,
    plan: Plan,
    order: Order | None,
) -> Subscription:
    """Продлевает действующую подписку или создаёт новую в ожидании активации."""
    existing = await subscription_service.get_current(session, payment.user_id)
    if existing is not None:
        subscription_service.extend(existing, plan=plan, payment_id=payment.id)
        payment.subscription_id = existing.id
        return existing

    subscription = await subscription_service.create_pending(
        session,
        user_id=payment.user_id,
        plan=plan,
        order_id=order.id if order else None,
        payment_id=payment.id,
        source="order" if order else "subscription",
    )
    await session.flush()
    payment.subscription_id = subscription.id
    return subscription


async def _reward_referrer(session: AsyncSession, *, payment: Payment, order: Order | None) -> None:
    """Бонус пригласившему — один раз, после первой успешной оплаты приглашённого."""
    referral = await session.scalar(
        select(Referral).where(
            Referral.referred_id == payment.user_id,
            Referral.status == ReferralStatus.PENDING,
        )
    )
    if referral is None:
        return

    bonus_days = settings.subscription.referral_bonus_days
    referral.status = ReferralStatus.REWARDED
    referral.reward_days = bonus_days
    referral.rewarded_at = utcnow()
    referral.order_id = order.id if order else None

    referrer_subscription = await subscription_service.get_active(session, referral.referrer_id)
    if referrer_subscription is not None:
        subscription_service.add_days(
            referrer_subscription,
            bonus_days,
            event=SubscriptionEventType.BONUS,
            comment=f"Реферальный бонус за пользователя {payment.user_id}",
        )
    else:
        referrer = await session.get(User, referral.referrer_id)
        if referrer is not None:
            referrer.bonus_days += bonus_days

    log.info(
        "referral.rewarded",
        referrer_id=referral.referrer_id,
        referred_id=payment.user_id,
        days=bonus_days,
    )


async def handle_webhook(
    session: AsyncSession,
    *,
    provider_name: PaymentProviderName,
    data: dict[str, Any],
) -> tuple[Payment | None, bool]:
    """Разбирает уведомление и проводит платёж. Возвращает (платёж, применён ли эффект)."""
    provider = get_provider(provider_name)
    parsed = provider.parse_webhook(data)

    payment = await find_payment_for_webhook(
        session,
        provider_payment_id=parsed.provider_payment_id,
        payload=parsed.payload,
    )
    if payment is None:
        log.warning(
            "payment.webhook_unknown",
            provider=str(provider_name),
            provider_payment_id=parsed.provider_payment_id,
        )
        payments_total.labels(provider=str(provider_name), status="unknown").inc()
        return None, False

    applied = await apply_status(
        session,
        payment,
        status=parsed.status,
        raw=data,
        amount=parsed.amount,
    )
    return payment, applied


async def sync_pending_payment(session: AsyncSession, payment: Payment) -> bool:
    """Опрос статуса у провайдера — страховка на случай потерянного колбэка."""
    if payment.provider_payment_id is None:
        return False
    provider = get_provider(payment.provider)
    if not provider.is_configured:
        return False

    result = await provider.check_status(payment.provider_payment_id)
    if result.status is payment.status:
        return False

    locked = await _lock_payment(session, payment.id)
    if locked is None:
        return False
    return await apply_status(session, locked, status=result.status, amount=result.amount)


async def expire_stale_payments(session: AsyncSession, *, now: dt.datetime | None = None) -> int:
    """Платежи с истёкшей ссылкой переводим в canceled, чтобы не висели вечно."""
    moment = now or utcnow()
    stale = await session.scalars(
        select(Payment).where(
            Payment.status == PaymentStatus.PENDING,
            Payment.expires_at.is_not(None),
            Payment.expires_at < moment,
        )
    )
    count = 0
    for payment in stale:
        payment.status = PaymentStatus.CANCELED
        payment.error_message = "Истёк срок действия платёжной ссылки"
        count += 1
    return count
