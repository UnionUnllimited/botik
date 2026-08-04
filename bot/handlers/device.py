"""Раздел «Мой роутер»: показания устройства и состояние подписки.

Данные берём из того, что уже собрано о роутере: телеметрию снимает воркер
через туннель, статус подключения — дашборд frps. Пока устройства не ходят
в наше API сами (этап 3), привязка делается из админки при отгрузке — на этом
экране это не видно, клиенту важен результат, а не способ.
"""

from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import inline
from bot.states import ActivationFlow
from bot.texts import ru
from bot.utils import screen
from core.config import settings
from core.dates import days_left, format_date_ru, utcnow
from core.enums import DeviceServiceStatus, DeviceStatus, SubscriptionStatus
from core.models import Device, Subscription, User
from core.services import activation
from core.services import subscriptions as subscription_service

router = Router(name="device")
log = structlog.get_logger("bot.device")


async def _device_of(session: AsyncSession, user_id: int) -> Device | None:
    """Роутер клиента. Отвязанные и заблокированные не показываем."""
    return await session.scalar(
        select(Device)
        .where(
            Device.user_id == user_id,
            Device.status.notin_([DeviceStatus.REVOKED, DeviceStatus.BLOCKED]),
        )
        .order_by(Device.activated_at.desc().nulls_last(), Device.id.desc())
        .limit(1)
    )


def _subscription_line(subscription: Subscription | None) -> str:
    if subscription is None:
        return ru.DEVICE_SUB_NONE

    plan = subscription.plan.title if subscription.plan else "—"
    if subscription.status is SubscriptionStatus.PENDING:
        return ru.DEVICE_SUB_PENDING.format(plan=plan)

    subscription_service.refresh_status(subscription)
    if subscription.status is SubscriptionStatus.GRACE:
        deadline = subscription.grace_until or subscription.expires_at
        remaining = max(days_left(deadline), 0) if deadline else 0
        return ru.DEVICE_SUB_GRACE.format(days=ru.days_phrase(remaining))
    if subscription.status is not SubscriptionStatus.ACTIVE or subscription.expires_at is None:
        return ru.DEVICE_SUB_EXPIRED

    return ru.DEVICE_SUB_ACTIVE.format(
        plan=plan,
        until=format_date_ru(subscription.expires_at),
        days=ru.days_phrase(max(days_left(subscription.expires_at), 0)),
    )


async def render(event: Message | CallbackQuery, session: AsyncSession, user: User) -> None:
    device = await _device_of(session, user.id)
    subscription = await subscription_service.get_current(session, user.id)
    paid_but_waiting = subscription is not None and subscription.status is SubscriptionStatus.PENDING

    if device is None or device.activated_at is None:
        # Подписка оплачена, но отсчёт не начат — значит роутер ещё не включали.
        # Предлагаем активацию даже без привязанного устройства: MAC клиент
        # видит на наклейке, а привязку мог не успеть сделать логист.
        if paid_but_waiting:
            text, can_activate = ru.DEVICE_READY_TO_ACTIVATE, True
        elif device is not None:
            text, can_activate = ru.DEVICE_WAITING, False
        else:
            text, can_activate = ru.DEVICE_NONE, False
        await screen.show(
            event,
            text,
            markup=inline.device_actions(
                has_device=False,
                has_subscription=subscription is not None,
                can_activate=can_activate,
            ),
        )
        return

    now = utcnow()
    online = device.frp_online or device.is_online(
        threshold_min=settings.subscription.heartbeat_offline_min, now=now
    )
    seen_at = (device.last_heartbeat_at, device.last_poll_at, device.frp_last_seen_at)
    last_seen = max((value for value in seen_at if value), default=None)

    await screen.show(
        event,
        ru.device_card(
            title=device.model or "Роутер",
            mac=device.mac,
            online=online,
            last_seen=last_seen,
            service_ok=device.service_status is DeviceServiceStatus.RUNNING or device.tunnel_running,
            uptime_sec=device.uptime_sec,
            clients_wifi=device.clients_wifi,
            clients_dhcp=device.clients_dhcp,
            cpu_pct=device.cpu_pct,
            ram_pct=device.ram_pct,
            rx_bytes=device.rx_bytes,
            tx_bytes=device.tx_bytes,
            subscription_line=_subscription_line(subscription),
        ),
        markup=inline.device_actions(has_device=True, has_subscription=subscription is not None),
    )


@router.callback_query(inline.MenuCB.filter(F.section == "device"))
async def open_device(
    callback: CallbackQuery, session: AsyncSession, user: User, state: FSMContext
) -> None:
    await callback.answer()
    await state.clear()
    await render(callback, session, user)


@router.message(F.text == ru.BTN_MY_DEVICE)
async def show_device(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    """Вход с прежней reply-клавиатуры — она ещё висит у старых клиентов."""
    await state.clear()
    await render(message, session, user)


# ------------------------------------------------------------------ активация


@router.callback_query(inline.NavCB.filter(F.action == "activate"))
async def ask_mac(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ActivationFlow.mac)
    await screen.show(callback, ru.ACTIVATION_ASK_MAC, markup=inline.waiting_for_text())


@router.message(ActivationFlow.mac, F.text)
async def submit_mac(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    """Настройка идёт до минуты, поэтому сначала показываем, что процесс пошёл."""
    await screen.show(message, ru.ACTIVATION_WORKING)

    try:
        device = await activation.activate(session, user=user, raw_mac=message.text or "")
    except activation.ActivationError as exc:
        await screen.show(
            message,
            ru.ACTIVATION_FAILED.format(reason=exc),
            markup=inline.activation_retry(),
            drop_incoming=False,
        )
        return

    await state.clear()
    subscription = await subscription_service.get_current(session, user.id)
    expires_at = subscription.expires_at if subscription else None
    plan = subscription.plan.title if subscription and subscription.plan else "—"
    until = format_date_ru(expires_at) if expires_at else "—"
    days = ru.days_phrase(max(days_left(expires_at), 0)) if expires_at else "—"

    await screen.show(
        message,
        ru.activation_done(model=device.model or "Роутер", plan=plan, until=until, days=days),
        markup=inline.device_actions(has_device=True, has_subscription=True),
        drop_incoming=False,
    )
