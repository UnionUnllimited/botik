"""Покупка роутера: каталог → карточка → комплект → данные → доставка → оплата.

Каталог не вываливает карточки всех моделей подряд: сначала список кнопок,
карточка открывается по нажатию и заменяет собой список. Один экран на шаг,
переход правит его на месте — см. `bot.utils.screen`.

Исключение — сообщения, к которым клиент вернётся позже: ссылка на оплату
и номер заказа отправляются с `persist=True` и остаются в переписке.
"""

from __future__ import annotations

from dataclasses import fields

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import inline
from bot.keyboards.reply import request_phone
from bot.states import OrderFlow
from bot.texts import ru
from bot.utils import screen, validators
from core.enums import DeliveryMethod, PaymentProviderName, PaymentPurpose
from core.models import Plan, Product, User
from core.payments import online_provider
from core.services import delivery as delivery_service
from core.services import orders as order_service
from core.services import payments as payment_service
from core.services import promo as promo_service
from core.services import settings_service
from core.services.orders import OrderDraft, OrderError

router = Router(name="catalog")
log = structlog.get_logger("bot.catalog")

Event = Message | CallbackQuery


def _draft_from_state(data: dict) -> OrderDraft:
    """Собирает черновик из данных FSM: в состоянии хранятся только простые типы."""
    draft = OrderDraft()
    for item in fields(OrderDraft):
        if item.name in data:
            setattr(draft, item.name, data[item.name])
    if isinstance(draft.delivery_method, str):
        draft.delivery_method = DeliveryMethod(draft.delivery_method)
    return draft


async def _store(state: FSMContext, **values: object) -> None:
    await state.update_data(**values)


async def _active_plans(session: AsyncSession) -> list[Plan]:
    return list(
        await session.scalars(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.months)
        )
    )


# ------------------------------------------------------------------ каталог


