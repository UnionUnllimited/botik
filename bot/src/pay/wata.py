"""Wata H2H API: платёжные ссылки и проверка подписи webhook.

Документация: https://wata.pro/api

Базовый URL API: ``https://api.wata.pro/api/h2h``

Аутентификация запросов к API: ``Authorization: Bearer <access token>``
(токен выпускается в ЛК мерчанта по терминалу).

Webhook: сырый JSON тела + заголовок ``X-Signature`` (RSA PKCS#1 v1.5, SHA-512).
Публичный ключ: ``GET .../public-key`` (поле ``value``, PEM).

Дальше: маршрут ``POST /wata/webhook`` в ``main.py``, создание ссылки в handlers,
в ``orderId`` передавать наш ``payment_id`` для сопоставления с БД.
"""
from __future__ import annotations

import base64
import json
import logging
import time
import uuid as py_uuid
from typing import Any, Mapping, Optional

import httpx
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import db_helpers

from .helpers import build_payment_metadata

logger = logging.getLogger(__name__)

WATA_API_ROOT = "https://api.wata.pro/api/h2h"
WATA_API_ROOT_PROD = WATA_API_ROOT

_PUBLIC_KEY_CACHE_TTL_SEC = 3600
_public_key_cached_at: float = 0.0
_public_key_pem: str = ""


async def fetch_wata_public_key_pem(*, force_refresh: bool = False) -> Optional[str]:
    """Загружает PEM публичного ключа для проверки ``X-Signature`` webhook."""
    global _public_key_cached_at, _public_key_pem
    now = time.monotonic()
    if (
        not force_refresh
        and _public_key_pem
        and now - _public_key_cached_at < _PUBLIC_KEY_CACHE_TTL_SEC
    ):
        return _public_key_pem

    url = f"{WATA_API_ROOT}/public-key"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers={"Accept": "application/json"})
            if r.status_code != 200:
                logger.warning("[WATA] public-key HTTP %s: %s", r.status_code, (r.text or "")[:300])
                return _public_key_pem or None
            data = r.json()
            pem = (data.get("value") or "").strip()
            if not pem or "BEGIN PUBLIC KEY" not in pem:
                logger.warning("[WATA] public-key: нет поля value PEM")
                return _public_key_pem or None
            _public_key_cached_at = now
            _public_key_pem = pem
            return pem
    except Exception as e:
        logger.warning("[WATA] public-key ошибка: %s", e)
        return _public_key_pem or None


def verify_wata_webhook_signature(
    raw_body: bytes,
    x_signature_header: Optional[str],
    public_key_pem: str,
) -> bool:
    """Проверяет подпись webhook: SHA512withRSA над **сырыми** байтами тела запроса."""
    if not raw_body or not x_signature_header or not public_key_pem:
        return False
    sig_b64 = x_signature_header.strip()
    try:
        pub = serialization.load_pem_public_key(
            public_key_pem.encode("utf-8"),
            backend=default_backend(),
        )
        signature = base64.b64decode(sig_b64, validate=True)
        pub.verify(signature, raw_body, padding.PKCS1v15(), hashes.SHA512())
        return True
    except Exception:
        return False


def decode_wata_webhook_json(raw_body: bytes) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    try:
        obj = json.loads(raw_body.decode("utf-8"))
        if isinstance(obj, dict):
            return obj, None
        return None, "payload is not a JSON object"
    except Exception as e:
        return None, str(e)


