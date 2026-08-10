"""Общие хелперы платёжного слоя."""
from __future__ import annotations


def build_payment_metadata(
    user_id: int,
    tariff_id: int,
    days: int,
    price: float,
    limit_ip: int = 0,
    registration_type: str = "telegram",
    email: str = "",
    **extra,
) -> dict:
    """Единая структура metadata для всех платёжных каналов.

    Поля:
        telegram_user_id — кому продаём.
        subscription_days, price, limit_ip, tariff_id — что именно продали.
        registration_type — 'telegram' (бот) | 'site' (сайт).
        email — для чека (если задано).
        **extra — любые провайдер-специфичные ключи (cms_name, payment_type,
        bot_payment_uuid, paid_stars и т.п.).
    """
    meta = {
        "telegram_user_id": user_id,
        "subscription_days": days,
        "price": price,
        "limit_ip": limit_ip,
        "tariff_id": tariff_id,
        "registration_type": registration_type,
    }
    if email:
        meta["email"] = email
    meta.update(extra)
    return meta


__all__ = ["build_payment_metadata"]
