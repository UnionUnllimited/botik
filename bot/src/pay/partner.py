"""Единая точка начисления партнёрских процентов и реферальных бонусов
после успешной оплаты.

Используется всеми платёжными провайдерами (YooKassa, Platega, CryptoBot,
TG Stars, YooMoney). Гарантирует одинаковое поведение во всех ветках:

  • Партнёрское начисление (% в RUB на ``users.partner_balance_rub``) —
    только при ``method == 'partner'``, ``currency == 'RUB'`` и
    ``amount_rub > 0``.
  • Реферальный бонус за первый платёж (дни подписки) — только при
    ``method != 'partner'`` и ``not is_referral_payment_bonus_given(...)``.
  • При выдаче ref-бонуса трафик пригласившего НЕ сбрасывается
    (``reset_traffic_on_renewal=False``), и сохраняется его текущий
    лимит устройств (через ``resolve_limit_ip_for_user``).
  • Партнёрский % и реф-бонус — взаимоисключающие пути (партнёру
    деньгами, рефералу днями).

Все операции идемпотентны и устойчивы к ошибкам в одном из шагов
(не валим всю обработку платежа).
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


async def credit_partner_and_referral(
    *,
    payer_user_id: int,
    payment_id: str,
    amount_rub: float,
    currency: str,
    bot: Any,
    db_helpers: Any,
    app_conf: Any,
    keyboards: Any,
    grant_subscription: Callable[..., Awaitable[Any]],
    resolve_limit_ip_for_user: Callable[[int], Awaitable[int]],
    log_prefix: str = "",
) -> None:
    """Двойной шаг после успешной оплаты подписки.

    Параметры:
        payer_user_id  — telegram_id пользователя, который оплатил.
        payment_id     — id платежа (наш внутренний).
        amount_rub     — сумма платежа в рублях.
        currency       — фактическая валюта платежа (для партнёрки берётся
                         только RUB; для XTR/USD партнёр не получает %).
        log_prefix     — короткая метка для логов: "YooKassa, webhook",
                         "Platega, webhook", "CryptoBot, webhook" и т.п.

    Все имплементации из main.py (db_helpers, app_conf, keyboards,
    grant_subscription, resolve_limit_ip_for_user) передаются явно — это
    нужно, чтобы избежать кругового импорта main.py ↔ src.pay.
    """
    try:
        invited_by = await db_helpers.get_invited_by(payer_user_id)
        method = await db_helpers.get_invited_by_method(payer_user_id)
    except Exception as e:
        logger.error(
            "[PARTNER] %s: ошибка получения invited_by/method для %s: %s",
            log_prefix or "?", payer_user_id, e,
        )
        return

    if not invited_by:
        return

    # ── 1. Партнёр: % деньгами на partner_balance_rub ────────────────────
    if method == "partner":
        try:
            cur = (currency or "").upper()
            if cur != "RUB" or amount_rub <= 0:
                return
            try:
                percent = await db_helpers.get_partner_percent(invited_by)
            except Exception as e:
                logger.warning(
                    "[PARTNER] %s: get_partner_percent упал: %s", log_prefix, e,
                )
                return
            bonus = round(float(amount_rub) * float(percent) / 100.0, 2)
            if bonus <= 0:
                return

            try:
                async with db_helpers.get_db_connection_safe() as db:
                    await db.execute(
                        "UPDATE users SET partner_balance_rub = "
                        "COALESCE(partner_balance_rub,0) + ? "
                        "WHERE telegram_id = ?",
                        (bonus, invited_by),
                    )
                    await db.commit()
            except Exception as e:
                logger.error(
                    "[PARTNER] %s: не удалось начислить partner_balance_rub: %s",
                    log_prefix, e,
                )
                return  # без записи в БД — не логируем и не уведомляем

            try:
                await db_helpers.log_partner_accrual(
                    invited_by, payer_user_id, payment_id,
                    float(amount_rub), "RUB", percent, bonus,
                )
            except Exception as e:
                logger.warning(
                    "[PARTNER] %s: log_partner_accrual упал: %s", log_prefix, e,
                )

            try:
                await bot.send_message(
                    invited_by,
                    f"💰 Партнёрское начисление: +{bonus} ₽ за оплату приглашённого",
                    reply_markup=keyboards.get_back_to_main_keyboard(),
                )
            except Exception:
                pass

            logger.info(
                "[PARTNER] %s: +%.2f ₽ → %s (от %s, %s %%)",
                log_prefix, bonus, invited_by, payer_user_id, percent,
            )
        except Exception as e:
            logger.error(
                "[PARTNER] %s: общая ошибка партнёрского начисления: %s",
                log_prefix, e,
            )
        return  # партнёр и реферал — взаимоисключающие пути

    # ── 2. Реферал: бонус-дни за первый платёж ───────────────────────────
    try:
        already_given = await db_helpers.is_referral_payment_bonus_given(
            invited_by, payer_user_id,
        )
        if already_given:
            return

        inviter = await db_helpers.get_user(invited_by)
        if not inviter:
            return

        try:
            ref_bonus_days = int(app_conf.get("ref_bonus_on_payment_days", 7))
        except (TypeError, ValueError):
            ref_bonus_days = 7
        if ref_bonus_days <= 0:
            return

        inviter_limit_ip = await resolve_limit_ip_for_user(invited_by)

        await grant_subscription(
            invited_by,
            ref_bonus_days,
            is_trial=False,
            limit_ip=inviter_limit_ip,
            reset_traffic_on_renewal=False,
        )
        try:
            await db_helpers.mark_referral_payment_bonus_given(
                invited_by, payer_user_id,
            )
        except Exception as e:
            logger.warning(
                "[REFERRAL] %s: mark_referral_payment_bonus_given: %s",
                log_prefix, e,
            )

        logger.info(
            "[REFERRAL] %s: +%d дн. → %s (за оплату %s)",
            log_prefix, ref_bonus_days, invited_by, payer_user_id,
        )

        try:
            tpl = app_conf.get("text_ref_bonus_on_payment") or ""
            text = tpl.format(days=ref_bonus_days) if tpl else (
                f"🎁 Вам начислен бонус: +{ref_bonus_days} дней подписки "
                f"за оплату приглашённого пользователя!"
            )
            await bot.send_message(
                invited_by,
                text,
                reply_markup=keyboards.get_back_to_main_keyboard(),
            )
        except Exception as e:
            logger.warning(
                "[REFERRAL] %s: не удалось уведомить пригласившего %s: %s",
                log_prefix, invited_by, e,
            )
    except Exception as e:
        logger.error(
            "[REFERRAL] %s: общая ошибка начисления реф-бонуса: %s",
            log_prefix, e,
        )
