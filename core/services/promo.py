"""Промокоды: проверка и расчёт скидки."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.enums import OrderStatus, PromoDiscountType
from core.models import Order, PromoCode, PromoUsage

MONEY = Decimal("0.01")


class PromoError(Exception):
    """Промокод не подходит. Текст сообщения показывается клиенту."""


@dataclass(frozen=True, slots=True)
class PromoResult:
    promo: PromoCode
    discount: Decimal


def normalize_code(raw: str) -> str:
    return "".join(raw.split()).upper()[:32]


def _round(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def calculate_discount(promo: PromoCode, amount: Decimal) -> Decimal:
    """Скидка никогда не превышает сумму заказа."""
    if promo.discount_type is PromoDiscountType.PERCENT:
        discount = amount * promo.value / Decimal("100")
    else:
        discount = promo.value
    return _round(min(discount, amount))


async def validate(
    session: AsyncSession,
    *,
    code: str,
    user_id: int,
    amount: Decimal,
    product_id: int | None = None,
    plan_id: int | None = None,
    now: dt.datetime | None = None,
) -> PromoResult:
    """Проверяет промокод по всем ограничениям и возвращает размер скидки."""
    normalized = normalize_code(code)
    if not normalized:
        raise PromoError("Введите промокод")

    promo = await session.scalar(select(PromoCode).where(PromoCode.code == normalized))
    if promo is None or not promo.is_active:
        raise PromoError("Промокод не найден")

    moment = now or dt.datetime.now(dt.UTC)
    if promo.valid_from and promo.valid_from > moment:
        raise PromoError("Промокод ещё не действует")
    if promo.valid_until and promo.valid_until < moment:
        raise PromoError("Срок действия промокода истёк")

    if promo.max_uses and promo.used_count >= promo.max_uses:
        raise PromoError("Промокод уже использован максимальное число раз")

    if promo.min_amount and amount < promo.min_amount:
        raise PromoError(f"Промокод действует при заказе от {promo.min_amount:.0f} ₽")

    if promo.product_id and promo.product_id != product_id:
        raise PromoError("Промокод не действует на этот товар")
    if promo.plan_id and promo.plan_id != plan_id:
        raise PromoError("Промокод не действует на этот тариф")

    if promo.per_user_limit:
        used_by_user = await session.scalar(
            select(func.count())
            .select_from(PromoUsage)
            .where(PromoUsage.promo_code_id == promo.id, PromoUsage.user_id == user_id)
        )
        if (used_by_user or 0) >= promo.per_user_limit:
            raise PromoError("Вы уже использовали этот промокод")

    if promo.new_clients_only:
        paid_orders = await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.user_id == user_id,
                Order.status.notin_([OrderStatus.NEW, OrderStatus.AWAITING_PAYMENT, OrderStatus.CANCELLED]),
            )
        )
        if paid_orders:
            raise PromoError("Промокод только для первого заказа")

    discount = calculate_discount(promo, amount)
    if discount <= 0:
        raise PromoError("Промокод не даёт скидку на этот заказ")
    return PromoResult(promo=promo, discount=discount)


async def register_usage(
    session: AsyncSession,
    *,
    promo: PromoCode,
    user_id: int,
    order_id: int,
    discount: Decimal,
) -> None:
    """Фиксирует применение промокода. Уникальный индекс не даст задвоить по заказу."""
    session.add(
        PromoUsage(
            promo_code_id=promo.id,
            user_id=user_id,
            order_id=order_id,
            amount_discounted=discount,
        )
    )
    promo.used_count += 1
