"""Разделы «Подписка» и «Пригласить друга»."""

from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import inline
from bot.states import OrderFlow
from bot.texts import ru
from bot.utils import screen
from bot.utils.deeplink import referral_link
from core.config import settings
from core.dates import days_left, format_date_ru
from core.enums import ReferralStatus, SubscriptionStatus
from core.models import Plan, Referral, User
from core.services import subscriptions as subscription_service

router = Router(name="subscription")
log = structlog.get_logger("bot.subscription")

Event = Message | CallbackQuery


@router.callback_query(inline.MenuCB.filter(F.section == "subscription"))
async def open_subscription(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await callback.answer()
    await render_subscription(callback, session, user)


@router.callback_query(inline.NavCB.filter(F.action == "subscription"))
async def back_to_subscription(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await callback.answer()
    await render_subscription(callback, session, user)


@router.message(F.text == ru.BTN_SUBSCRIPTION)
async def subscription_by_text(message: Message, session: AsyncSession, user: User) -> None:
    """Вход с прежней reply-клавиатуры."""
    await screen.remove_reply_keyboard(message)
    await render_subscription(message, session, user)


async def render_subscription(event: Event, session: AsyncSession, user: User) -> None:
    subscription = await subscription_service.get_current(session, user.id)

    if subscription is None:
        await screen.show(
            event, ru.SUBSCRIPTION_NONE, markup=inline.subscription_actions(has_subscription=False)
        )
        return

    if subscription.status is SubscriptionStatus.PENDING:
        deadline = format_date_ru(subscription.pending_expires_at) if subscription.pending_expires_at else "—"
        await screen.show(
            event,
            ru.SUBSCRIPTION_PENDING.format(
                plan=subscription.plan.title if subscription.plan else "—",
                deadline=deadline,
            ),
            markup=inline.back_to_menu(),
        )
        return

    subscription_service.refresh_status(subscription)

    if subscription.status is SubscriptionStatus.EXPIRED or subscription.expires_at is None:
        await screen.show(
            event, ru.SUBSCRIPTION_EXPIRED, markup=inline.subscription_actions(has_subscription=True)
        )
        return

    in_grace = subscription.status is SubscriptionStatus.GRACE
    remaining = (
        days_left(subscription.grace_until or subscription.expires_at)
        if in_grace
        else days_left(subscription.expires_at)
    )
    await screen.show(
        event,
        ru.subscription_active(
            plan=subscription.plan.title if subscription.plan else "—",
            expires_at=subscription.expires_at,
            days=remaining,
            in_grace=in_grace,
        ),
        markup=inline.subscription_actions(has_subscription=True),
    )


@router.callback_query(inline.NavCB.filter(F.action == "buy_subscription"))
async def buy_subscription(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await callback.answer()
    plans = list(
        await session.scalars(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.months)
        )
    )
    if not plans:
        await screen.show(callback, ru.CATALOG_EMPTY, markup=inline.back_to_menu())
        return

    await state.clear()
    await state.set_state(OrderFlow.plan)
    await state.update_data(product_id=None)
    await screen.show(callback, ru.PLAN_TITLE, markup=inline.plans(plans, with_device=False))


@router.callback_query(inline.MenuCB.filter(F.section == "referral"))
async def open_referral(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await callback.answer()
    await render_referral(callback, session, user)


@router.message(F.text == ru.BTN_REFERRAL)
async def referral_by_text(message: Message, session: AsyncSession, user: User) -> None:
    await screen.remove_reply_keyboard(message)
    await render_referral(message, session, user)


async def render_referral(event: Event, session: AsyncSession, user: User) -> None:
    invited = await session.scalar(
        select(func.count()).select_from(Referral).where(Referral.referrer_id == user.id)
    )
    rewarded = await session.scalar(
        select(func.count())
        .select_from(Referral)
        .where(Referral.referrer_id == user.id, Referral.status == ReferralStatus.REWARDED)
    )
    link = referral_link(user.tg_id)
    await screen.show(
        event,
        ru.referral_text(
            link=link,
            invited=invited or 0,
            rewarded=rewarded or 0,
            bonus_days=settings.subscription.referral_bonus_days,
        ),
        markup=inline.referral_share(link),
    )
