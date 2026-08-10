"""Платёжный слой проекта.

Структура:
    src/pay/
    ├── partner.py    — credit_partner_and_referral (общая логика начислений)
    ├── helpers.py    — build_payment_metadata
    ├── yookassa.py   — create_yookassa_payment_*
    ├── platega.py    — create_platega_payment_*
    ├── cryptobot.py  — fiat-инвойсы, webhook (подпись, resolve payment_id), setWebhook
    ├── yoomoney.py   — create_yoomoney_quickpay, verify_yoomoney_payment_via_api
    ├── wata.py       — create_wata_payment_link, verify webhook (Wata H2H, prod API only)
    └── tgstar.py     — rub_to_stars

Импортируйте из конкретного модуля или из пакета напрямую:

    from src.pay import (
        create_yookassa_payment_shared,
        create_platega_payment_device_upgrade,
        create_yoomoney_quickpay,
        rub_to_stars,
        credit_partner_and_referral,
    )
"""
from .partner import credit_partner_and_referral
from .helpers import build_payment_metadata
from .cryptobot import (
    cryptobot_fiat_invoice_body,
    create_cryptobot_invoice_device_upgrade,
    cryptobot_set_webhook_url,
    resolve_cryptobot_payment_row,
    verify_cryptobot_webhook_signature,
)
from .tgstar import rub_to_stars
from .yookassa import (
    create_yookassa_payment_shared,
    create_yookassa_payment_device_upgrade,
)
from .platega import (
    create_platega_payment_shared,
    create_platega_payment_device_upgrade,
)
from .yoomoney import (
    create_yoomoney_quickpay,
    create_yoomoney_quickpay_device_upgrade,
    verify_yoomoney_payment_via_api,
    verify_yoomoney_signature,
)
from .wata import (
    create_wata_payment_link,
    create_wata_payment_shared,
    create_wata_payment_device_upgrade,
    create_wata_payment_traffic_renewal,
    fetch_wata_public_key_pem,
    verify_wata_webhook_signature,
    decode_wata_webhook_json,
)

__all__ = [
    "credit_partner_and_referral",
    "build_payment_metadata",
    "cryptobot_fiat_invoice_body",
    "create_cryptobot_invoice_device_upgrade",
    "cryptobot_set_webhook_url",
    "resolve_cryptobot_payment_row",
    "verify_cryptobot_webhook_signature",
    "rub_to_stars",
    "create_yookassa_payment_shared",
    "create_yookassa_payment_device_upgrade",
    "create_platega_payment_shared",
    "create_platega_payment_device_upgrade",
    "create_yoomoney_quickpay",
    "create_yoomoney_quickpay_device_upgrade",
    "verify_yoomoney_payment_via_api",
    "verify_yoomoney_signature",
    "create_wata_payment_link",
    "create_wata_payment_shared",
    "create_wata_payment_device_upgrade",
    "create_wata_payment_traffic_renewal",
    "fetch_wata_public_key_pem",
    "verify_wata_webhook_signature",
    "decode_wata_webhook_json",
]
