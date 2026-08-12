"""Создание и проверка платежей в YooMoney (Quickpay).

Ссылка на оплату собирается локально (``https://yoomoney.ru/quickpay/confirm.xml``
с query-параметрами по документации YooMoney). Библиотека ``yoomoney.Quickpay``
делает лишний HTTP-запрос к ЮMoney при инициализации — на части хостингов он
обрывается (``RemoteProtocolError``), поэтому запросы не используем. Платёж
подтверждается одним из трёх способов:

1. Webhook от YooMoney (см. ``yoomoney_webhook_handler`` в main.py).
   Подпись HTTP-уведомления:
     • Новый формат — параметр ``sign``: HMAC-SHA256 в HEX от
       URL-кодированной строки всех параметров (кроме ``sign``),
       отсортированных по алфавиту. Действует с 18 мая 2026.
     • Старый формат — параметр ``sha1_hash``: SHA-1 от
       строки фиксированных полей. Перестанет приходить с 18 мая 2026.
   Поддерживаются оба, чтобы переход прошёл без даунтайма.

2. Опрос API ``operation-history`` по нашему label (fallback, если
   подпись не сошлась — иногда у YooMoney меняется формула).

Именование payment_id (``YOOMONEY_<scope>_<ts>_<user>...``) генерируется
вызывающим кодом, потому что YooMoney не возвращает свой id транзакции
до получения операции.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Mapping, Optional
from urllib.parse import quote, urlencode

import httpx

import db_helpers

logger = logging.getLogger(__name__)

_QUICKPAY_CONFIRM = "https://yoomoney.ru/quickpay/confirm.xml"


def build_yoomoney_quickpay_url(
    *,
    receiver: str,
    targets: str,
    sum_amount: float,
    label: str,
    payment_type: str = "SB",
    quickpay_form: str = "shop",
) -> str:
    """Готовая URL формы «Магазин» / Quickpay без сетевых вызовов."""
    params = {
        "receiver": receiver.strip(),
        "quickpay-form": quickpay_form,
        "targets": targets,
        "paymentType": payment_type,
        "sum": f"{float(sum_amount):.2f}",
        "label": label,
    }
    return _QUICKPAY_CONFIRM + "?" + urlencode(params, encoding="utf-8")


async def create_yoomoney_quickpay(
    *,
    account: str,
    telegram_id: int,
    payment_id: str,
    amount: float,
    currency: str,
    target_text: str,
    metadata: Optional[dict] = None,
) -> Optional[str]:
    """Создаёт Quickpay-форму YooMoney и сохраняет платёж в БД.

    Параметры:
        account       — номер счёта YooMoney (yoomoney_account из настроек).
        telegram_id   — кому продаём.
        payment_id    — наш внутренний id (например, ``YOOMONEY_<ts>_<uid>_<tar>``).
                        Используется как ``label`` инвойса.
        amount        — сумма (в рублях, для совместимости с остальными провайдерами).
        currency      — валюта для записи в БД (обычно 'RUB').
        target_text   — текст «назначения платежа», который видит пользователь.
        metadata      — словарь, который сохраним в payments.metadata_json.

    Возвращает redirect-url или None при ошибке.
    """
    if not account:
        logger.error("[PAYMENT] YooMoney: yoomoney_account не задан")
        return None

    try:
        redirect_url = build_yoomoney_quickpay_url(
            receiver=account,
            targets=target_text,
            sum_amount=amount,
            label=payment_id,
        )

        await db_helpers.add_payment(
            payment_id=payment_id,
            telegram_id=telegram_id,
            amount=float(amount),
            currency=currency,
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
        )
        logger.info(
            f"[PAYMENT] YooMoney Quickpay создан: {payment_id}, "
            f"user={telegram_id}, amount={amount} {currency}"
        )
        return redirect_url

    except Exception as e:
        logger.error(
            f"[PAYMENT] YooMoney ошибка сохранения платежа Quickpay: "
            f"{type(e).__name__}: {e}"
        )
        return None


async def verify_yoomoney_payment_via_api(
    label: str,
    operation_id: Optional[str],
    expected_amount: float,
    token: str,
) -> Optional[bool]:
    """Проверяет платёж напрямую через YooMoney API ``operation-history``.

    Используется как fallback к проверке подписи webhook'а: формат подписи
    ``sign`` (SHA-256) официально не задокументирован и периодически
    меняется на стороне ЮMoney, поэтому самый надёжный способ подтвердить
    факт оплаты — спросить об этом сам YooMoney.

    Возвращает:
      * True  — платёж найден и подтверждён (status=success, сумма совпадает).
      * False — платёж не найден / не подтверждён.
      * None  — нет токена или произошла ошибка сети/API (нельзя сделать вывод).
    """
    if not token:
        logger.warning(
            "[PAYMENT] YooMoney API: токен не передан — fallback-проверка недоступна"
        )
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://yoomoney.ru/api/operation-history",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"label": label, "type": "deposition", "records": "20"},
            )
    except Exception as e:
        logger.error(
            f"[PAYMENT] YooMoney API: ошибка сети при запросе operation-history: {e}"
        )
        return None

    if resp.status_code != 200:
        logger.warning(
            f"[PAYMENT] YooMoney API: operation-history вернул HTTP "
            f"{resp.status_code}: {resp.text[:300]}"
        )
        return None

    try:
        data = resp.json()
    except Exception as e:
        logger.error(
            f"[PAYMENT] YooMoney API: невалидный JSON в ответе: {e}; "
            f"тело: {resp.text[:300]}"
        )
        return None

    if data.get("error"):
        logger.warning(
            f"[PAYMENT] YooMoney API: API вернул ошибку: {data.get('error')}"
        )
        return None

    operations = data.get("operations") or []
    expected_amount_f = float(expected_amount or 0)
    expected_min = expected_amount_f * 0.97  # та же логика, что и в webhook (комиссия до 3%)

    for op in operations:
        op_status = (op.get("status") or "").lower()
        op_id = str(op.get("operation_id") or "")
        op_amount = float(op.get("amount") or 0)
        op_label = op.get("label") or ""

        if op_label != label or op_status != "success":
            continue
        if operation_id and op_id and str(operation_id) != op_id:
            # Другая операция с тем же label (маловероятно, но возможно
            # при повторной оплате) — пропускаем.
            continue
        if op_amount + 0.01 < expected_min:
            logger.warning(
                f"[PAYMENT] YooMoney API: сумма {op_amount} < "
                f"минимально допустимой {expected_min} для label={label}"
            )
            continue
        return True

    logger.warning(
        f"[PAYMENT] YooMoney API: операция не найдена (label={label}, "
        f"op_id={operation_id}, всего операций в ответе: {len(operations)})"
    )
    return False


# ── Подпись webhook'а ────────────────────────────────────────────────────────
def _build_sign_string(form_data: Mapping[str, Any], exclude_keys: set) -> str:
    """Собирает каноническую строку для HMAC-SHA256 из новой документации.

    Алгоритм (https://yoomoney.ru/docs/wallet/using-api/notifications/p2p-incoming):
    1. Извлечь параметры уведомления.
    2. Удалить параметр ``sign`` (он не участвует в формировании подписи).
    3. Отсортировать оставшиеся параметры по алфавиту (A-Z).
    4. Применить URL-кодирование (UTF-8, RFC 3986) к значениям.
    5. Объединить параметры в формат ``key=value``, разделяя ``&``.
       Пустые значения остаются как ``key=`` (без пробелов).
    """
    keys = sorted({k for k in form_data.keys() if k not in exclude_keys})
    parts = []
    for k in keys:
        v = form_data.get(k, "")
        if v is None:
            v = ""
        # quote(safe='') кодирует ВСЕ символы кроме alphanum и
        # `_.-~`, как требует RFC 3986 для "unreserved characters".
        parts.append(f"{k}={quote(str(v), safe='')}")
    return "&".join(parts)


def _verify_sign_hmac256(form_data: Mapping[str, Any], secret: str) -> bool:
    """Новый формат: HMAC-SHA256 в HEX, нижний регистр."""
    received = (form_data.get("sign") or "").strip().lower()
    if not received:
        return False
    sign_str = _build_sign_string(form_data, exclude_keys={"sign"})
    computed = hmac.new(
        secret.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().lower()
    if hmac.compare_digest(computed, received):
        logger.info("[PAYMENT] YooMoney signature: HMAC-SHA256 OK")
        return True
    logger.warning(
        "[PAYMENT] YooMoney signature: HMAC-SHA256 mismatch "
        "(computed=%s, received=%s, sign_str=%s)",
        computed, received, sign_str,
    )
    return False


def _verify_sha1_legacy(form_data: Mapping[str, Any], secret: str) -> bool:
    """Старый формат: SHA-1 от фиксированной строки полей.

    Перестанет приходить с 18 мая 2026 — оставляем для совместимости
    переходного периода.
    """
    received = (form_data.get("sha1_hash") or "").strip().lower()
    if not received or len(received) != 40:
        return False
    params = [
        form_data.get("notification_type", ""),
        form_data.get("operation_id", ""),
        form_data.get("amount", ""),
        form_data.get("currency", ""),
        form_data.get("datetime", ""),
        form_data.get("sender", ""),
        form_data.get("codepro", ""),
        secret,
        form_data.get("label", ""),
    ]
    sign_str = "&".join("" if p is None else str(p) for p in params)
    computed = hashlib.sha1(sign_str.encode("utf-8")).hexdigest()
    if computed == received:
        logger.info("[PAYMENT] YooMoney signature: SHA-1 OK (legacy)")
        return True
    logger.warning(
        "[PAYMENT] YooMoney signature: SHA-1 mismatch (computed=%s, received=%s)",
        computed, received,
    )
    return False


def verify_yoomoney_signature(
    form_data: Mapping[str, Any],
    secret: str,
) -> bool:
    """Проверяет подпись HTTP-уведомления YooMoney.

    Поддерживаются оба формата (см. модульный docstring):
      • новый ``sign`` — HMAC-SHA256;
      • старый ``sha1_hash`` — SHA-1 (deprecated, до 18 мая 2026).

    При наличии обоих параметров приоритет у нового. Если подпись не
    сошлась ни в одном формате — возвращает False; вызывающий код может
    дополнительно подтвердить факт оплаты через
    :func:`verify_yoomoney_payment_via_api`.
    """
    if not secret:
        logger.warning(
            "[PAYMENT] YooMoney signature: секрет не задан "
            "(yoomoney_notification_secret) — подпись не проверена"
        )
        return False

    has_sign = bool((form_data.get("sign") or "").strip())
    has_sha1 = bool((form_data.get("sha1_hash") or "").strip())
    if not has_sign and not has_sha1:
        logger.warning(
            "[PAYMENT] YooMoney signature: подпись отсутствует "
            "(нет ни 'sign', ни 'sha1_hash')"
        )
        return False

    if has_sign and _verify_sign_hmac256(form_data, secret):
        return True
    if has_sha1 and _verify_sha1_legacy(form_data, secret):
        return True
    return False


__all__ = [
    "build_yoomoney_quickpay_url",
    "create_yoomoney_quickpay",
    "verify_yoomoney_payment_via_api",
    "verify_yoomoney_signature",
]
