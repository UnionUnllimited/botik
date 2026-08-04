"""Инлайн-клавиатуры и фабрики callback-данных.

Общее правило: с любого экрана должен быть выход. Внизу каждой клавиатуры
стоит ряд навигации — «назад» там, где есть куда возвращаться, и «главное
меню» всегда. Ряд добавляется через `_nav`, чтобы его нельзя было забыть.
"""

from __future__ import annotations

from decimal import Decimal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.texts import ru


class ProductCB(CallbackData, prefix="prod"):
    product_id: int
    action: str = "open"
    """open — показать карточку модели, take — выбрать её и идти к тарифам."""


class PlanCB(CallbackData, prefix="plan"):
    plan_id: int
    with_device: bool


class DeliveryCB(CallbackData, prefix="dlv"):
    method: str


class DeliveryTargetCB(CallbackData, prefix="dlvt"):
    to_pvz: bool


class PaymentCB(CallbackData, prefix="pay"):
    order_id: int
    method: str


class OrderCB(CallbackData, prefix="ord"):
    order_id: int
    action: str


class NavCB(CallbackData, prefix="nav"):
    action: str


class MenuCB(CallbackData, prefix="menu"):
    section: str


def _nav(
    builder: InlineKeyboardBuilder,
    *,
    back: str | None = None,
    cancel: bool = False,
) -> None:
    """Ряд навигации внизу клавиатуры. Вызывать после adjust().

    Больше двух кнопок в ряд не ставим: на телефоне они схлопываются
    в нечитаемые огрызки. Поэтому «назад» и «отмена» — взаимоисключающие.
    """
    row: list[InlineKeyboardButton] = []
    if back is not None:
        row.append(InlineKeyboardButton(text=ru.BTN_BACK, callback_data=NavCB(action=back).pack()))
    elif cancel:
        row.append(InlineKeyboardButton(text=ru.BTN_CANCEL, callback_data=NavCB(action="cancel").pack()))
    row.append(InlineKeyboardButton(text=ru.BTN_MENU, callback_data=NavCB(action="menu").pack()))
    builder.row(*row)


def main_menu() -> InlineKeyboardMarkup:
    """Главное меню. Покупка — отдельной широкой кнопкой: это основное действие."""
    builder = InlineKeyboardBuilder()
    builder.button(text=ru.BTN_BUY, callback_data=MenuCB(section="buy"))
    builder.button(text=ru.BTN_MY_DEVICE, callback_data=MenuCB(section="device"))
    builder.button(text=ru.BTN_SUBSCRIPTION, callback_data=MenuCB(section="subscription"))
    builder.button(text=ru.BTN_GUIDES, callback_data=MenuCB(section="guides"))
    builder.button(text=ru.BTN_SUPPORT, callback_data=MenuCB(section="support"))
    builder.button(text=ru.BTN_REFERRAL, callback_data=MenuCB(section="referral"))
    builder.adjust(1, 2, 2, 1)
    return builder.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _nav(builder)
    return builder.as_markup()


def catalog(products: list) -> InlineKeyboardMarkup:
    """Список моделей. Карточка каждой открывается отдельным экраном."""
    builder = InlineKeyboardBuilder()
    for product in products:
        label = f"{product.title} — {ru.money(product.price)}"
        if not product.in_stock:
            label = f"{product.title} — нет в наличии"
        builder.button(text=label, callback_data=ProductCB(product_id=product.id))
    builder.adjust(1)
    _nav(builder)
    return builder.as_markup()


