"""Состояния диалогов бота."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class OrderFlow(StatesGroup):
    """Покупка роутера: каталог → тариф → контакты → доставка → промокод → оплата."""

    product = State()
    plan = State()
    name = State()
    phone = State()
    city = State()
    delivery_method = State()
    delivery_target = State()
    delivery_address = State()
    promo = State()
    confirm = State()
    payment = State()


class ActivationFlow(StatesGroup):
    """Активация роутера: клиент называет MAC с наклейки, мы настраиваем устройство."""

    mac = State()


class SubscriptionFlow(StatesGroup):
    """Покупка или продление подписки без роутера."""

    plan = State()
    promo = State()
    confirm = State()
