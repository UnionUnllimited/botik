"""Промокоды и партии кодов активации."""

from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal, InvalidOperation

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin import audit
from api.admin.auth import Principal, form_value, require_section, verify_csrf
from api.admin.templating import render
from api.deps import get_session, get_transaction
from core.dates import utcnow
from core.enums import ActivationCodeStatus, PromoDiscountType
from core.models import ActivationCode, ActivationCodeBatch, Plan, PromoCode
from core.security import generate_activation_code
from core.services import promo as promo_service

router = APIRouter(prefix="/promo")
log = structlog.get_logger("admin.promo")

MAX_BATCH = 5000


def _decimal(raw: str, default: str = "0") -> Decimal:
    try:
        return Decimal(raw.replace(",", ".").strip() or default)
    except InvalidOperation:
        return Decimal(default)


def _int(raw: str, default: int = 0) -> int:
    try:
        return int(raw.strip() or default)
    except ValueError:
        return default


def _date(raw: str) -> dt.datetime | None:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw).replace(tzinfo=dt.UTC)
    except ValueError:
        return None


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def promo_page(
    request: Request,
    principal: Principal = Depends(require_section("promo")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    codes = list(await session.scalars(select(PromoCode).order_by(PromoCode.id.desc())))
    batches = list(await session.scalars(select(ActivationCodeBatch).order_by(ActivationCodeBatch.id.desc())))
    plans = list(await session.scalars(select(Plan).order_by(Plan.sort_order, Plan.months)))

    used_counts = dict(
        (
            await session.execute(
                select(ActivationCode.batch_id, func.count())
                .where(ActivationCode.status == ActivationCodeStatus.USED)
                .group_by(ActivationCode.batch_id)
            )
        ).all()
    )

    return render(
        request,
        "promo.html",
        principal,
        codes=codes,
        batches=batches,
        plans=plans,
        used_counts=used_counts,
        discount_types=list(PromoDiscountType),
    )


@router.post("/codes/{promo_id}", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def save_promo(
    promo_id: int,
    request: Request,
    principal: Principal = Depends(require_section("promo")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    form = await request.form()
    promo = await session.get(PromoCode, promo_id) if promo_id else None
    creating = promo is None

    if creating:
        code = promo_service.normalize_code(form_value(form, "code"))
        if not code:
            return RedirectResponse("/admin/promo?err=Введите+код", status_code=303)
        exists = await session.scalar(select(PromoCode).where(PromoCode.code == code))
        if exists is not None:
            return RedirectResponse("/admin/promo?err=Такой+промокод+уже+есть", status_code=303)
        promo = PromoCode(code=code, discount_type=PromoDiscountType.PERCENT, value=Decimal("0"))
        session.add(promo)

    before = {"value": promo.value, "is_active": promo.is_active, "max_uses": promo.max_uses}

    promo.description = form_value(form, "description")
    promo.discount_type = PromoDiscountType(form_value(form, "discount_type", "percent"))
    promo.value = _decimal(form_value(form, "value"), str(promo.value))
    promo.max_uses = _int(form_value(form, "max_uses"), promo.max_uses)
    promo.per_user_limit = _int(form_value(form, "per_user_limit"), promo.per_user_limit)
    promo.min_amount = _decimal(form_value(form, "min_amount"), "0")
    promo.valid_from = _date(form_value(form, "valid_from"))
    promo.valid_until = _date(form_value(form, "valid_until"))
    promo.new_clients_only = form_value(form, "new_clients_only") == "on"
    promo.is_active = form_value(form, "is_active") == "on"

    await session.flush()
    old_changed, new_changed = audit.diff(
        before, {"value": promo.value, "is_active": promo.is_active, "max_uses": promo.max_uses}
    )
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="promo.created" if creating else "promo.updated",
        entity_type="promo_code",
        entity_id=promo.id,
        old=old_changed,
        new=new_changed | {"code": promo.code},
        request=request,
    )
    return RedirectResponse("/admin/promo?ok=Промокод+сохранён", status_code=303)


@router.post("/batches", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def create_batch(
    request: Request,
    principal: Principal = Depends(require_section("promo")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    """Генерация партии кодов активации для печати на коробки."""
    form = await request.form()
    size = _int(form_value(form, "size"), 0)
    if size < 1 or size > MAX_BATCH:
        return RedirectResponse(f"/admin/promo?err=Размер+партии+1..{MAX_BATCH}", status_code=303)

    plan_id_raw = form_value(form, "plan_id")
    plan = await session.get(Plan, int(plan_id_raw)) if plan_id_raw.isdigit() else None

    batch = ActivationCodeBatch(
        title=form_value(form, "title") or f"Партия от {utcnow():%d.%m.%Y}",
        plan_id=plan.id if plan else None,
        months=plan.months if plan else _int(form_value(form, "months"), 1),
        extra_days=plan.extra_days if plan else 0,
        size=size,
        expires_at=_date(form_value(form, "expires_at")),
        created_by_admin_id=principal.admin.id,
        comment=form_value(form, "comment") or None,
    )
    session.add(batch)
    await session.flush()

    # Коллизия кода почти невероятна, но проверяем: код одноразовый и уникальный.
    existing = set((await session.scalars(select(ActivationCode.code))).all())
    created = 0
    while created < size:
        code = generate_activation_code()
        if code in existing:
            continue
        existing.add(code)
        session.add(
            ActivationCode(
                code=code,
                batch_id=batch.id,
                plan_id=batch.plan_id,
                months=batch.months,
                extra_days=batch.extra_days,
                status=ActivationCodeStatus.NEW,
                expires_at=batch.expires_at,
            )
        )
        created += 1

    audit.record(
        session,
        admin_id=principal.admin.id,
        action="activation_batch.created",
        entity_type="activation_code_batch",
        entity_id=batch.id,
        new={"size": size, "plan": plan.title if plan else None, "months": batch.months},
        request=request,
    )
    log.info("admin.activation_batch", batch_id=batch.id, size=size)
    return RedirectResponse(f"/admin/promo?ok=Партия+создана:+{size}+кодов", status_code=303)


@router.get("/batches/{batch_id}/export", include_in_schema=False, response_model=None)
async def export_batch(
    batch_id: int,
    request: Request,
    principal: Principal = Depends(require_section("promo")),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse | RedirectResponse:
    batch = await session.get(ActivationCodeBatch, batch_id)
    if batch is None:
        return RedirectResponse("/admin/promo?err=Партия+не+найдена", status_code=303)

    codes = list(
        await session.scalars(
            select(ActivationCode).where(ActivationCode.batch_id == batch_id).order_by(ActivationCode.id)
        )
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Код", "Срок, мес", "Действует до", "Статус"])
    for code in codes:
        writer.writerow(
            [
                code.code,
                code.months,
                f"{code.expires_at:%d.%m.%Y}" if code.expires_at else "бессрочно",
                code.status,
            ]
        )

    audit.record(
        session,
        admin_id=principal.admin.id,
        action="activation_batch.exported",
        entity_type="activation_code_batch",
        entity_id=batch_id,
        new={"count": len(codes)},
        request=request,
    )
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="codes-batch-{batch_id}.csv"'},
    )


@router.post("/batches/{batch_id}/revoke", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def revoke_batch(
    batch_id: int,
    request: Request,
    principal: Principal = Depends(require_section("promo")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    """Партия потерялась или ушла не туда — гасим все неиспользованные коды."""
    codes = list(
        await session.scalars(
            select(ActivationCode).where(
                ActivationCode.batch_id == batch_id,
                ActivationCode.status.in_([ActivationCodeStatus.NEW, ActivationCodeStatus.ISSUED]),
            )
        )
    )
    for code in codes:
        code.status = ActivationCodeStatus.REVOKED

    audit.record(
        session,
        admin_id=principal.admin.id,
        action="activation_batch.revoked",
        entity_type="activation_code_batch",
        entity_id=batch_id,
        new={"revoked": len(codes)},
        request=request,
    )
    return RedirectResponse(f"/admin/promo?ok=Погашено+кодов:+{len(codes)}", status_code=303)