def _link_payload(
    *,
    amount: float,
    currency: str,
    order_id: Optional[str] = None,
    description: Optional[str] = None,
    link_type: Optional[str] = None,
    success_redirect_url: Optional[str] = None,
    fail_redirect_url: Optional[str] = None,
    expiration_datetime: Optional[str] = None,
    is_arbitrary_amount_allowed: Optional[bool] = None,
    arbitrary_amount_prompts: Optional[list[Any]] = None,
) -> dict[str, Any]:
    cur = (currency or "RUB").strip().upper()
    body: dict[str, Any] = {
        "amount": round(float(amount), 2),
        "currency": cur,
    }
    if order_id is not None:
        body["orderId"] = str(order_id)
    if description is not None:
        body["description"] = str(description)
    if link_type is not None:
        body["type"] = str(link_type)
    if success_redirect_url is not None:
        body["successRedirectUrl"] = success_redirect_url
    if fail_redirect_url is not None:
        body["failRedirectUrl"] = fail_redirect_url
    if expiration_datetime is not None:
        body["expirationDateTime"] = expiration_datetime
    if is_arbitrary_amount_allowed is not None:
        body["isArbitraryAmountAllowed"] = bool(is_arbitrary_amount_allowed)
    if arbitrary_amount_prompts is not None:
        body["arbitraryAmountPrompts"] = arbitrary_amount_prompts
    return body


