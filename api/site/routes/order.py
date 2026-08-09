"""Оформление заказа: выбор тарифа, контакты, доставка.

Оплаты здесь пока нет намеренно — заказ создаётся в статусе «новый», и клиент
видит его номер. Расчёт сумм, промокоды и доставка не переписывались: считает
всё тот же `core/services/orders.py`, что считал для бота.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session, get_transaction
from api.site.auth import Client, current_client, form_value, verify_csrf
from api.site.templating import render
from core.enums import DeliveryMethod
from core.models import Order, OrderItem, Plan, Product
from core.services import delivery as delivery_service
from core.services import orders as orders_service
from core.validators import clean_address, clean_city, clean_full_name, clean_phone, clean_pvz

log = structlog.get_logger("site.order")

router = APIRouter(include_in_schema=False)


async def _product_or_404(session: AsyncSession, slug: str) -> Product:
    product = await session.scalar(
        select(Product).where(Product.slug == slug, Product.is_active.is_(True))
    )
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Такой модели нет")
    return product


async def _checkout_page(
    request: Request,
    client: Client,
    session: AsyncSession,
    product: Product,
    *,
    error: str = "",
    values: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    plans = list(
        await session.scalars(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.months)
        )
    )
    options = await delivery_service.get_options(session)
    user = client.user

    filled = {
        "name": user.full_name or "",
        "phone": user.phone or "",
        "city": user.city or "",
        "address": "",
        "pvz": "",
        "promo": "",
        "plan_id": str(next((plan.id for plan in plans if plan.is_default), plans[0].id if plans else "")),
        "delivery": options[0].method.value if options else "",
        "target": "pvz",
    }
    filled.update(values or {})

    return render(
        request,
        "checkout.html",
        client,
        status_code=status_code,
        product=product,
        plans=plans,
        options=options,
        values=filled,
        error=error,
    )


@router.get("/order/{slug}", response_class=HTMLResponse)
async def checkout_form(
    slug: str,
    request: Request,
    client: Client = Depends(current_client),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    product = await _product_or_404(session, slug)
    return await _checkout_page(request, client, session, product)


@router.post("/order/{slug}", dependencies=[Depends(verify_csrf)], response_class=HTMLResponse)
async def checkout_submit(
    slug: str,
    request: Request,
    client: Client = Depends(current_client),
    session: AsyncSession = Depends(get_transaction),
) -> HTMLResponse:
    product = await _product_or_404(session, slug)
    form = await request.form()

    values = {
        "name": form_value(form, "name"),
        "phone": form_value(form, "phone"),
        "city": form_value(form, "city"),
        "address": form_value(form, "address"),
        "pvz": form_value(form, "pvz"),
        "promo": form_value(form, "promo"),
        "plan_id": form_value(form, "plan_id"),
        "delivery": form_value(form, "delivery"),
        "target": form_value(form, "target") or "pvz",
    }

    async def fail(message: str) -> HTMLResponse:
        return await _checkout_page(
            request, client, session, product, error=message, values=values, status_code=400
        )

    name = clean_full_name(values["name"])
    if not name:
        return await fail("Укажите фамилию и имя — минимум два слова, только буквы.")
    phone = clean_phone(values["phone"])
    if not phone:
        return await fail("Телефон не похож на российский номер. Пример: +7 900 123-45-67.")
    city = clean_city(values["city"])
    if not city:
        return await fail("Проверьте название города.")

    try:
        method = DeliveryMethod(values["delivery"])
    except ValueError:
        return await fail("Выберите способ доставки.")

    to_pvz = values["target"] != "courier"
    address, pvz = "", ""
    if to_pvz:
        pvz = clean_pvz(values["pvz"])
        if not pvz:
            return await fail("Укажите пункт выдачи — адрес или его номер.")
    else:
        address = clean_address(values["address"])
        if not address:
            return await fail("Адрес нужен с номером дома, иначе курьер не доедет.")

    draft = orders_service.OrderDraft(
        product_id=product.id,
        plan_id=int(values["plan_id"]) if values["plan_id"].isdigit() else None,
        customer_name=name,
        customer_phone=phone,
        customer_city=city,
        delivery_method=method,
        delivery_to_pvz=to_pvz,
        delivery_address=address,
        pvz_address=pvz,
        promo_code=values["promo"],
    )

    try:
        order = await orders_service.create_order(session, user=client.user, draft=draft)
    except orders_service.OrderError as exc:
        return await fail(str(exc))

    log.info("site.order.created", user_id=client.user.id, order=order.public_number)
    return render(
        request,
        "order_done.html",
        client,
        order=order,
        items=list(order.items),
    )


@router.get("/orders/{number}", response_class=HTMLResponse)
async def order_card(
    number: str,
    request: Request,
    client: Client = Depends(current_client),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Заказ по номеру. Чужой не покажем — номер угадать проще, чем кажется."""
    order = await session.scalar(
        select(Order).where(Order.public_number == number, Order.user_id == client.user.id)
    )
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заказ не найден")
    items = list(await session.scalars(select(OrderItem).where(OrderItem.order_id == order.id)))
    return render(request, "order_done.html", client, order=order, items=items)
