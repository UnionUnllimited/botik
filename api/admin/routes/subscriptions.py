"""Подписки: список по состояниям и массовые операции."""

from __future__ import annotations

import datetime as dt

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.admin import audit
from api.admin.auth import Principal, form_value, require_section, verify_csrf
from api.admin.templating import render
from api.deps import get_session, get_transaction
from core.dates import utcnow
from core.enums import SubscriptionEventType, SubscriptionStatus
from core.models import Subscription
from core.services import subscriptions as subscription_service

router = APIRouter(prefix="/subscriptions")
log = structlog.get_logger("admin.subscriptions")

PAGE_SIZE = 40

FILTERS = {
    "active": "активные",
    "expiring": "истекают за 7 дней",
    "grace": "льготный период",
    "expired": "истёкшие",
    "pending": "ждут активации",
    "all": "все",
}


def _apply_filter(query, name: str, now: dt.datetime):
    match name:
        case "expiring":
            return query.where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at > now,
                Subscription.expires_at <= now + dt.timedelta(days=7),
            )
        case "grace":
            return query.where(Subscription.status == SubscriptionStatus.GRACE)
        case "expired":
            return query.where(Subscription.status == SubscriptionStatus.EXPIRED)
        case "pending":
            return query.where(Subscription.status == SubscriptionStatus.PENDING)
        case "all":
            return query
        case _:
            return query.where(Subscription.status == SubscriptionStatus.ACTIVE)


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def subscription_list(
    request: Request,
    principal: Principal = Depends(require_section("subscriptions")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    name = request.query_params.get("filter", "active")
    page = max(int(request.query_params.get("page", "1") or 1), 1)
    now = utcnow()

    query = _apply_filter(
        select(Subscription).options(selectinload(Subscription.user), selectinload(Subscription.plan)),
        name,
        now,
    )
    counter = _apply_filter(select(func.count()).select_from(Subscription), name, now)

    total = await session.scalar(counter) or 0
    items = list(
        await session.scalars(
            query.order_by(Subscription.expires_at.asc().nulls_last())
            .limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)
        )
    )

    return render(
        request,
        "subscriptions.html",
        principal,
        items=items,
        filters=FILTERS,
        active_filter=name,
        page=page,
        pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        total=total,
    )


@router.post("/bulk", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def bulk_add_days(
    request: Request,
    principal: Principal = Depends(require_section("subscriptions")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    """Массовое продление — компенсация всем после аварии."""
    form = await request.form()
    name = form_value(form, "filter", "active")
    comment = form_value(form, "comment") or "Массовая компенсация"
    try:
        days = int(form_value(form, "days", "0"))
    except ValueError:
        days = 0

    if days <= 0 or days > 60:
        return RedirectResponse(
            f"/admin/subscriptions?filter={name}&err=Укажите+от+1+до+60+дней", status_code=303
        )

    now = utcnow()
    query = _apply_filter(select(Subscription), name, now)
    items = list(await session.scalars(query))

    for subscription in items:
        subscription_service.add_days(
            subscription,
            days,
            event=SubscriptionEventType.MANUAL_ADJUST,
            admin_id=principal.admin.id,
            comment=comment,
            now=now,
        )

    audit.record(
        session,
        admin_id=principal.admin.id,
        action="subscription.bulk_adjust",
        entity_type="subscription",
        new={"filter": name, "days": days, "count": len(items), "comment": comment},
        request=request,
    )
    log.info("admin.subscriptions.bulk", filter=name, days=days, count=len(items))
    return RedirectResponse(
        f"/admin/subscriptions?filter={name}&ok=Изменено+подписок:+{len(items)}", status_code=303
    )
