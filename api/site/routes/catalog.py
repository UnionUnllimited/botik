"""Витрина: список роутеров и карточка модели. Открыты без входа."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from api.site.auth import Client, optional_client
from api.site.templating import render
from core.models import Plan, Product

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
async def storefront(
    request: Request,
    session: AsyncSession = Depends(get_session),
    client: Client | None = Depends(optional_client),
) -> HTMLResponse:
    products = list(
        await session.scalars(
            select(Product)
            .where(Product.is_active.is_(True))
            .order_by(Product.sort_order, Product.id)
        )
    )
    plans = list(
        await session.scalars(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.months)
        )
    )
    return render(request, "index.html", client, products=products, plans=plans)


@router.get("/catalog/{slug}", response_class=HTMLResponse)
async def product_card(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    client: Client | None = Depends(optional_client),
) -> HTMLResponse:
    product = await session.scalar(
        select(Product).where(Product.slug == slug, Product.is_active.is_(True))
    )
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Такой модели нет")
    return render(request, "product.html", client, product=product)
