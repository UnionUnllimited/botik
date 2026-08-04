"""Клиенты: поиск, карточка, действия над подпиской и блокировка."""

from __future__ import annotations

import datetime as dt

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.admin import audit
from api.admin.auth import Principal, form_value, require_section, verify_csrf
from api.admin.templating import render
from api.deps import get_session, get_transaction
from core.config import settings
from core.dates import utcnow
from core.enums import SubscriptionEventType, SubscriptionStatus
from core.models import Device, Order, Payment, Plan, Referral, Subscription, Ticket, User
from core.notifications import send_message
from core.security import normalize_mac
from core.services import subscriptions as subscription_service
from core.services.stats import PAID_STATUSES as PAID_ORDER_STATUSES

router = APIRouter(prefix="/clients")
log = structlog.get_logger("admin.clients")

PAGE_SIZE = 30


LIVE_SUBSCRIPTION_STATUSES = (
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.GRACE,
    SubscriptionStatus.PENDING,
)

CLIENT_FILTERS = {
    "": "все",
    "expiring": "истекают за 7 дней",
    "grace": "льготный период",
    "pending": "ждут активации",
    "expired": "истёкшие",
    "none": "без подписки",
}


def _subscription_filter(query, name: str, now: dt.datetime):
    """Отбор клиентов по состоянию подписки.

    Раздел подписок жил отдельным списком, но поддержка всё равно шла
    от клиента: искала человека, а не строку подписки. Поэтому выборки
    переехали сюда фильтрами.
    """
    owners = select(Subscription.user_id)
    match name:
        case "expiring":
            condition = owners.where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at > now,
                Subscription.expires_at <= now + dt.timedelta(days=7),
            )
        case "grace":
            condition = owners.where(Subscription.status == SubscriptionStatus.GRACE)
        case "pending":
            condition = owners.where(Subscription.status == SubscriptionStatus.PENDING)
        case "expired":
            condition = owners.where(Subscription.status == SubscriptionStatus.EXPIRED)
        case "none":
            return query.where(User.id.notin_(owners))
        case _:
            return query
    return query.where(User.id.in_(condition))


