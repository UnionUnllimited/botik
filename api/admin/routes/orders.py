"""Заказы: список с фильтрами, карточка, смена статуса, отгрузка, экспорт."""

from __future__ import annotations

import csv
import io

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.admin import audit
from api.admin.auth import Principal, form_value, require_section, verify_csrf
from api.admin.templating import render, status_label
from api.deps import get_session, get_transaction
from core.dates import to_display, utcnow
from core.enums import DeviceStatus, OrderStatus
from core.models import Device, Order, Payment, User
from core.security import normalize_mac
from core.services import delivery as delivery_service
from core.services import orders as order_service
from core.services.notifier import notify_order_status

router = APIRouter(prefix="/orders")
log = structlog.get_logger("admin.orders")

PAGE_SIZE = 30


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def order_list(
    request: Request,
    principal: Principal = Depends(require_section("orders")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    status_filter = request.query_params.get("status", "").strip()
    query_text = request.query_params.get("q", "").strip()
    page = max(int(request.query_params.get("page", "1") or 1), 1)

    query = select(Order).options(selectinload(Order.user), selectinload(Order.delivery))
    counter = select(func.count()).select_from(Order)

    if status_filter:
        query = query.where(Order.status == status_filter)
        counter = counter.where(Order.status == status_filter)
    if query_text:
        pattern = f"%{query_text}%"
        condition = or_(
            Order.public_number.ilike(pattern),
            Order.customer_name.ilike(pattern),
            Order.customer_phone.ilike(pattern),
            Order.customer_city.ilike(pattern),
        )
        query = query.where(condition)
        counter = counter.where(condition)

    total = await session.scalar(counter) or 0
    orders = list(
        await session.scalars(query.order_by(Order.id.desc()).limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE))
    )

    return render(
        request,
        "orders.html",
        principal,
        orders=orders,
        statuses=list(OrderStatus),
        status_filter=status_filter,
        query_text=query_text,
        page=page,
        pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        total=total,
    )


@router.get("/export", include_in_schema=False)
async def export_orders(
    request: Request,
    principal: Principal = Depends(require_section("orders")),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """CSV для бухгалтерии и печати накладных."""
    status_filter = request.query_params.get("status", "").strip()
    query = select(Order).options(selectinload(Order.delivery)).order_by(Order.id.desc())
    if status_filter:
        query = query.where(Order.status == status_filter)
    orders = list(await session.scalars(query.limit(5000)))

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        [
            "Номер",
            "Дата",
            "Статус",
            "Клиент",
            "Телефон",
            "Город",
            "Доставка",
            "Адрес/ПВЗ",
            "Трек",
            "Сумма",
            "Оплата",
        ]
    )
    for order in orders:
        delivery = order.delivery
        writer.writerow(
            [
                order.public_number,
                f"{to_display(order.created_at):%d.%m.%Y %H:%M}",
                status_label(order.status),
                order.customer_name,
                order.customer_phone,
                order.customer_city,
                delivery.method if delivery else "",
                (delivery.pvz_address or delivery.address or "") if delivery else "",
                (delivery.tracking_number or "") if delivery else "",
                f"{order.total:.2f}",
                "при получении" if order.is_cod else "онлайн",
            ]
        )

    audit.record(
        session,
        admin_id=principal.admin.id,
        action="orders.export",
        entity_type="order",
        new={"count": len(orders), "status": status_filter or "все"},
        request=request,
    )
    buffer.seek(0)
    filename = f"orders-{utcnow():%Y%m%d-%H%M}.csv"
    return StreamingResponse(
        iter([buffer.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{order_id}", response_class=HTMLResponse, include_in_schema=False, response_model=None)
async def order_card(
    order_id: int,
    request: Request,
    principal: Principal = Depends(require_section("orders")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    order = await order_service.get_order(session, order_id)
    if order is None:
        return RedirectResponse("/admin/orders?err=Заказ+не+найден", status_code=303)

    payments = list(
        await session.scalars(select(Payment).where(Payment.order_id == order.id).order_by(Payment.id.desc()))
    )
    devices = list(await session.scalars(select(Device).where(Device.order_id == order.id)))
    free_devices = list(
        await session.scalars(
            select(Device)
            .where(Device.status == DeviceStatus.NEW, Device.order_id.is_(None))
            .order_by(Device.id)
            .limit(50)
        )
    )

    return render(
        request,
        "order.html",
        principal,
        order=order,
        payments=payments,
        devices=devices,
        free_devices=free_devices,
        next_statuses=[item for item in OrderStatus if order_service.can_transition(order.status, item)],
        delivery_text=order_service.delivery_summary(order.delivery),
    )


@router.post("/{order_id}/status", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def change_status(
    order_id: int,
    request: Request,
    principal: Principal = Depends(require_section("orders")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    order = await order_service.get_order(session, order_id)
    if order is None:
        return RedirectResponse("/admin/orders?err=Заказ+не+найден", status_code=303)

    form = await request.form()
    target = form_value(form, "status")
    reason = form_value(form, "reason") or None
    notify = form_value(form, "notify") == "on"

    try:
        previous = order.status
        order_service.set_status(order, OrderStatus(target), reason=reason)
    except (order_service.OrderError, ValueError) as exc:
        return RedirectResponse(f"/admin/orders/{order_id}?err={exc}", status_code=303)

    audit.record(
        session,
        admin_id=principal.admin.id,
        action="order.status_changed",
        entity_type="order",
        entity_id=order.id,
        old={"status": str(previous)},
        new={"status": str(order.status), "reason": reason},
        request=request,
    )
    await session.flush()

    if notify:
        await notify_order_status(session, order, reason=reason)

    return RedirectResponse(f"/admin/orders/{order_id}?ok=Статус+обновлён", status_code=303)


@router.post("/{order_id}/shipping", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def set_tracking(
    order_id: int,
    request: Request,
    principal: Principal = Depends(require_section("orders")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    order = await order_service.get_order(session, order_id)
    if order is None or order.delivery is None:
        return RedirectResponse("/admin/orders?err=Нет+данных+доставки", status_code=303)

    form = await request.form()
    track = form_value(form, "tracking_number")
    old = order.delivery.tracking_number

    order.delivery.tracking_number = track or None
    order.delivery.tracking_url = delivery_service.tracking_url(order.delivery.method, track)

    audit.record(
        session,
        admin_id=principal.admin.id,
        action="order.tracking_set",
        entity_type="order",
        entity_id=order.id,
        old={"tracking_number": old},
        new={"tracking_number": track},
        request=request,
    )
    return RedirectResponse(f"/admin/orders/{order_id}?ok=Трек-номер+сохранён", status_code=303)


@router.post("/{order_id}/device", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def attach_device(
    order_id: int,
    request: Request,
    principal: Principal = Depends(require_section("orders")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    """Привязка MAC к заказу при отгрузке — по нему клиент активирует роутер."""
    order = await order_service.get_order(session, order_id)
    if order is None:
        return RedirectResponse("/admin/orders?err=Заказ+не+найден", status_code=303)

    form = await request.form()
    mac = normalize_mac(form_value(form, "mac"))
    if not mac:
        return RedirectResponse(f"/admin/orders/{order_id}?err=Некорректный+MAC", status_code=303)

    device = await session.scalar(select(Device).where(Device.mac == mac))
    if device is None:
        device = Device(mac=mac, model=form_value(form, "model"), status=DeviceStatus.NEW)
        session.add(device)
        await session.flush()
    elif device.order_id and device.order_id != order.id:
        return RedirectResponse(
            f"/admin/orders/{order_id}?err=MAC+уже+привязан+к+другому+заказу", status_code=303
        )

    old_state = {"order_id": device.order_id, "user_id": device.user_id, "status": str(device.status)}
    device.order_id = order.id
    device.user_id = order.user_id
    if device.status is DeviceStatus.NEW:
        device.status = DeviceStatus.ASSIGNED

    audit.record(
        session,
        admin_id=principal.admin.id,
        action="order.device_attached",
        entity_type="device",
        entity_id=device.id,
        old=old_state,
        new={"order_id": order.id, "user_id": order.user_id, "mac": mac},
        request=request,
    )
    log.info("admin.device_attached", order_id=order.id, mac=mac)
    return RedirectResponse(f"/admin/orders/{order_id}?ok=Устройство+привязано", status_code=303)


@router.post("/{order_id}/note", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def save_note(
    order_id: int,
    request: Request,
    principal: Principal = Depends(require_section("orders")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    order = await session.get(Order, order_id)
    if order is None:
        return RedirectResponse("/admin/orders?err=Заказ+не+найден", status_code=303)
    form = await request.form()
    order.admin_note = form_value(form, "admin_note") or None
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="order.note_saved",
        entity_type="order",
        entity_id=order.id,
        request=request,
    )
    return RedirectResponse(f"/admin/orders/{order_id}?ok=Заметка+сохранена", status_code=303)


@router.get("/by-user/{user_id}", include_in_schema=False)
async def orders_of_user(
    user_id: int,
    principal: Principal = Depends(require_section("orders")),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    user = await session.get(User, user_id)
    query = user.phone if user and user.phone else str(user_id)
    return RedirectResponse(f"/admin/orders?q={query}", status_code=303)