@router.callback_query(inline.MenuCB.filter(F.section == "buy"))
async def open_catalog(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await callback.answer()
    await show_catalog(callback, session, state)


@router.callback_query(inline.NavCB.filter(F.action == "catalog"))
async def back_to_catalog(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await callback.answer()
    await show_catalog(callback, session, state)


@router.message(F.text == ru.BTN_BUY)
async def catalog_by_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    """Вход с прежней reply-клавиатуры."""
    await screen.remove_reply_keyboard(message)
    await show_catalog(message, session, state)


async def show_catalog(event: Event, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    products = list(
        await session.scalars(
            select(Product).where(Product.is_active.is_(True)).order_by(Product.sort_order, Product.id)
        )
    )
    if not products:
        await screen.show(event, ru.CATALOG_EMPTY, markup=inline.back_to_menu())
        return

    await state.set_state(OrderFlow.product)
    await screen.show(event, ru.CATALOG_TITLE, markup=inline.catalog(products))


@router.callback_query(inline.ProductCB.filter(F.action == "open"))
async def open_product(
    callback: CallbackQuery,
    callback_data: inline.ProductCB,
    session: AsyncSession,
) -> None:
    """Карточка модели вместо списка. Фото, если оно есть, — тем же экраном."""
    product = await session.get(Product, callback_data.product_id)
    if product is None or not product.is_active:
        await callback.answer(ru.OUT_OF_STOCK, show_alert=True)
        return

    await callback.answer()
    card = ru.product_card(
        title=product.title,
        subtitle=product.subtitle,
        description=product.description,
        price=product.price,
        specs=product.specs or {},
    )
    await screen.show(
        callback,
        card,
        markup=inline.product_card(product.id, in_stock=product.in_stock),
        photo=product.photo_file_id or None,
    )


@router.callback_query(inline.ProductCB.filter(F.action == "take"))
async def choose_product(
    callback: CallbackQuery,
    callback_data: inline.ProductCB,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    product = await session.get(Product, callback_data.product_id)
    if product is None or not product.is_active or not product.in_stock:
        await callback.answer(ru.OUT_OF_STOCK, show_alert=True)
        return

    await callback.answer()
    await _store(state, product_id=product.id)
    await state.set_state(OrderFlow.plan)
    await screen.show(
        callback,
        ru.PLAN_TITLE,
        markup=inline.plans(await _active_plans(session), with_device=True),
    )


@router.callback_query(inline.PlanCB.filter())
async def choose_plan(
    callback: CallbackQuery,
    callback_data: inline.PlanCB,
    session: AsyncSession,
    state: FSMContext,
    user: User,
) -> None:
    plan = await session.get(Plan, callback_data.plan_id)
    if plan is None or not plan.is_active:
        await callback.answer(ru.PLAN_GONE, show_alert=True)
        return

    await callback.answer()
    await _store(state, plan_id=plan.id)

    if not callback_data.with_device:
        # Покупка подписки без роутера — контакты и доставка не нужны.
        await _store(state, product_id=None)
        await state.set_state(OrderFlow.promo)
        await screen.show(callback, ru.ASK_PROMO, markup=inline.skip_promo())
        return

    await state.set_state(OrderFlow.name)
    prompt = ru.ASK_NAME
    if user.full_name:
        prompt += f"\n\nПрошлый раз: <code>{user.full_name}</code>"
    await screen.show(callback, prompt, markup=inline.waiting_for_text())


# ------------------------------------------------------------ данные клиента


@router.message(OrderFlow.name, F.text)
async def enter_name(message: Message, state: FSMContext, user: User) -> None:
    name = validators.clean_full_name(message.text or "")
    if not name:
        await screen.show(message, ru.ASK_NAME_INVALID, markup=inline.waiting_for_text())
        return
    await _store(state, customer_name=name)
    await state.set_state(OrderFlow.phone)

    prompt = ru.ASK_PHONE
    if user.phone:
        prompt += f"\n\nПрошлый раз: <code>{validators.format_phone(user.phone)}</code>"
    await screen.show(message, prompt, markup=inline.waiting_for_text())
    # Кнопка «отправить номер» живёт только на reply-клавиатуре, а её нельзя
    # приложить к экрану с инлайн-кнопками. Отдельное сообщение уйдёт следующим шагом.
    await screen.notify_with_keyboard(message, ru.ASK_PHONE_HINT, markup=request_phone())


@router.message(OrderFlow.phone, F.contact)
async def enter_phone_contact(message: Message, state: FSMContext) -> None:
    phone = validators.clean_phone(message.contact.phone_number if message.contact else "")
    if not phone:
        await screen.show(message, ru.ASK_PHONE_INVALID, markup=inline.waiting_for_text())
        return
    await _finish_phone(message, state, phone)


@router.message(OrderFlow.phone, F.text)
async def enter_phone_text(message: Message, state: FSMContext) -> None:
    phone = validators.clean_phone(message.text or "")
    if not phone:
        await screen.show(message, ru.ASK_PHONE_INVALID, markup=inline.waiting_for_text())
        return
    await _finish_phone(message, state, phone)


async def _finish_phone(message: Message, state: FSMContext, phone: str) -> None:
    await state.update_data(customer_phone=phone)
    await state.set_state(OrderFlow.city)
    await screen.remove_reply_keyboard(message)
    await screen.show(message, ru.ASK_CITY, markup=inline.waiting_for_text())


@router.message(OrderFlow.city, F.text)
async def enter_city(message: Message, session: AsyncSession, state: FSMContext) -> None:
    city = validators.clean_city(message.text or "")
    if not city:
        await screen.show(message, ru.ASK_CITY_INVALID, markup=inline.waiting_for_text())
        return
    await _store(state, customer_city=city)
    await state.set_state(OrderFlow.delivery_method)
    options = await delivery_service.get_options(session)
    await screen.show(message, ru.ASK_DELIVERY, markup=inline.delivery_methods(options))


# ---------------------------------------------------------------- доставка


@router.callback_query(OrderFlow.delivery_method, inline.DeliveryCB.filter())
async def choose_delivery(
    callback: CallbackQuery,
    callback_data: inline.DeliveryCB,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    method = DeliveryMethod(callback_data.method)
    option = await delivery_service.get_option(session, method)
    if option is None:
        await callback.answer(ru.DELIVERY_GONE, show_alert=True)
        return

    await callback.answer()
    await _store(state, delivery_method=method.value)
    await state.set_state(OrderFlow.delivery_target)
    await screen.show(
        callback,
        ru.ASK_DELIVERY_TARGET,
        markup=inline.delivery_targets(
            courier_price=option.courier_price, pvz_price=option.pvz_price
        ),
    )


@router.callback_query(OrderFlow.delivery_target, inline.DeliveryTargetCB.filter())
async def choose_delivery_target(
    callback: CallbackQuery,
    callback_data: inline.DeliveryTargetCB,
    state: FSMContext,
) -> None:
    await callback.answer()
    await _store(state, delivery_to_pvz=callback_data.to_pvz)
    await state.set_state(OrderFlow.delivery_address)
    await screen.show(
        callback,
        ru.ASK_PVZ if callback_data.to_pvz else ru.ASK_ADDRESS,
        markup=inline.waiting_for_text(),
    )


@router.message(OrderFlow.delivery_address, F.text)
async def enter_address(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    to_pvz = bool(data.get("delivery_to_pvz", True))
    raw = message.text or ""
    value = validators.clean_pvz(raw) if to_pvz else validators.clean_address(raw)
    if not value:
        await screen.show(
            message,
            ru.ASK_PVZ if to_pvz else ru.ASK_ADDRESS_INVALID,
            markup=inline.waiting_for_text(),
        )
        return
    if to_pvz:
        await _store(state, pvz_address=value, delivery_address="")
    else:
        await _store(state, delivery_address=value, pvz_address="")
    await _ask_promo(message, state)


# ----------------------------------------------------------------- промокод


async def _ask_promo(event: Event, state: FSMContext) -> None:
    await state.set_state(OrderFlow.promo)
    await screen.show(event, ru.ASK_PROMO, markup=inline.skip_promo())


@router.message(OrderFlow.promo, F.text)
async def enter_promo(message: Message, session: AsyncSession, state: FSMContext, user: User) -> None:
    data = await state.get_data()
    draft = _draft_from_state(data)
    draft.promo_code = message.text or ""
    try:
        totals = await order_service.calculate_totals(session, draft=draft, user_id=user.id)
    except promo_service.PromoError as exc:
        await screen.show(message, ru.PROMO_FAILED.format(reason=exc), markup=inline.skip_promo())
        return
    except OrderError as exc:
        await state.clear()
        await screen.show(message, str(exc), markup=inline.back_to_menu())
        return

    code = promo_service.normalize_code(draft.promo_code)
    await _store(state, promo_code=code)
    await _show_confirmation(message, session, state, user)
    await screen.notify(
        message, ru.PROMO_APPLIED.format(code=code, discount=ru.money(totals.discount))
    )


@router.callback_query(OrderFlow.promo, inline.NavCB.filter(F.action == "skip_promo"))
async def skip_promo(callback: CallbackQuery, session: AsyncSession, state: FSMContext, user: User) -> None:
    await callback.answer()
    await _store(state, promo_code="")
    await _show_confirmation(callback, session, state, user)


# ------------------------------------------------------------ подтверждение


async def _show_confirmation(event: Event, session: AsyncSession, state: FSMContext, user: User) -> None:
    data = await state.get_data()
    draft = _draft_from_state(data)
    try:
        totals = await order_service.calculate_totals(session, draft=draft, user_id=user.id)
    except (OrderError, promo_service.PromoError) as exc:
        await state.clear()
        await screen.show(event, str(exc), markup=inline.back_to_menu())
        return

    delivery_text = "—"
    if draft.delivery_method is not None:
        option = await delivery_service.get_option(session, draft.delivery_method)
        target = draft.pvz_address or draft.delivery_address or draft.customer_city
        delivery_text = f"{option.title if option else draft.delivery_method}, {target}"

    cod_enabled = await settings_service.get_bool(session, "order.cod_enabled")
    provider = online_provider()

    await state.set_state(OrderFlow.confirm)
    await screen.show(
        event,
        ru.order_summary(
            product_title=totals.product.title if totals.product else None,
            plan_title=totals.plan.title if totals.plan else None,
            subtotal=totals.subtotal,
            discount=totals.discount,
            delivery_price=totals.delivery,
            total=totals.total,
            name=draft.customer_name or user.display_name,
            phone=validators.format_phone(draft.customer_phone) if draft.customer_phone else "—",
            city=draft.customer_city,
            delivery_text=delivery_text,
        ),
        markup=inline.confirm_order(
            cod_enabled=cod_enabled and totals.product is not None,
            online_enabled=provider is not None,
        ),
    )


@router.callback_query(OrderFlow.confirm, inline.NavCB.filter(F.action.in_({"pay_online", "pay_cod"})))
async def submit_order(
    callback: CallbackQuery,
    callback_data: inline.NavCB,
    session: AsyncSession,
    state: FSMContext,
    user: User,
) -> None:
    await callback.answer()

    data = await state.get_data()
    draft = _draft_from_state(data)
    draft.is_cod = callback_data.action == "pay_cod"
    draft.utm_source = user.utm_source

    try:
        order = await order_service.create_order(session, user=user, draft=draft)
    except (OrderError, promo_service.PromoError) as exc:
        await state.clear()
        await screen.show(callback, str(exc), markup=inline.back_to_menu())
        return

    await session.flush()
    await state.clear()

    if draft.is_cod:
        # Деньги заберёт перевозчик — заказ сразу уходит в работу логисту.
        shipping_days = await settings_service.get_str(session, "order.shipping_days")
        await screen.show(
            callback,
            ru.ORDER_COD_CREATED.format(
                number=order.public_number,
                total=ru.money(order.total),
                shipping_days=shipping_days,
            ),
            markup=inline.back_to_menu(),
            persist=True,
        )
        await _notify_admins_new_order(session, order=order, payment_kind="при получении")
        return

    await _create_payment_and_reply(callback, session, user=user, order=order)


async def _create_payment_and_reply(event: Event, session: AsyncSession, *, user: User, order) -> None:
    try:
        payment = await payment_service.start_payment(
            session,
            user=user,
            provider_name=PaymentProviderName.PLATEGA,
            amount=order.total,
            purpose=PaymentPurpose.ORDER,
            description=f"Заказ {order.public_number}",
            order=order,
        )
    except Exception as exc:
        log.exception("payment.create_failed", order_id=order.id, error=str(exc))
        await screen.show(event, ru.PAYMENT_UNAVAILABLE, markup=inline.back_to_menu())
        return

    minutes = 15
    if payment.expires_at is not None and payment.created_at is not None:
        minutes = max(int((payment.expires_at - payment.created_at).total_seconds() // 60), 1)

    if not payment.confirmation_url:
        await screen.show(event, ru.PAYMENT_UNAVAILABLE, markup=inline.back_to_menu())
        return

    # Ссылку на оплату оставляем в переписке: клиент вернётся к ней позже.
    await screen.show(
        event,
        ru.PAYMENT_LINK.format(
            number=order.public_number,
            total=ru.money(order.total),
            minutes=minutes,
        ),
        markup=inline.payment_link(payment.confirmation_url, order.id),
        persist=True,
    )
    await _notify_admins_new_order(session, order=order, payment_kind="онлайн, ожидаем")


async def _notify_admins_new_order(session: AsyncSession, *, order, payment_kind: str) -> None:
    from core.notifications import notify_admins

    items = ", ".join(item.title for item in order.items) or "—"
    user_link = f"<a href='tg://user?id={order.user.tg_id}'>профиль</a>" if order.user else "—"
    await notify_admins(
        ru.ADMIN_NEW_ORDER.format(
            number=order.public_number,
            customer=order.customer_name or "—",
            user_link=user_link,
            items=items,
            total=ru.money(order.total),
            payment=payment_kind,
            delivery=order_service.delivery_summary(order.delivery),
        )
    )


# ---------------------------------------------------------- проверка оплаты


@router.callback_query(inline.OrderCB.filter(F.action == "check"))
async def check_payment(
    callback: CallbackQuery, callback_data: inline.OrderCB, session: AsyncSession
) -> None:
    from core.enums import PaymentStatus
    from core.models import Payment

    payment = await session.scalar(
        select(Payment).where(Payment.order_id == callback_data.order_id).order_by(Payment.id.desc()).limit(1)
    )
    if payment is None:
        await callback.answer(ru.PAYMENT_FAILED, show_alert=True)
        return

    if payment.status is PaymentStatus.PENDING:
        await payment_service.sync_pending_payment(session, payment)

    if payment.status is PaymentStatus.SUCCEEDED:
        await callback.answer(ru.PAYMENT_CONFIRMED, show_alert=True)
    elif payment.status is PaymentStatus.CANCELED:
        await callback.answer(ru.PAYMENT_EXPIRED, show_alert=True)
        await screen.show(
            callback,
            ru.PAYMENT_EXPIRED,
            markup=inline.retry_payment(callback_data.order_id),
            persist=True,
        )
    else:
        await callback.answer(ru.PAYMENT_PENDING, show_alert=True)


@router.callback_query(inline.OrderCB.filter(F.action == "repay"))
async def repay(
    callback: CallbackQuery,
    callback_data: inline.OrderCB,
    session: AsyncSession,
    user: User,
) -> None:
    await callback.answer()
    order = await order_service.get_order(session, callback_data.order_id)
    if order is None or order.user_id != user.id:
        return
    await _create_payment_and_reply(callback, session, user=user, order=order)


@router.callback_query(inline.NavCB.filter(F.action == "cancel"))
async def cancel_flow(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await screen.show(callback, ru.CANCELLED, markup=inline.main_menu())


@router.message(F.text == ru.BTN_CANCEL)
async def cancel_by_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    await screen.remove_reply_keyboard(message)
    await screen.show(message, ru.CANCELLED, markup=inline.main_menu())
