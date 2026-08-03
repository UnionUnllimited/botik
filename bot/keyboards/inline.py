"""Инлайн-клавиатуры и фабрики callback-данных."""

from __future__ import annotations

from decimal import Decimal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.texts import ru


class ProductCB(CallbackData, prefix="prod"):
    product_id: int


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


def main_menu() -> InlineKeyboardMarkup:
    """Главное меню — инлайн-кнопками под сообщением, по две в ряд."""
    builder = InlineKeyboardBuilder()
    builder.button(text=ru.BTN_BUY, callback_data=MenuCB(section="buy"))
    builder.button(text=ru.BTN_MY_DEVICE, callback_data=MenuCB(section="device"))
    builder.button(text=ru.BTN_SUBSCRIPTION, callback_data=MenuCB(section="subscription"))
    builder.button(text=ru.BTN_GUIDES, callback_data=MenuCB(section="guides"))
    builder.button(text=ru.BTN_SUPPORT, callback_data=MenuCB(section="support"))
    builder.button(text=ru.BTN_REFERRAL, callback_data=MenuCB(section="referral"))
    builder.adjust(2, 2, 2)
    return builder.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=ru.BTN_MENU, callback_data=NavCB(action="menu"))
    return builder.as_markup()


def catalog(products: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for product in products:
        label = f"{product.title} — {ru.money(product.price)}"
        if not product.in_stock:
            label = f"{product.title} — нет в наличии"
        builder.button(text=label, callback_data=ProductCB(product_id=product.id))
    builder.adjust(1)
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
    builder.button(text=ru.BTN_BACK, callback_data=NavCB(action="catalog"))
    builder.adjust(1)
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
    return builder.as_markup()


def skip_promo() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=ru.BTN_SKIP, callback_data=NavCB(action="skip_promo"))
    builder.button(text=ru.BTN_CANCEL, callback_data=NavCB(action="cancel"))
    builder.adjust(2)
    return builder.as_markup()


def confirm_order(*, cod_enabled: bool, online_enabled: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if online_enabled:
        builder.button(text=ru.PAY_ONLINE, callback_data=NavCB(action="pay_online"))
    if cod_enabled:
        builder.button(text=ru.PAY_ON_DELIVERY, callback_data=NavCB(action="pay_cod"))
    builder.button(text=ru.BTN_CANCEL, callback_data=NavCB(action="cancel"))
    builder.adjust(1)
    return builder.as_markup()


def payment_link(url: str, order_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=ru.PAYMENT_BUTTON, url=url)
    builder.button(
        text="🔄 Проверить оплату",
        callback_data=OrderCB(order_id=order_id, action="check"),
    )
    builder.adjust(1)
    return builder.as_markup()


def retry_payment(order_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=ru.PAY_AGAIN, callback_data=OrderCB(order_id=order_id, action="repay"))
    builder.adjust(1)
    return builder.as_markup()


def subscription_actions(*, has_subscription: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    label = ru.SUBSCRIPTION_EXTEND if has_subscription else ru.SUBSCRIPTION_BUY
    builder.button(text=label, callback_data=NavCB(action="buy_subscription"))
    builder.adjust(1)
    return builder.as_markup()


def referral_share(link: str) -> InlineKeyboardMarkup:
    share_url = f"https://t.me/share/url?url={link}&text={ru.REFERRAL_SHARE_TEXT}"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ru.REFERRAL_SHARE, url=share_url)]]
    )


def tracking(url: str | None) -> InlineKeyboardMarkup | None:
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=ru.TRACK_LINK, url=url)]])
