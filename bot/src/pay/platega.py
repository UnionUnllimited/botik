"""Создание платежей в Platega (v2 endpoint).

Используется и ботом, и сайтом. v2: метод оплаты выбирает плательщик
на странице Platega; ``transactionId`` генерирует Platega.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

import db_helpers

from .helpers import build_payment_metadata

logger = logging.getLogger(__name__)

PLATEGA_V2_URL = "https://app.platega.io/v2/transaction/process"


async def create_platega_payment_shared(
    merchant_id: str,
    api_secret: str,
    amount: float,
    tariff: dict,
    user_id: int,
    return_url: str,
    registration_type: str = "telegram",
    email: str = "",
    payment_method_id: int = 2,  # deprecated: в v2 endpoint метод выбирает плательщик
    **extra,
) -> Optional[tuple[str, str]]:
    """Создаёт платёж Platega через v2 endpoint /v2/transaction/process.

    Метод оплаты выбирается плательщиком на странице Platega.
    Возвращает (payment_id, redirect_url) или None при ошибке.
    """
    _ = payment_method_id  # больше не используется в v2
    try:
        tariff_id = tariff["id"]
        days = int(tariff["days"])

        meta = build_payment_metadata(
            user_id=user_id,
            tariff_id=tariff_id,
            days=days,
            price=amount,
            limit_ip=int(tariff.get("limit_ip") or 0),
            registration_type=registration_type,
            email=email,
            cms_name="platega",
            **extra,
        )

        payload = {
            "paymentDetails": {"amount": amount, "currency": "RUB"},
            "description": f"Подписка: {tariff['name']}",
            "return": return_url,
            "failedUrl": return_url,
            "payload": json.dumps(meta, ensure_ascii=False),
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                PLATEGA_V2_URL,
                headers={
                    "Content-Type": "application/json",
                    "X-MerchantId": str(merchant_id),
                    "X-Secret": str(api_secret),
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        # В v2 transactionId генерирует Platega — сохраняем его как payment_id
        tx_id = data.get("transactionId") or data.get("id")
        redirect_url = (
            data.get("url") or data.get("redirect") or data.get("paymentLink")
        )
        if not tx_id or not redirect_url:
            logger.error(f"[PAYMENT] Platega v2 неполный ответ: {data}")
            return None

        payment_id = f"PLATEGA_{tx_id}"

        await db_helpers.add_payment(
            payment_id=payment_id,
            telegram_id=user_id,
            amount=amount,
            currency="RUB",
            metadata_json=json.dumps(meta, ensure_ascii=False),
        )
        logger.info(
            f"[PAYMENT] Platega v2 платёж создан: {payment_id}, "
            f"user={user_id}, type={registration_type}"
        )
        return payment_id, redirect_url

    except Exception as e:
        logger.error(f"[PAYMENT] Platega v2 ошибка: {type(e).__name__}: {e}")
        return None


async def create_platega_payment_device_upgrade(
    merchant_id: str,
    api_secret: str,
    user_id: int,
    new_limit: int,
    upgrade_meta: dict,
    return_url: str,
    payment_method_id: int = 2,  # deprecated: в v2 метод выбирает плательщик
) -> Optional[tuple[str, str]]:
    """Создаёт платёж Platega через v2 endpoint для расширения лимита устройств.

    upgrade_meta — результат device_upgrade_service.build_payment_metadata().
    Возвращает (payment_id, redirect_url) или None.
    """
    _ = payment_method_id  # больше не используется в v2
    try:
        amount = float(upgrade_meta.get("price") or 0)
        if amount <= 0:
            logger.error(f"[PAYMENT] device_upgrade Platega: неверная сумма {amount}")
            return None

        meta = dict(upgrade_meta)
        meta["cms_name"] = "platega"
        meta.setdefault("registration_type", "bot")

        payload = {
            "paymentDetails": {"amount": amount, "currency": "RUB"},
            "description": f"Расширение лимита устройств до {new_limit}",
            "return": return_url,
            "failedUrl": return_url,
            "payload": json.dumps(meta, ensure_ascii=False),
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                PLATEGA_V2_URL,
                headers={
                    "Content-Type": "application/json",
                    "X-MerchantId": str(merchant_id),
                    "X-Secret": str(api_secret),
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        tx_id = data.get("transactionId") or data.get("id")
        redirect_url = (
            data.get("url") or data.get("redirect") or data.get("paymentLink")
        )
        if not tx_id or not redirect_url:
            logger.error(f"[PAYMENT] device_upgrade Platega v2 неполный ответ: {data}")
            return None

        payment_id = f"PLATEGA_{tx_id}"

        await db_helpers.add_payment(
            payment_id=payment_id,
            telegram_id=user_id,
            amount=amount,
            currency="RUB",
            metadata_json=json.dumps(meta, ensure_ascii=False),
        )
        logger.info(
            f"[PAYMENT] device_upgrade Platega v2 создан: {payment_id}, "
            f"user={user_id}, new_limit={new_limit}"
        )
        return payment_id, redirect_url

    except Exception as e:
        logger.error(
            f"[PAYMENT] device_upgrade Platega ошибка: {type(e).__name__}: {e}"
        )
        return None


__all__ = [
    "create_platega_payment_shared",
    "create_platega_payment_device_upgrade",
]
