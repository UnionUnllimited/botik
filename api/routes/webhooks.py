"""Вебхуки платёжных провайдеров.

Отвечаем 200 только после успешного коммита в БД: провайдер повторит
уведомление, если мы вернём ошибку или не ответим за 60 секунд.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import httpx
import orjson
import structlog
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import client_ip, get_session
from core.config import settings
from core.enums import PaymentProviderName, PaymentStatus
from core.payments import get_provider
from core.services import payments as payment_service
from core.services.notifier import notify_payment_result

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = structlog.get_logger("api.webhooks")


def _ip_allowed(ip: str, allowed: list[str]) -> bool:
    if not allowed:
        return True
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowed:
        try:
            if "/" in entry:
                if address in ipaddress.ip_network(entry, strict=False):
                    return True
            elif address == ipaddress.ip_address(entry):
                return True
        except ValueError:
            log.warning("webhook.bad_allowed_ip", entry=entry)
    return False


@router.post("/platega", summary="Уведомление PLATEGA об изменении статуса транзакции")
async def platega_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    body = await request.body()
    headers = dict(request.headers)
    ip = client_ip(request)

    if not _ip_allowed(ip, settings.platega.allowed_ips):
        log.warning("webhook.ip_rejected", ip=ip)
        return Response(status_code=403)

    provider = get_provider(PaymentProviderName.PLATEGA)
    if not provider.verify_webhook(headers, body):
        # У провайдера нет HMAC-подписи: подлинность подтверждают заголовки
        # X-MerchantId/X-Secret, которые он присылает обратно.
        log.warning("webhook.auth_failed", ip=ip)
        return Response(status_code=401)

    try:
        data: dict[str, Any] = orjson.loads(body)
    except orjson.JSONDecodeError:
        log.warning("webhook.bad_json", ip=ip)
        return Response(status_code=400)

    payment, applied = await payment_service.handle_webhook(
        session,
        provider_name=PaymentProviderName.PLATEGA,
        data=data,
    )
    if payment is None:
        # Платёж не наш — значит бота: железо продаём мы, подписку он.
        # Провайдер шлёт уведомления по одному адресу на мерчанта, поэтому
        # публичный приёмник один, и чужое он передаёт дальше как есть.
        # Раньше здесь стоял голый 200: клиент платил за подписку, а она
        # не включалась, потому что бот об оплате не узнавал.
        await _forward_to_partner(body, headers)
        return Response(status_code=200)

    await session.commit()

    if applied and payment.status is PaymentStatus.SUCCEEDED:
        await notify_payment_result(session, payment)

    log.info(
        "webhook.processed",
        payment_id=payment.id,
        status=str(payment.status),
        applied=applied,
    )
    return Response(status_code=200)


async def _forward_to_partner(body: bytes, headers: dict[str, str]) -> None:
    """Отдаёт чужой колбэк боту. Ошибку не поднимает наверх.

    Провайдеру мы уже ответили 200 — и обязаны ответить, иначе он будет слать
    повторы вечно. Если бот в этот момент перезапускается, уведомление
    потеряется: у него для таких случаев есть свой опрос статуса платежей.
    Ронять из-за этого ответ провайдеру нельзя.

    Заголовки подлинности передаём: бот проверяет их так же, как мы.
    Остальные (Host, Content-Length) выбрасываем — их подставит клиент.
    """
    url = settings.platega.partner_callback_url.strip()
    if not url:
        # Молча терять чужой колбэк нельзя: клиент заплатил, подписка
        # не включилась, и в журнале об этом не будет ни строчки. Адрес
        # задаётся PLATEGA_PARTNER_CALLBACK_URL.
        log.warning("webhook.partner_url_missing")
        return

    passthrough = {
        key: value
        for key, value in headers.items()
        if key.lower() in ("content-type", "x-merchantid", "x-secret")
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, content=body, headers=passthrough)
    except httpx.HTTPError as exc:
        log.warning("webhook.forward_failed", url=url, error=str(exc))
        return
    log.info("webhook.forwarded", url=url, status=response.status_code)