async def create_wata_payment_link(
    access_token: str,
    *,
    amount: float,
    currency: str = "RUB",
    order_id: Optional[str] = None,
    description: Optional[str] = None,
    link_type: Optional[str] = None,
    success_redirect_url: Optional[str] = None,
    fail_redirect_url: Optional[str] = None,
    expiration_datetime: Optional[str] = None,
    is_arbitrary_amount_allowed: Optional[bool] = None,
    arbitrary_amount_prompts: Optional[list[Any]] = None,
    timeout: float = 65.0,
    extra_json: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Создаёт платёжную ссылку ``POST /links``.

    Возвращает словарь с ключами:
    - ``ok`` (bool)
    - при успехе: ``id``, ``url``, ``status``, ``raw`` (ответ API)
    - при ошибке: ``error``, ``http_status``, опционально ``raw``
    """
    tok = (access_token or "").strip()
    if not tok:
        return {"ok": False, "error": "access_token пуст", "http_status": 0}

    url = f"{WATA_API_ROOT}/links"
    payload = _link_payload(
        amount=amount,
        currency=currency,
        order_id=order_id,
        description=description,
        link_type=link_type,
        success_redirect_url=success_redirect_url,
        fail_redirect_url=fail_redirect_url,
        expiration_datetime=expiration_datetime,
        is_arbitrary_amount_allowed=is_arbitrary_amount_allowed,
        arbitrary_amount_prompts=arbitrary_amount_prompts,
    )
    if extra_json:
        for k, v in extra_json.items():
            if k not in payload:
                payload[k] = v

    headers = {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers=headers, json=payload)
            text = r.text or ""
            try:
                data = r.json()
            except Exception:
                data = None

            if r.status_code == 200 and isinstance(data, dict):
                pay_url = data.get("url")
                lid = data.get("id")
                if pay_url and lid:
                    return {
                        "ok": True,
                        "id": str(lid),
                        "url": str(pay_url),
                        "status": str(data.get("status") or ""),
                        "raw": data,
                    }
                return {
                    "ok": False,
                    "error": "в ответе нет url или id",
                    "http_status": r.status_code,
                    "raw": data,
                }

            err_msg = None
            if isinstance(data, dict):
                err = data.get("error")
                if isinstance(err, dict):
                    err_msg = err.get("message") or err.get("details") or json.dumps(err, ensure_ascii=False)
                elif err:
                    err_msg = str(err)
            if not err_msg:
                err_msg = text[:500] if text else f"HTTP {r.status_code}"

            return {
                "ok": False,
                "error": err_msg,
                "http_status": r.status_code,
                "raw": data if isinstance(data, dict) else None,
            }
    except httpx.TimeoutException:
        return {"ok": False, "error": "timeout", "http_status": 0}
    except Exception as e:
        logger.exception("[WATA] create link: %s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "http_status": 0}


async def create_wata_payment_shared(
    access_token: str,
    *,
    amount: float,
    tariff: dict,
    user_id: int,
    return_url: str,
    registration_type: str = "telegram",
    email: str = "",
    currency: str = "RUB",
    terminal_public_id: str = "",
    **extra,
) -> Optional[tuple[str, str]]:
    """Создаёт платёж Wata для обычной подписки (renewal/первая покупка).

    Используется и ботом, и сайтом. По образцу ``create_platega_payment_shared``.
    Метаданные собираются через ``build_payment_metadata`` — webhook Wata
    (``payment_type`` отсутствует или = ``subscription``) обработает их через
    ``process_successful_payment``.

    ``**extra`` — провайдер-специфичные ключи (``payment_type``,
    ``traffic_to_add_gb``, ``bot_payment_uuid`` и т.п.). Для Wata ничего
    дополнительного класть в meta не нужно — webhook читает ``orderId``
    и метаданные из БД.

    Возвращает ``(payment_id, pay_url)`` или ``None`` при ошибке.
    """
    try:
        tariff_id = tariff["id"]
        days = int(tariff.get("days") or 0)
        cur = str(currency or tariff.get("currency") or "RUB").upper()

        meta = build_payment_metadata(
            user_id=user_id,
            tariff_id=tariff_id,
            days=days,
            price=amount,
            limit_ip=int(tariff.get("limit_ip") or 0),
            registration_type=registration_type,
            email=email,
            cms_name="wata",
            payment_method="Wata",
            **extra,
        )

        payment_id = f"WATA_{py_uuid.uuid4()}"
        extra_json = {"publicId": terminal_public_id} if terminal_public_id else None

        description = f"Подписка: {tariff.get('name', '')}".strip() or "Оплата подписки"

        result = await create_wata_payment_link(
            access_token,
            amount=float(amount),
            currency=cur,
            order_id=payment_id,
            description=description,
            link_type="OneTime",
            success_redirect_url=return_url,
            fail_redirect_url=return_url,
            extra_json=extra_json,
        )

        if not result.get("ok"):
            logger.error(
                f"[PAYMENT] Wata shared: создание ссылки не удалось: "
                f"{result.get('error')}"
            )
            return None

        pay_url = result.get("url")
        if not pay_url:
            logger.error("[PAYMENT] Wata shared: пустой url в ответе")
            return None

        await db_helpers.add_payment(
            payment_id,
            user_id,
            float(amount),
            cur,
            json.dumps(meta, ensure_ascii=False),
        )
        logger.info(
            f"[PAYMENT] Wata shared платёж создан: {payment_id}, "
            f"user={user_id}, type={registration_type}"
        )
        return payment_id, str(pay_url)

    except Exception as e:
        logger.error(
            f"[PAYMENT] Wata shared ошибка: {type(e).__name__}: {e}",
            exc_info=True,
        )
        return None


async def create_wata_payment_device_upgrade(
    access_token: str,
    *,
    user_id: int,
    new_limit: int,
    upgrade_meta: dict,
    return_url: str,
    terminal_public_id: str = "",
) -> Optional[tuple[str, str]]:
    """Создаёт платёж Wata для расширения лимита устройств.

    ``upgrade_meta`` — результат ``device_upgrade_service.build_payment_metadata()``
    (содержит ``payment_type='device_limit_upgrade'``, ``new_limit``, ``old_limit``,
    ``price``, ``currency``, ``telegram_user_id`` и т.д.).

    Возвращает ``(payment_id, pay_url)`` или ``None`` при ошибке.
    Wata-webhook (``payment_type == 'device_limit_upgrade'``) сам применит
    апгрейд через ``process_successful_payment``.
    """
    try:
        amount = float(upgrade_meta.get("price") or 0)
        if amount <= 0:
            logger.error(f"[PAYMENT] device_upgrade Wata: неверная сумма {amount}")
            return None

        currency = str(upgrade_meta.get("currency") or "RUB").upper()

        meta = dict(upgrade_meta)
        meta["cms_name"] = "wata"
        meta["payment_method"] = "Wata"
        meta.setdefault("registration_type", "bot")
        meta.setdefault("telegram_user_id", int(user_id))

        payment_id = f"WATA_{py_uuid.uuid4()}"
        extra = {"publicId": terminal_public_id} if terminal_public_id else None

        result = await create_wata_payment_link(
            access_token,
            amount=amount,
            currency=currency,
            order_id=payment_id,
            description=f"Расширение лимита устройств до {new_limit}",
            link_type="OneTime",
            success_redirect_url=return_url,
            fail_redirect_url=return_url,
            extra_json=extra,
        )

        if not result.get("ok"):
            logger.error(
                f"[PAYMENT] device_upgrade Wata: создание ссылки не удалось: "
                f"{result.get('error')}"
            )
            return None

        pay_url = result.get("url")
        if not pay_url:
            logger.error("[PAYMENT] device_upgrade Wata: пустой url в ответе")
            return None

        await db_helpers.add_payment(
            payment_id,
            user_id,
            float(amount),
            currency,
            json.dumps(meta, ensure_ascii=False),
        )
        logger.info(
            f"[PAYMENT] device_upgrade Wata создан: {payment_id}, "
            f"user={user_id}, new_limit={new_limit}"
        )
        return payment_id, str(pay_url)

    except Exception as e:
        logger.error(
            f"[PAYMENT] device_upgrade Wata ошибка: {type(e).__name__}: {e}",
            exc_info=True,
        )
        return None


async def create_wata_payment_traffic_renewal(
    access_token: str,
    *,
    user_id: int,
    price: float,
    traffic_to_add_gb: float,
    return_url: str,
    currency: str = "RUB",
    tariff_id: Optional[int] = None,
    terminal_public_id: str = "",
    registration_type: str = "bot",
    email: str = "",
) -> Optional[tuple[str, str]]:
    """Создаёт платёж Wata для докупки трафика (`payment_type='traffic_renewal'`).

    После оплаты webhook Wata вызовет ``process_successful_payment``,
    который добавит ``traffic_to_add_gb`` GB к лимиту Remnawave-подписки
    **без** изменения ``subscription_end_date``.

    Возвращает ``(payment_id, pay_url)`` или ``None`` при ошибке.
    """
    try:
        amount = float(price)
        if amount <= 0:
            logger.error(f"[PAYMENT] traffic_renewal Wata: неверная сумма {amount}")
            return None

        cur = str(currency or "RUB").upper()

        meta: dict[str, Any] = {
            "payment_type": "traffic_renewal",
            "telegram_user_id": int(user_id),
            "price": amount,
            "traffic_to_add_gb": float(traffic_to_add_gb),
            "registration_type": registration_type,
            "cms_name": "wata",
            "payment_method": "Wata",
        }
        if tariff_id:
            meta["tariff_id"] = int(tariff_id)
        if email:
            meta["email"] = email

        payment_id = f"WATA_{py_uuid.uuid4()}"
        extra = {"publicId": terminal_public_id} if terminal_public_id else None

        result = await create_wata_payment_link(
            access_token,
            amount=amount,
            currency=cur,
            order_id=payment_id,
            description=f"Продление трафика (+{traffic_to_add_gb} GB)",
            link_type="OneTime",
            success_redirect_url=return_url,
            fail_redirect_url=return_url,
            extra_json=extra,
        )

        if not result.get("ok"):
            logger.error(
                f"[PAYMENT] traffic_renewal Wata: создание ссылки не удалось: "
                f"{result.get('error')}"
            )
            return None

        pay_url = result.get("url")
        if not pay_url:
            logger.error("[PAYMENT] traffic_renewal Wata: пустой url в ответе")
            return None

        await db_helpers.add_payment(
            payment_id,
            user_id,
            float(amount),
            cur,
            json.dumps(meta, ensure_ascii=False),
        )
        logger.info(
            f"[PAYMENT] traffic_renewal Wata создан: {payment_id}, "
            f"user={user_id}, +{traffic_to_add_gb} GB"
        )
        return payment_id, str(pay_url)

    except Exception as e:
        logger.error(
            f"[PAYMENT] traffic_renewal Wata ошибка: {type(e).__name__}: {e}",
            exc_info=True,
        )
        return None


__all__ = [
    "WATA_API_ROOT",
    "WATA_API_ROOT_PROD",
    "fetch_wata_public_key_pem",
    "verify_wata_webhook_signature",
    "decode_wata_webhook_json",
    "create_wata_payment_link",
    "create_wata_payment_shared",
    "create_wata_payment_device_upgrade",
    "create_wata_payment_traffic_renewal",
]
