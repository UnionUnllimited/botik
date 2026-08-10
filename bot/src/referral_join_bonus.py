"""Начисление реферального бонуса за первое подключение приглашённого (join).

Триггер: вебхук Remnawave ``user.first_connected``.
Партнёрская привязка и пригласивший с ``registration_type == 'site'`` — без бонуса.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


async def try_grant_referral_join_bonus(
    *,
    invited_user_id: int,
    bot: Any,
    db_helpers: Any,
    app_conf: Any,
    keyboards: Any,
    grant_subscription: Callable[..., Awaitable[Any]],
    resolve_limit_ip_for_user: Callable[[int], Awaitable[int]],
    log_prefix: str = "Remnawave, user.first_connected",
) -> bool:
    """Пробует выдать join-бонус пригласившему. Возвращает True, если бонус выдан."""
    try:
        invited_by = await db_helpers.get_invited_by(invited_user_id)
        if not invited_by:
            return False

        method = await db_helpers.get_invited_by_method(invited_user_id)
        if method == "partner":
            logger.debug(
                "[REFERRAL] %s: партнёрская привязка для %s — join-бонус не начисляем",
                log_prefix,
                invited_user_id,
            )
            return False

        inviter = await db_helpers.get_user(invited_by)
        if not inviter:
            logger.warning(
                "[REFERRAL] %s: пригласивший %s не найден в БД (приглашённый %s)",
                log_prefix,
                invited_by,
                invited_user_id,
            )
            return False

        if (inviter.get("registration_type") or "").strip().lower() == "site":
            logger.info(
                "[REFERRAL] %s: пригласивший %s с registration_type=site — join-бонус пропущен",
                log_prefix,
                invited_by,
            )
            return False

        already_given = await db_helpers.is_referral_payment_bonus_given(
            invited_by, invited_user_id, bonus_type="join",
        )
        if already_given:
            return False

        try:
            ref_bonus_days = int(app_conf.get("ref_bonus_on_join_days", 3))
        except (TypeError, ValueError):
            ref_bonus_days = 3
        if ref_bonus_days <= 0:
            return False

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
                invited_by, invited_user_id, bonus_type="join",
            )
        except Exception as e:
            logger.warning(
                "[REFERRAL] %s: mark_referral_payment_bonus_given: %s",
                log_prefix,
                e,
            )

        logger.info(
            "[REFERRAL] %s: +%d дн. → %s (за первое подключение %s)",
            log_prefix,
            ref_bonus_days,
            invited_by,
            invited_user_id,
        )

        try:
            tpl = app_conf.get("text_ref_bonus_on_join") or ""
            text = tpl.format(days=ref_bonus_days) if tpl else (
                f"🎁 Вы получили {ref_bonus_days} дней бонуса "
                f"за первое подключение приглашённого друга!"
            )
            await bot.send_message(
                invited_by,
                text,
                reply_markup=keyboards.get_back_to_main_keyboard(),
            )
        except Exception as e:
            logger.warning(
                "[REFERRAL] %s: не удалось уведомить пригласившего %s: %s",
                log_prefix,
                invited_by,
                e,
            )
        return True
    except Exception as e:
        logger.error(
            "[REFERRAL] %s: ошибка начисления join-бонуса для %s: %s",
            log_prefix,
            invited_user_id,
            e,
            exc_info=True,
        )
        return False