async def _client_summaries(
    session: AsyncSession, users: list[User]
) -> tuple[dict[int, Subscription], dict[int, Device], dict[int, tuple[int, object]]]:
    """Подписка, роутер и оплаты для списка клиентов — тремя запросами, не в цикле."""
    ids = [user.id for user in users]
    if not ids:
        return {}, {}, {}

    subscriptions: dict[int, Subscription] = {}
    rows = await session.scalars(
        select(Subscription)
        .where(Subscription.user_id.in_(ids), Subscription.status.in_(LIVE_SUBSCRIPTION_STATUSES))
        .order_by(Subscription.expires_at.desc().nulls_last())
        .options(selectinload(Subscription.plan))
    )
    for item in rows:
        # Первая по порядку — самая поздняя, она и показывается в карточке.
        subscriptions.setdefault(item.user_id, item)

    devices: dict[int, Device] = {}
    for device in await session.scalars(
        select(Device).where(Device.user_id.in_(ids)).order_by(Device.id.desc())
    ):
        if device.user_id is not None:
            devices.setdefault(device.user_id, device)

    money_rows = await session.execute(
        select(Order.user_id, func.count(), func.coalesce(func.sum(Order.total), 0))
        .where(Order.user_id.in_(ids), Order.status.in_(PAID_ORDER_STATUSES))
        .group_by(Order.user_id)
    )
    spend = {row[0]: (row[1], row[2]) for row in money_rows}
    return subscriptions, devices, spend


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def client_list(
    request: Request,
    principal: Principal = Depends(require_section("clients")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    query_text = request.query_params.get("q", "").strip()
    active_filter = request.query_params.get("filter", "").strip()
    page = max(int(request.query_params.get("page", "1") or 1), 1)
    now = utcnow()

    query = _subscription_filter(select(User), active_filter, now)
    counter = _subscription_filter(select(func.count()).select_from(User), active_filter, now)

    if query_text:
        pattern = f"%{query_text}%"
        conditions = [
            User.full_name.ilike(pattern),
            User.username.ilike(pattern),
            User.phone.ilike(pattern),
            User.first_name.ilike(pattern),
        ]
        if query_text.isdigit():
            conditions.append(User.tg_id == int(query_text))
        # Поиск по MAC и номеру заказа — обычные запросы поддержки.
        mac = normalize_mac(query_text)
        if mac:
            conditions.append(
                User.id.in_(select(Device.user_id).where(Device.mac == mac, Device.user_id.is_not(None)))
            )
        conditions.append(User.id.in_(select(Order.user_id).where(Order.public_number.ilike(pattern))))
        condition = or_(*conditions)
        query = query.where(condition)
        counter = counter.where(condition)

    total = await session.scalar(counter) or 0
    users = list(
        await session.scalars(query.order_by(User.id.desc()).limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE))
    )
    subscriptions, devices, spend = await _client_summaries(session, users)

    return render(
        request,
        "clients.html",
        principal,
        users=users,
        subscriptions=subscriptions,
        devices=devices,
        spend=spend,
        filters=CLIENT_FILTERS,
        active_filter=active_filter,
        query_text=query_text,
        page=page,
        pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        total=total,
        online_threshold=settings.subscription.heartbeat_offline_min,
        now=now,
    )


@router.get("/{user_id}", response_class=HTMLResponse, include_in_schema=False, response_model=None)
async def client_card(
    user_id: int,
    request: Request,
    principal: Principal = Depends(require_section("clients")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    user = await session.get(User, user_id)
    if user is None:
        return RedirectResponse("/admin/clients?err=Клиент+не+найден", status_code=303)

    orders = list(
        await session.scalars(select(Order).where(Order.user_id == user_id).order_by(Order.id.desc()))
    )
    payments = list(
        await session.scalars(
            select(Payment).where(Payment.user_id == user_id).order_by(Payment.id.desc()).limit(20)
        )
    )
    devices = list(await session.scalars(select(Device).where(Device.user_id == user_id)))
    subscriptions = list(
        await session.scalars(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.id.desc())
            .options(selectinload(Subscription.plan))
        )
    )
    tickets = list(
        await session.scalars(
            select(Ticket).where(Ticket.user_id == user_id).order_by(Ticket.id.desc()).limit(10)
        )
    )
    referrals = list(await session.scalars(select(Referral).where(Referral.referrer_id == user_id)))
    plans = list(await session.scalars(select(Plan).order_by(Plan.sort_order, Plan.months)))

    return render(
        request,
        "client.html",
        principal,
        user=user,
        orders=orders,
        payments=payments,
        devices=devices,
        subscriptions=subscriptions,
        tickets=tickets,
        referrals=referrals,
        plans=plans,
        current_subscription=await subscription_service.get_current(session, user_id),
    )


@router.post("/{user_id}/block", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def toggle_block(
    user_id: int,
    request: Request,
    principal: Principal = Depends(require_section("clients")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    user = await session.get(User, user_id)
    if user is None:
        return RedirectResponse("/admin/clients?err=Клиент+не+найден", status_code=303)

    form = await request.form()
    reason = form_value(form, "reason") or None
    was_blocked = user.is_blocked
    user.is_blocked = not was_blocked
    user.block_reason = None if was_blocked else reason

    audit.record(
        session,
        admin_id=principal.admin.id,
        action="client.blocked" if user.is_blocked else "client.unblocked",
        entity_type="user",
        entity_id=user.id,
        old={"is_blocked": was_blocked},
        new={"is_blocked": user.is_blocked, "reason": reason},
        request=request,
    )
    log.info("admin.client_block", user_id=user.id, blocked=user.is_blocked)
    action = "заблокирован" if user.is_blocked else "разблокирован"
    return RedirectResponse(f"/admin/clients/{user_id}?ok=Клиент+{action}", status_code=303)


@router.post("/{user_id}/bonus", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def add_bonus_days(
    user_id: int,
    request: Request,
    principal: Principal = Depends(require_section("subscriptions")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    """Бонусные дни: компенсация за аварию или подарок."""
    user = await session.get(User, user_id)
    if user is None:
        return RedirectResponse("/admin/clients?err=Клиент+не+найден", status_code=303)

    form = await request.form()
    try:
        days = int(form_value(form, "days", "0"))
    except ValueError:
        days = 0
    comment = form_value(form, "comment") or "Начисление администратором"
    if days == 0:
        return RedirectResponse(f"/admin/clients/{user_id}?err=Укажите+число+дней", status_code=303)

    subscription = await subscription_service.get_current(session, user_id)
    if subscription is None or subscription.expires_at is None:
        # Подписки ещё нет — копим дни на аккаунте, применим при активации.
        before = user.bonus_days
        user.bonus_days = max(user.bonus_days + days, 0)
        audit.record(
            session,
            admin_id=principal.admin.id,
            action="client.bonus_days",
            entity_type="user",
            entity_id=user.id,
            old={"bonus_days": before},
            new={"bonus_days": user.bonus_days, "comment": comment},
            request=request,
        )
        return RedirectResponse(f"/admin/clients/{user_id}?ok=Дни+начислены+на+аккаунт", status_code=303)

    old_expires = subscription.expires_at
    subscription_service.add_days(
        subscription,
        days,
        event=SubscriptionEventType.MANUAL_ADJUST,
        admin_id=principal.admin.id,
        comment=comment,
    )
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="subscription.days_adjusted",
        entity_type="subscription",
        entity_id=subscription.id,
        old={"expires_at": old_expires},
        new={"expires_at": subscription.expires_at, "days": days, "comment": comment},
        request=request,
    )
    return RedirectResponse(f"/admin/clients/{user_id}?ok=Подписка+изменена", status_code=303)


@router.post("/{user_id}/extend", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def extend_subscription(
    user_id: int,
    request: Request,
    principal: Principal = Depends(require_section("subscriptions")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    """Продление тарифом без оплаты — компенсации и ручные продажи."""
    form = await request.form()
    plan_id = form_value(form, "plan_id")
    plan = await session.get(Plan, int(plan_id)) if plan_id.isdigit() else None
    if plan is None:
        return RedirectResponse(f"/admin/clients/{user_id}?err=Тариф+не+найден", status_code=303)

    subscription = await subscription_service.get_current(session, user_id)
    if subscription is None:
        subscription = await subscription_service.create_pending(
            session, user_id=user_id, plan=plan, source="manual"
        )
        await session.flush()
        message = "Подписка+создана"
    else:
        old_expires = subscription.expires_at
        subscription_service.extend(subscription, plan=plan, admin_id=principal.admin.id)
        audit.record(
            session,
            admin_id=principal.admin.id,
            action="subscription.extended_manually",
            entity_type="subscription",
            entity_id=subscription.id,
            old={"expires_at": old_expires},
            new={"expires_at": subscription.expires_at, "plan": plan.title},
            request=request,
        )
        message = "Подписка+продлена"

    return RedirectResponse(f"/admin/clients/{user_id}?ok={message}", status_code=303)


@router.post("/{user_id}/message", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def send_direct_message(
    user_id: int,
    request: Request,
    principal: Principal = Depends(require_section("clients")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    user = await session.get(User, user_id)
    if user is None:
        return RedirectResponse("/admin/clients?err=Клиент+не+найден", status_code=303)

    form = await request.form()
    text = form_value(form, "text")
    if not text:
        return RedirectResponse(f"/admin/clients/{user_id}?err=Пустое+сообщение", status_code=303)

    delivered = await send_message(user.tg_id, text, session=session)
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="client.message_sent",
        entity_type="user",
        entity_id=user.id,
        new={"text": text[:500], "delivered": delivered},
        request=request,
    )
    result = "Сообщение+отправлено" if delivered else "err=Не+доставлено:+бот+заблокирован"
    separator = "?ok=" if delivered else "?"
    return RedirectResponse(f"/admin/clients/{user_id}{separator}{result}", status_code=303)


@router.post("/{user_id}/note", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def save_note(
    user_id: int,
    request: Request,
    principal: Principal = Depends(require_section("clients")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    user = await session.get(User, user_id)
    if user is None:
        return RedirectResponse("/admin/clients?err=Клиент+не+найден", status_code=303)
    form = await request.form()
    user.admin_note = form_value(form, "admin_note") or None
    user.last_seen_at = user.last_seen_at or utcnow()
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="client.note_saved",
        entity_type="user",
        entity_id=user.id,
        request=request,
    )
    return RedirectResponse(f"/admin/clients/{user_id}?ok=Заметка+сохранена", status_code=303)
