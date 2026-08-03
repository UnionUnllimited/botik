"""Покупка роутера: каталог → комплект → данные → доставка → промокод → оплата."""

from __future__ import annotations

from dataclasses import fields

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import inline
from bot.keyboards.reply import REMOVE, main_menu
from bot.states import OrderFlow
from bot.texts import ru
from bot.utils import validators
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


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=ru.BTN_SHARE_PHONE, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# ------------------------------------------------------------------ каталог


@router.message(F.text == ru.BTN_BUY)
async def show_catalog(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    products = list(
        await session.scalars(
            select(Product).where(Product.is_active.is_(True)).order_by(Product.sort_order, Product.id)
        )
    )
    if not products:
        await message.answer(ru.CATALOG_EMPTY, reply_markup=main_menu())
        return

    await state.set_state(OrderFlow.product)
    await message.answer(ru.CATALOG_TITLE, reply_markup=main_menu())
    for product in products:
        card = ru.product_card(
            title=product.title,
            subtitle=product.subtitle,
            description=product.description,
            price=product.price,
            specs=product.specs or {},
        )
        markup = inline.catalog([product])
        if product.photo_file_id:
            await message.answer_photo(product.photo_file_id, caption=card, reply_markup=markup)
        else:
            await message.answer(card, reply_markup=markup)


@router.callback_query(inline.NavCB.filter(F.action == "catalog"))
async def back_to_catalog(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await callback.answer()
    if callback.message is not None:
        await show_catalog(callback.message, session, state)


@router.callback_query(inline.ProductCB.filter())
async def choose_product(
    callback: CallbackQuery,
    callback_data: inline.ProductCB,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    product = await session.get(Product, callback_data.product_id)
    if product is None or not product.is_active:
        await callback.answer(ru.OUT_OF_STOCK, show_alert=True)
        return
    if not product.in_stock:
        await callback.answer(ru.OUT_OF_STOCK, show_alert=True)
        return

    await callback.answer()
    await _store(state, product_id=product.id)
    await state.set_state(OrderFlow.plan)

    plans = list(
        await session.scalars(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.months)
        )
    )
    if callback.message is not None:
        await callback.message.answer(ru.PLAN_TITLE, reply_markup=inline.plans(plans, with_device=True))


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
        await callback.answer("Тариф больше не доступен", show_alert=True)
        return

    await callback.answer()
    await _store(state, plan_id=plan.id)

    if not callback_data.with_device:
        # Покупка подписки без роутера — контакты и доставка не нужны.
        await _store(state, product_id=None)
        await state.set_state(OrderFlow.promo)
        if callback.message is not None:
            await callback.message.answer(ru.ASK_PROMO, reply_markup=inline.skip_promo())
        return

    await state.set_state(OrderFlow.name)
    if callback.message is not None:
        prompt = ru.ASK_NAME
        if user.full_name:
            prompt += f"\n\nПрошлый раз: <code>{user.full_name}</code>"
        await callback.message.answer(prompt, reply_markup=REMOVE)


# ------------------------------------------------------------ данные клиента


@router.message(OrderFlow.name, F.text)
async def enter_name(message: Message, state: FSMContext, user: User) -> None:
    name = validators.clean_full_name(message.text or "")
    if not name:
        await message.answer(ru.ASK_NAME_INVALID)
        return
    await _store(state, customer_name=name)
    await state.set_state(OrderFlow.phone)
    prompt = ru.ASK_PHONE
    if user.phone:
        prompt += f"\n\nПрошлый раз: <code>{validators.format_phone(user.phone)}</code>"
    await message.answer(prompt, reply_markup=phone_keyboard())


@router.message(OrderFlow.phone, F.contact)
async def enter_phone_contact(message: Message, state: FSMContext) -> None:
    phone = validators.clean_phone(message.contact.phone_number if message.contact else "")
    if not phone:
        await message.answer(ru.ASK_PHONE_INVALID)
        return
    await _finish_phone(message, state, phone)


@router.message(OrderFlow.phone, F.text)
async def enter_phone_text(message: Message, state: FSMContext) -> None:
    phone = validators.clean_phone(message.text or "")
    if not phone:
        await message.answer(ru.ASK_PHONE_INVALID)
        return
    await _finish_phone(message, state, phone)


async def _finish_phone(message: Message, state: FSMContext, phone: str) -> None:
    await state.update_data(customer_phone=phone)
    await state.set_state(OrderFlow.city)
    await message.answer(ru.ASK_CITY, reply_markup=REMOVE)


@router.message(OrderFlow.city, F.text)
async def enter_city(message: Message, session: AsyncSession, state: FSMContext) -> None:
    city = validators.clean_city(message.text or "")
    if not city:
        await message.answer(ru.ASK_CITY_INVALID)
        return
    await _store(state, customer_city=city)
    await state.set_state(OrderFlow.delivery_method)
    options = await delivery_service.get_options(session)
    await message.answer(ru.ASK_DELIVERY, reply_markup=inline.delivery_methods(options))


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
        await callback.answer("Способ недоступен", show_alert=True)
        return

    await callback.answer()
    await _store(state, delivery_method=method.value)

    if option.is_pickup:
        address = await settings_service.get_str(session, "delivery.pickup_address")
        await _store(state, delivery_to_pvz=True, pvz_address=address, delivery_address="")
        if callback.message is not None:
            await callback.message.answer(ru.PICKUP_INFO.format(address=address or "уточним в чате"))
        await _ask_promo(callback.message, state)
        return

    await state.set_state(OrderFlow.delivery_target)
    if callback.message is not None:
        await callback.message.answer(
            ru.ASK_DELIVERY_TARGET,
            reply_markup=inline.delivery_targets(
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
    if callback.message is not None:
        await callback.message.answer(ru.ASK_PVZ if callback_data.to_pvz else ru.ASK_ADDRESS)


@router.message(OrderFlow.delivery_address, F.text)
async def enter_address(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    to_pvz = bool(data.get("delivery_to_pvz", True))
    raw = message.text or ""
    value = validators.clean_pvz(raw) if to_pvz else validators.clean_address(raw)
    if not value:
        await message.answer(ru.ASK_PVZ if to_pvz else ru.ASK_ADDRESS_INVALID)
        return
    if to_pvz:
        await _store(state, pvz_address=value, delivery_address="")
    else:
        await _store(state, delivery_address=value, pvz_address="")
    await _ask_promo(message, state)


# ----------------------------------------------------------------- промокод


async def _ask_promo(message: Message | None, state: FSMContext) -> None:
    await state.set_state(OrderFlow.promo)
    if message is not None:
        await message.answer(ru.ASK_PROMO, reply_markup=inline.skip_promo())


@router.message(OrderFlow.promo, F.text)
async def enter_promo(message: Message, session: AsyncSession, state: FSMContext, user: User) -> None:
    data = await state.get_data()
    draft = _draft_from_state(data)
    draft.promo_code = message.text or ""
    try:
        totals = await order_service.calculate_totals(session, draft=draft, user_id=user.id)
    except promo_service.PromoError as exc:
        await message.answer(ru.PROMO_FAILED.format(reason=exc), reply_markup=inline.skip_promo())
        return
    except OrderError as exc:
        await message.answer(str(exc), reply_markup=main_menu())
        await state.clear()
        return

    await _store(state, promo_code=promo_service.normalize_code(draft.promo_code))
    await message.answer(
        ru.PROMO_APPLIED.format(
            code=promo_service.normalize_code(draft.promo_code),
            discount=ru.money(totals.discount),
        )
    )
    await _show_confirmation(message, session, state, user)


@router.callback_query(OrderFlow.promo, inline.NavCB.filter(F.action == "skip_promo"))
async def skip_promo(callback: CallbackQuery, session: AsyncSession, state: FSMContext, user: User) -> None:
    await callback.answer()
    await _store(state, promo_code="")
    if callback.message is not None:
        await _show_confirmation(callback.message, session, state, user)


# ------------------------------------------------------------ подтверждение


async def _show_confirmation(message: Message, session: AsyncSession, state: FSMContext, user: User) -> None:
    data = await state.get_data()
    draft = _draft_from_state(data)
    try:
        totals = await order_service.calculate_totals(session, draft=draft, user_id=user.id)
    except (OrderError, promo_service.PromoError) as exc:
        await message.answer(str(exc), reply_markup=main_menu())
        await state.clear()
        return

    delivery_text = "—"
    if draft.delivery_method is not None:
        option = await delivery_service.get_option(session, draft.delivery_method)
        target = draft.pvz_address or draft.delivery_address or draft.customer_city
        delivery_text = f"{option.title if option else draft.delivery_method}, {target}"

    cod_enabled = await settings_service.get_bool(session, "order.cod_enabled")
    provider = online_provider()

    await state.set_state(OrderFlow.confirm)
    await message.answer(
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
        reply_markup=inline.confirm_order(
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
    if callback.message is None:
        return

    data = await state.get_data()
    draft = _draft_from_state(data)
    draft.is_cod = callback_data.action == "pay_cod"
    draft.utm_source = user.utm_source

    try:
        order = await order_service.create_order(session, user=user, draft=draft)
    except (OrderError, promo_service.PromoError) as exc:
        await callback.message.answer(str(exc), reply_markup=main_menu())
        await state.clear()
        return

    await session.flush()
    await state.clear()

    if draft.is_cod:
        # Деньги заберёт перевозчик — заказ сразу уходит в работу логисту.
        shipping_days = await settings_service.get_str(session, "order.shipping_days")
        await callback.message.answer(
            ru.ORDER_COD_CREATED.format(
                number=order.public_number,
                total=ru.money(order.total),
                shipping_days=shipping_days,
            ),
            reply_markup=main_menu(),
        )
        await _notify_admins_new_order(session, order=order, payment_kind="при получении")
        return

    await _create_payment_and_reply(callback.message, session, user=user, order=order)


async def _create_payment_and_reply(message: Message, session: AsyncSession, *, user: User, order) -> None:
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
        await message.answer(ru.PAYMENT_UNAVAILABLE, reply_markup=main_menu())
        return

    minutes = 15
    if payment.expires_at is not None and payment.created_at is not None:
        minutes = max(int((payment.expires_at - payment.created_at).total_seconds() // 60), 1)

    if not payment.confirmation_url:
        await message.answer(ru.PAYMENT_UNAVAILABLE, reply_markup=main_menu())
        return

    await message.answer(
        ru.PAYMENT_LINK.format(
            number=order.public_number,
            total=ru.money(order.total),
            minutes=minutes,
        ),
        reply_markup=inline.payment_link(payment.confirmation_url, order.id),
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
        await callback.answer("Оплата подтверждена ✅", show_alert=True)
    elif payment.status is PaymentStatus.CANCELED:
        await callback.answer(ru.PAYMENT_EXPIRED, show_alert=True)
        if callback.message is not None:
            await callback.message.answer(
                ru.PAYMENT_EXPIRED, reply_markup=inline.retry_payment(callback_data.order_id)
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
    if callback.message is not None:
        await _create_payment_and_reply(callback.message, session, user=user, order=order)


@router.callback_query(inline.NavCB.filter(F.action == "cancel"))
async def cancel_flow(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if callback.message is not None:
        await callback.message.answer(ru.CANCELLED, reply_markup=main_menu())


@router.message(F.text == ru.BTN_CANCEL)
async def cancel_by_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(ru.CANCELLED, reply_markup=main_menu())