def product_card(product_id: int, *, in_stock: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if in_stock:
        builder.button(
            text=ru.BTN_CHOOSE,
            callback_data=ProductCB(product_id=product_id, action="take"),
        )
    builder.adjust(1)
    _nav(builder, back="catalog")
    return builder.as_markup()


def plans(plan_list: list, *, with_device: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for plan in plan_list:
        builder.button(
            text=ru.plan_button(
                title=plan.title,
                price=plan.price,
                months=plan.months,
                discount=plan.discount_percent,
            ),
            callback_data=PlanCB(plan_id=plan.id, with_device=with_device),
        )
    builder.adjust(1)
    _nav(builder, back="catalog" if with_device else "subscription")
    return builder.as_markup()


def delivery_methods(options: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for option in options:
        price = option.pvz_price if not option.is_pickup else Decimal("0.00")
        suffix = "бесплатно" if price <= 0 else ru.money(price)
        builder.button(
            text=f"{option.title} — {suffix}",
            callback_data=DeliveryCB(method=option.method.value),
        )
    builder.adjust(1)
    _nav(builder, cancel=True)
    return builder.as_markup()


def delivery_targets(*, courier_price: Decimal, pvz_price: Decimal) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"📦 В пункт выдачи — {ru.money(pvz_price)}",
        callback_data=DeliveryTargetCB(to_pvz=True),
    )
    builder.button(
        text=f"🚚 Курьером до двери — {ru.money(courier_price)}",
        callback_data=DeliveryTargetCB(to_pvz=False),
    )
    builder.adjust(1)
    _nav(builder, cancel=True)
    return builder.as_markup()


def waiting_for_text() -> InlineKeyboardMarkup:
    """Экран, где ждём ответ текстом: выход обязан быть виден и там."""
    builder = InlineKeyboardBuilder()
    _nav(builder, cancel=True)
    return builder.as_markup()


def skip_promo() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=ru.BTN_SKIP, callback_data=NavCB(action="skip_promo"))
    builder.adjust(1)
    _nav(builder, cancel=True)
    return builder.as_markup()


def confirm_order(*, cod_enabled: bool, online_enabled: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if online_enabled:
        builder.button(text=ru.PAY_ONLINE, callback_data=NavCB(action="pay_online"))
    if cod_enabled:
        builder.button(text=ru.PAY_ON_DELIVERY, callback_data=NavCB(action="pay_cod"))
    builder.adjust(1)
    _nav(builder, cancel=True)
    return builder.as_markup()


def payment_link(url: str, order_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=ru.PAYMENT_BUTTON, url=url)
    builder.button(
        text=ru.PAYMENT_CHECK,
        callback_data=OrderCB(order_id=order_id, action="check"),
    )
    builder.adjust(1)
    _nav(builder)
    return builder.as_markup()


def retry_payment(order_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=ru.PAY_AGAIN, callback_data=OrderCB(order_id=order_id, action="repay"))
    builder.adjust(1)
    _nav(builder)
    return builder.as_markup()


def subscription_actions(*, has_subscription: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    label = ru.SUBSCRIPTION_EXTEND if has_subscription else ru.SUBSCRIPTION_BUY
    builder.button(text=label, callback_data=NavCB(action="buy_subscription"))
    builder.adjust(1)
    _nav(builder)
    return builder.as_markup()


def device_actions(*, has_device: bool, has_subscription: bool) -> InlineKeyboardMarkup:
    """Экран «Мой роутер»: обновить показания и уйти туда, где что-то можно сделать."""
    builder = InlineKeyboardBuilder()
    if has_device:
        builder.button(text=ru.BTN_REFRESH, callback_data=MenuCB(section="device"))
    if has_subscription:
        builder.button(text=ru.BTN_SUBSCRIPTION, callback_data=MenuCB(section="subscription"))
    else:
        builder.button(text=ru.BTN_BUY, callback_data=MenuCB(section="buy"))
    builder.adjust(1)
    _nav(builder)
    return builder.as_markup()


def referral_share(link: str) -> InlineKeyboardMarkup:
    share_url = f"https://t.me/share/url?url={link}&text={ru.REFERRAL_SHARE_TEXT}"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=ru.REFERRAL_SHARE, url=share_url))
    _nav(builder)
    return builder.as_markup()


def tracking(url: str | None) -> InlineKeyboardMarkup | None:
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=ru.TRACK_LINK, url=url)]])
