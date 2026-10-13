"""Один поиск на всю админку: что бы ни ввели — нужная карточка.

У каждого раздела был свой поиск: роутеры по MAC, заказы по номеру, клиенты
по логину. Оператор с номером телефона в руке не знал, в какой из них идти.
Здесь всё разом: номер заказа, кусок MAC, @логин, телефон, почта, Telegram-ID,
имя из доставки.

Телефон сравнивается цифрами: «+7 900 123-45-67», «89001234567» и «9001234567»
— один и тот же человек, и хранится номер то так, то эдак.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.models import Device, Order, User
from core.texts import ORDER_STATUS_TITLES

LIMIT = 10
"""На каждый вид. Найти больше десяти — значит, запрос слишком общий,
и листать их всё равно никто не станет."""

_MAC_CHARS = re.compile(r"^[0-9a-fA-F:\-.]+$")
_DIGITS = re.compile(r"\D")


def _phone_tail(text: str) -> str:
    """Последние десять цифр: так «+7…» и «8…» сходятся в одно."""
    digits = _DIGITS.sub("", text)
    return digits[-10:] if len(digits) >= 7 else ""


def _digits_of(column):
    return func.regexp_replace(column, r"\D", "", "g")


async def find(session: AsyncSession, query: str) -> list[dict[str, Any]]:
    text = (query or "").strip()
    if len(text) < 2:
        return []
    like = f"%{text}%"
    found: list[dict[str, Any]] = []
    phone = _phone_tail(text)
    handle = text.lstrip("@")

    # Роутеры: MAC в любом написании, модель.
    mac_parts = [Device.model.ilike(like)]
    bare = re.sub(r"[:\-.\s]", "", text)
    if _MAC_CHARS.match(text) and len(bare) >= 4:
        mac_parts.append(func.replace(Device.mac, ":", "").ilike(f"%{bare}%"))
    for device in await session.scalars(
        select(Device).where(or_(*mac_parts)).options(selectinload(Device.user)).limit(LIMIT)
    ):
        found.append({
            "kind": "router",
            "id": device.id,
            "title": device.mac,
            "sub": " · ".join(
                part for part in (device.model, device.user.telegram_name if device.user else "") if part
            ),
        })

    # Заказы: номер, телефон, имя из доставки.
    order_parts = [Order.public_number.ilike(like), Order.customer_name.ilike(like)]
    if phone:
        order_parts.append(_digits_of(Order.customer_phone).like(f"%{phone}"))
    for order in await session.scalars(
        select(Order).where(or_(*order_parts)).order_by(Order.id.desc()).limit(LIMIT)
    ):
        found.append({
            "kind": "order",
            "id": order.id,
            "title": order.public_number,
            "sub": " · ".join(
                part
                for part in (
                    ORDER_STATUS_TITLES.get(order.status, str(order.status)),
                    order.customer_name,
                    order.customer_city,
                )
                if part
            ),
        })

    # Клиенты: логин, Telegram-ID, телефон, почта, имя. Только те, у кого
    # есть Telegram: карточку клиента админка бота открывает по нему.
    user_parts = [
        User.username.ilike(f"%{handle}%"),
        User.email.ilike(like),
        User.full_name.ilike(like),
        User.first_name.ilike(like),
        cast(User.tg_id, String) == text,
    ]
    if phone:
        user_parts.append(_digits_of(User.phone).like(f"%{phone}"))
    for user in await session.scalars(
        select(User).where(User.tg_id.is_not(None), or_(*user_parts)).limit(LIMIT)
    ):
        found.append({
            "kind": "client",
            "id": user.tg_id,
            "title": user.telegram_name,
            "sub": " · ".join(part for part in (user.display_name, user.phone or "") if part),
        })
    return found
