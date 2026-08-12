"""Создание платежей в YooKassa.

Используется и ботом, и сайтом. Конкретная конфигурация (shop_id,
secret_key) передаётся параметрами — не подтягивается из app_conf,
чтобы один и тот же модуль работал из разных мест с разными
учётками (бот, сайт).
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid as _uuid
from typing import Optional

# Внешний SDK ЮKassa. В Python 3 absolute imports — пакет `yookassa` будет
# найден в site-packages, не в текущем каталоге `src/pay/`.
from yookassa import Configuration, Payment
from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder

import db_helpers

from .helpers import build_payment_metadata

logger = logging.getLogger(__name__)


async def create_yookassa_payment_shared(
    shop_id: str,
    secret_key: str,
    amount: float,
    currency: str,
    tariff: dict,
    user_id: int,
    return_url: str,
    registration_type: str = "telegram",
    email: str = "",
    sbp_only: bool = False,
    **extra,
) -> Optional[tuple[str, str]]:
    """Создаёт платёж YooKassa, сохраняет в БД.

    Возвращает (payment_id, confirmation_url) или None при ошибке.
    """
    try:
        Configuration.account_id = shop_id
        Configuration.secret_key = secret_key

        tariff_id = tariff["id"]
        days = int(tariff["days"])
        price_str = f"{amount:.2f}"
        idempotence = str(_uuid.uuid4())

        receipt_email = email if (
            email and "@" in email and not email.startswith("tg:")
        ) else f"tg{user_id}@gmail.com"

        meta = build_payment_metadata(
            user_id=user_id,
            tariff_id=tariff_id,
            days=days,
            price=amount,
            limit_ip=int(tariff.get("limit_ip") or 0),
            registration_type=registration_type,
            email=email,
            bot_payment_uuid=idempotence,
            **extra,
        )

        builder = PaymentRequestBuilder()
        builder.set_amount({"value": price_str, "currency": currency}) \
               .set_capture(True) \
               .set_confirmation({"type": "redirect", "return_url": return_url}) \
               .set_description(f"Оплата подписки: {tariff['name']}") \
               .set_metadata(meta) \
               .set_receipt({
                   "customer": {"email": receipt_email},
                   "items": [{
                       "description": f"Подписка ({tariff['name']})",
                       "quantity": "1.00",
                       "amount": {"value": price_str, "currency": currency},
                       "vat_code": 1,
                       "payment_mode": "full_payment",
                       "payment_subject": "service",
                   }],
               })

        if sbp_only:
            try:
                builder.set_payment_method_data({"type": "sbp"})
            except Exception:
                pass

        loop = asyncio.get_event_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: Payment.create(builder.build(), idempotence)
            ),
            timeout=10.0,
        )

        if not (response.confirmation and response.confirmation.confirmation_url):
            logger.error("[PAYMENT] YooKassa не вернула confirmation_url")
            return None

        await db_helpers.add_payment(
            payment_id=response.id,
            telegram_id=user_id,
            amount=amount,
            currency=currency,
            metadata_json=json.dumps(meta, ensure_ascii=False),
        )
        logger.info(
            f"[PAYMENT] YooKassa платёж создан: {response.id}, "
            f"user={user_id}, type={registration_type}"
        )
        return response.id, response.confirmation.confirmation_url

    except Exception as e:
        logger.error(f"[PAYMENT] YooKassa ошибка: {type(e).__name__}: {e}")
        return None


__all__ = [
    "create_yookassa_payment_shared",
]
