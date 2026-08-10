"""Повторная выдача услуги по платежам со статусом failed (подписка / докупка GB)."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from app_config import app_conf
import db_helpers
from src.core.utils import get_default_limit_gb
from subscription_manager import grant_subscription

_RETRYABLE_TYPE_FILTER = (
    "COALESCE(json_extract(metadata_json, '$.payment_type'), '') "
    "NOT IN ('device_limit_upgrade')"
)


def _parse_metadata(raw: Any) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


async def _resolve_traffic_gb(meta: dict) -> int:
    traffic_to_add_gb = int(meta.get("traffic_to_add_gb") or 0)
    if traffic_to_add_gb > 0:
        return traffic_to_add_gb

    tariff_id = meta.get("tariff_id")
    if tariff_id:
        try:
            tariff = await db_helpers.get_traffic_topup_tariff_by_id(int(tariff_id))
            if tariff:
                gb = int(dict(tariff).get("traffic_gb") or 0)
                if gb > 0:
                    return gb
        except (TypeError, ValueError):
            pass

    default_gb = get_default_limit_gb()
    return int(default_gb) if default_gb else 0


async def retry_failed_payment(payment_id: str) -> Tuple[bool, Optional[str]]:
    """
    Повторно выдаёт подписку или докупку GB для платежа failed.
    device_limit_upgrade не обрабатывается.
    """
    row = await db_helpers.get_payment(payment_id)
    if not row:
        return False, "not_found"

    status = row[4] if len(row) > 4 else None
    if status != "failed":
        return False, f"status_{status or 'unknown'}"

    meta = _parse_metadata(row[6] if len(row) > 6 else None)
    payment_type = (meta.get("payment_type") or "").strip()

    if payment_type == "device_limit_upgrade":
        return False, "skipped_device_upgrade"

    try:
        telegram_id = int(meta.get("telegram_user_id") or row[1] or 0)
    except (TypeError, ValueError):
        telegram_id = 0
    if telegram_id <= 0:
        return False, "no_telegram_id"

    user = await db_helpers.get_user(telegram_id)
    if not user:
        return False, "user_not_found"
    if user.get("is_blocked"):
        return False, "user_blocked"

    await app_conf.load_settings()

    if payment_type == "traffic_renewal":
        remnawave_uuid = user.get("xui_client_uuid")
        if not remnawave_uuid:
            return False, "no_uuid"

        traffic_to_add_gb = await _resolve_traffic_gb(meta)
        if traffic_to_add_gb <= 0:
            return False, "no_traffic_gb"

        from remnawave_manager import remnawave_manager_instance

        try:
            renewal_result = await remnawave_manager_instance.update_user_subscription(
                str(remnawave_uuid),
                days_to_add=0,
                traffic_to_add_gb=traffic_to_add_gb,
                apply_default_squad=True,
            )
        except Exception as e:
            logger.error(f"[PAYMENT RETRY] traffic {payment_id}: {e}", exc_info=True)
            return False, f"remnawave_error:{type(e).__name__}"

        if not renewal_result:
            return False, "traffic_grant_failed"

        await db_helpers.update_payment_status(payment_id, "succeeded")
        logger.info(
            f"[PAYMENT RETRY] traffic OK payment={payment_id} user={telegram_id} +{traffic_to_add_gb}GB"
        )
        return True, None

    # Продление подписки (payment_type отсутствует или иной, кроме device/traffic)
    days = int(meta.get("subscription_days") or app_conf.get("subscription_days", 30) or 30)
    limit_ip = int(meta.get("limit_ip") or 0)
    traffic_gb_to_add = 0
    tariff_id = meta.get("tariff_id")
    if tariff_id:
        try:
            tariff = await db_helpers.get_tariff_by_id(int(tariff_id))
            if tariff:
                traffic_gb_to_add = int(tariff.get("traffic_gb") or 0)
        except (TypeError, ValueError):
            pass

    try:
        result = await grant_subscription(
            telegram_id,
            days,
            is_trial=False,
            limit_ip=limit_ip,
            traffic_gb_to_add=traffic_gb_to_add,
            fetch_sub_link=False,
            recreate_if_missing=True,
        )
    except Exception as e:
        logger.error(f"[PAYMENT RETRY] subscription {payment_id}: {e}", exc_info=True)
        return False, f"grant_error:{type(e).__name__}"

    if not result:
        return False, "grant_failed"

    await db_helpers.update_payment_status(payment_id, "succeeded")
    logger.info(
        f"[PAYMENT RETRY] subscription OK payment={payment_id} user={telegram_id} +{days}d"
    )
    return True, None
