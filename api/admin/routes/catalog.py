"""Товары и тарифы: цены и тексты правятся без деплоя."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin import audit
from api.admin.auth import Principal, form_value, require_section, verify_csrf
from api.admin.templating import render
from api.deps import get_session, get_transaction
from core.enums import VatCode
from core.models import NodeGroup, Plan, Product
from core.services import media

router = APIRouter(prefix="/catalog")
log = structlog.get_logger("admin.catalog")


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


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def catalog_page(
    request: Request,
    principal: Principal = Depends(require_section("catalog")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    products = list(await session.scalars(select(Product).order_by(Product.sort_order, Product.id)))
    plans = list(await session.scalars(select(Plan).order_by(Plan.sort_order, Plan.months)))
    groups = list(await session.scalars(select(NodeGroup).order_by(NodeGroup.id)))
    return render(
        request,
        "catalog.html",
        principal,
        products=products,
        plans=plans,
        groups=groups,
        vat_codes=list(VatCode),
    )


@router.post("/products/{product_id}", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def save_product(
    product_id: int,
    request: Request,
    principal: Principal = Depends(require_section("catalog")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    """product_id = 0 — создание новой карточки."""
    form = await request.form()
    product = await session.get(Product, product_id) if product_id else None
    creating = product is None

    if creating:
        slug = form_value(form, "slug")
        if not slug:
            return RedirectResponse("/admin/catalog?err=Нужен+slug", status_code=303)
        exists = await session.scalar(select(Product).where(Product.slug == slug))
        if exists is not None:
            return RedirectResponse("/admin/catalog?err=Такой+slug+уже+есть", status_code=303)
        product = Product(slug=slug, title=form_value(form, "title") or slug, price=Decimal("0"))
        session.add(product)

    before = {
        "title": product.title,
        "price": product.price,
        "stock": product.stock,
        "is_active": product.is_active,
    }

    product.title = form_value(form, "title") or product.title
    product.subtitle = form_value(form, "subtitle")
    product.description = form_value(form, "description")
    product.model_code = form_value(form, "model_code")
    product.price = _decimal(form_value(form, "price"), str(product.price))
    old_price_raw = form_value(form, "old_price")
    product.old_price = _decimal(old_price_raw) if old_price_raw else None
    product.stock = _int(form_value(form, "stock"), product.stock)
    product.sort_order = _int(form_value(form, "sort_order"), product.sort_order)
    product.is_active = form_value(form, "is_active") == "on"
    product.allow_preorder = form_value(form, "allow_preorder") == "on"
    product.photo_file_id = form_value(form, "photo_file_id") or None

    # Картинка для витрины. Загруженный файл главнее ссылки: если админ выбрал
    # и то и другое, он явно только что выбрал файл.
    upload = form.get("photo")
    photo_url = form_value(form, "photo_url")
    if upload is not None and getattr(upload, "filename", ""):
        try:
            saved = media.save_image(
                await upload.read(),
                getattr(upload, "content_type", "") or "",
                prefix=f"product-{product.id or 'new'}",
            )
        except media.MediaError as exc:
            return RedirectResponse(f"/admin/catalog?err={quote(str(exc))}", status_code=303)
        media.delete_image(product.photo_url)
        product.photo_url = saved
    elif photo_url != (product.photo_url or ""):
        if not photo_url:
            media.delete_image(product.photo_url)
        product.photo_url = photo_url or None

    vat = form_value(form, "vat_code")
    if vat:
        product.vat_code = VatCode(vat)

    specs_raw = form_value(form, "specs")
    if specs_raw:
        try:
            parsed = json.loads(specs_raw)
            if isinstance(parsed, dict):
                product.specs = {str(k): str(v) for k, v in parsed.items()}
        except json.JSONDecodeError:
            return RedirectResponse("/admin/catalog?err=Характеристики:+неверный+JSON", status_code=303)

    await session.flush()
    after = {
        "title": product.title,
        "price": product.price,
        "stock": product.stock,
        "is_active": product.is_active,
    }
    old_changed, new_changed = audit.diff(before, after)
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="product.created" if creating else "product.updated",
        entity_type="product",
        entity_id=product.id,
        old=old_changed,
        new=new_changed,
        request=request,
    )
    return RedirectResponse("/admin/catalog?ok=Товар+сохранён", status_code=303)


@router.post("/plans/{plan_id}", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def save_plan(
    plan_id: int,
    request: Request,
    principal: Principal = Depends(require_section("catalog")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    form = await request.form()
    plan = await session.get(Plan, plan_id) if plan_id else None
    creating = plan is None

    if creating:
        slug = form_value(form, "slug")
        if not slug:
            return RedirectResponse("/admin/catalog?err=Нужен+slug", status_code=303)
        exists = await session.scalar(select(Plan).where(Plan.slug == slug))
        if exists is not None:
            return RedirectResponse("/admin/catalog?err=Такой+slug+уже+есть", status_code=303)
        plan = Plan(slug=slug, title=form_value(form, "title") or slug, months=1, price=Decimal("0"))
        session.add(plan)

    before = {"title": plan.title, "price": plan.price, "months": plan.months, "is_active": plan.is_active}

    plan.title = form_value(form, "title") or plan.title
    plan.description = form_value(form, "description")
    plan.months = _int(form_value(form, "months"), plan.months)
    plan.extra_days = _int(form_value(form, "extra_days"), plan.extra_days)
    plan.price = _decimal(form_value(form, "price"), str(plan.price))
    old_price_raw = form_value(form, "old_price")
    plan.old_price = _decimal(old_price_raw) if old_price_raw else None
    plan.discount_percent = _decimal(form_value(form, "discount_percent"), "0")
    plan.sort_order = _int(form_value(form, "sort_order"), plan.sort_order)
    plan.is_active = form_value(form, "is_active") == "on"
    plan.is_default = form_value(form, "is_default") == "on"
    group_id = form_value(form, "node_group_id")
    plan.node_group_id = int(group_id) if group_id.isdigit() else None
    vat = form_value(form, "vat_code")
    if vat:
        plan.vat_code = VatCode(vat)

    await session.flush()
    after = {"title": plan.title, "price": plan.price, "months": plan.months, "is_active": plan.is_active}
    old_changed, new_changed = audit.diff(before, after)
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="plan.created" if creating else "plan.updated",
        entity_type="plan",
        entity_id=plan.id,
        old=old_changed,
        new=new_changed,
        request=request,
    )
    return RedirectResponse("/admin/catalog?ok=Тариф+сохранён", status_code=303)
