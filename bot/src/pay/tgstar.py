"""Telegram Stars: конвертация рублёвых цен в ⭐.

Цены тарифов всегда хранятся в рублях. При оплате через TG Stars мы
конвертируем по настройке `tgstar_rub_per_star` (₽ за 1 ⭐). Округление
вверх — лучше получить лишнюю копейку, чем продать в убыток.
"""
from __future__ import annotations

import math


def rub_to_stars(rub_amount: float, app_conf) -> int:
    """Конвертирует ₽ в звёзды Telegram по курсу из настроек.

    Курс: `tgstar_rub_per_star` (₽ за 1 ⭐). По умолчанию 2.0
    (~ актуальный курс Fragment ≈ 50 stars / 1 USD ≈ 95 ₽).
    Минимум 1 звезда: Telegram не примет инвойс на 0 ⭐.
    """
    try:
        rate = float(app_conf.get("tgstar_rub_per_star", "2.0") or 2.0)
    except (TypeError, ValueError):
        rate = 2.0
    if rate <= 0:
        rate = 2.0

    try:
        rub = float(rub_amount or 0)
    except (TypeError, ValueError):
        rub = 0.0
    stars = int(math.ceil(rub / rate))
    return max(1, stars)


__all__ = ["rub_to_stars"]
