"""Вебхуки платёжных провайдеров.

Отвечаем 200 только после успешного коммита в БД: провайдер повторит
уведомление, если мы вернём ошибку или не ответим за 60 секунд.
"""

from __future__ import annotations

import ipaddress
from typing import Any

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
        # Платёж не найден: отвечаем 200, иначе провайдер будет слать повторы вечно.
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
