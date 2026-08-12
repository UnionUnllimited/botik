"""CryptoBot fiat-инвойсы.

Все цены проекта в RUB. CryptoBot принимает fiat-инвойсы
(`currency_type='fiat', fiat='RUB'`) и сам конвертит в крипту по
своему курсу.

Здесь живут:
- `cryptobot_fiat_invoice_body(amount)` — формирует тело инвойса в RUB,
  результат объединять с описанием/payload через `**`.
- `verify_cryptobot_webhook_signature` — проверка подписи HTTP-уведомления Crypto Pay.
- `resolve_cryptobot_payment_row` — наш ``payment_id`` по ``invoice_id`` из webhook/API.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Optional, Sequence, Tuple

import httpx

import db_helpers

logger = logging.getLogger(__name__)


def cryptobot_payment_id_candidates(invoice_id: int) -> Sequence[str]:
    """Варианты полей ``payments.payment_id`` для одного числового invoice_id."""
    s = str(int(invoice_id))
    return (f"CRYPTO_{s}", f"CRYPTO_TRAFFIC_RENEWAL_{s}", f"CRYPTO_DEVICE_UPGRADE_{s}")


def verify_cryptobot_webhook_signature(
    api_token: str,
    raw_body: bytes,
    signature_header: Optional[str],
) -> bool:
    """Подпись Crypto Pay: ``HMAC_SHA256( SHA256(api_token), raw_body )`` в HEX.

    Заголовок: ``crypto-pay-api-signature`` (регистр может отличаться у прокси).
    """
    if not api_token or not raw_body or not signature_header:
        return False
    sig_in = signature_header.strip().lower()
    try:
        secret = hashlib.sha256(api_token.encode("utf-8")).digest()
        computed = hmac.new(secret, raw_body, hashlib.sha256).hexdigest().lower()
        return hmac.compare_digest(computed, sig_in)
    except Exception:
        return False


async def resolve_cryptobot_payment_row(
    invoice_id: int,
    merchant_payload_from_invoice: str = "",
) -> Tuple[Optional[str], Optional[Any], str]:
    """Ищет платёж в БД по invoice_id и возвращает эффективный строковый payload мерчанта."""
    mp = (merchant_payload_from_invoice or "").strip()
    for pid in cryptobot_payment_id_candidates(invoice_id):
        row = await db_helpers.get_payment(pid)
        if not row:
            continue
        ps = mp
        if not ps and row[6]:
            try:
                meta = json.loads(row[6])
                ps = (meta.get("payload") or "").strip()
            except Exception:
                ps = ""
        return pid, row, ps
    return None, None, ""


async def cryptobot_set_webhook_url(api_token: str, webhook_url: str) -> tuple[bool, str]:
    """POST ``/api/setWebhook`` — при старте бота, если задан публичный URL."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://pay.crypt.bot/api/setWebhook",
                headers={"Crypto-Pay-API-Token": api_token},
                json={"url": webhook_url},
            )
            body = (r.text or "")[:800]
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}: {body}"
            try:
                data = r.json()
            except Exception:
                return False, body
            if data.get("ok"):
                return True, "ok"
            return False, str(data.get("error") or data)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def cryptobot_fiat_invoice_body(amount: float) -> dict:
    """Тело fiat-инвойса CryptoBot для оплаты в рублях.

    Минимальный набор полей: CryptoBot сам выберет доступные крипто-активы
    из настроек мерчанта и сконвертит по своему курсу. Передавать
    ``accepted_assets`` НЕ НУЖНО — это вызывает 400 Bad Request у части
    аккаунтов; пусть мерчант рулит ассетами в настройках @CryptoBot.

    Возвращает dict, который нужно объединить (через ``**``) с другими
    полями инвойса (description, payload, paid_btn_*).
    """
    # CryptoBot принимает amount как строку с десятичной точкой.
    amount_str = f"{float(amount or 0):.2f}".rstrip("0").rstrip(".") or "0"
    return {
        "currency_type": "fiat",
        "fiat": "RUB",
        "amount": amount_str,
    }


__all__ = [
    "cryptobot_fiat_invoice_body",
    "cryptobot_payment_id_candidates",
    "verify_cryptobot_webhook_signature",
    "resolve_cryptobot_payment_row",
    "cryptobot_set_webhook_url",
]
