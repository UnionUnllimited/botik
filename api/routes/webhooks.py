"""Вебхуки платёжных провайдеров.

Отвечаем 200 только после успешного коммита в БД: провайдер повторит
уведомление, если мы вернём ошибку или не ответим за 60 секунд.
"""

from __future__ import annotations

import hmac
import ipaddress
from typing import Any

import httpx
import orjson
import structlog
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import client_ip, get_session
from core import texts
from core.config import settings
from core.enums import PaymentProviderName, PaymentStatus
from core.notifications import notify_admins
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
    ours = provider.verify_webhook(headers, body)
    if not ours and not _is_partners(headers):
        # У провайдера нет HMAC-подписи: подлинность подтверждают заголовки
        # X-MerchantId/X-Secret, которые он присылает обратно.
        log.warning("webhook.auth_failed", ip=ip)
        return Response(status_code=401)

    try:
        data: dict[str, Any] = orjson.loads(body)
    except orjson.JSONDecodeError:
        log.warning("webhook.bad_json", ip=ip)
        return Response(status_code=400)

    if not ours:
        # Реквизиты бота: такой платёж нашим не бывает по определению,
        # искать его у себя незачем — передаём и отвечаем «принято».
        return await _hand_over(session, body, headers, data)

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
        return await _hand_over(session, body, headers, data)

    # Сообщение клиенту кладётся в очередь строкой в базе, поэтому коммит
    # обязан быть последним: зависимость `get_session` сама не коммитит,
    # и всё, что записано после него, пропадало вместе с сессией.
    if applied and payment.status is PaymentStatus.SUCCEEDED:
        await notify_payment_result(session, payment)

    await session.commit()

    log.info(
        "webhook.processed",
        payment_id=payment.id,
        status=str(payment.status),
        applied=applied,
    )
    return Response(status_code=200)


def _is_partners(headers: dict[str, str]) -> bool:
    """Реквизиты бота, если он торгует под своим мерчантом.

    Признать колбэк чужим — это не пустить его дальше, а наоборот: своим
    платежом он после этого стать не может, и единственное, что с ним
    происходит, — пересылка боту, который проверит те же заголовки сам.
    """
    merchant = settings.platega.partner_merchant_id
    secret = settings.platega.partner_secret.get_secret_value()
    if not (merchant and secret):
        return False
    normalized = {key.lower(): value for key, value in headers.items()}
    return hmac.compare_digest(
        normalized.get("x-merchantid", ""), merchant
    ) and hmac.compare_digest(normalized.get("x-secret", ""), secret)


async def _hand_over(
    session: AsyncSession, body: bytes, headers: dict[str, str], data: dict[str, Any]
) -> Response:
    """Отдаёт колбэк боту и зовёт оператора, если не вышло.

    Провайдеру отвечаем «принято» в любом случае: повтор нам не поможет —
    чужой платёж мы и во второй раз не узнаем, а вечные повторы он
    в какой-то момент бросит совсем.
    """
    trouble = await _forward_to_partner(body, headers)
    if trouble:
        await notify_admins(
            texts.ADMIN_PARTNER_CALLBACK_LOST.format(
                transaction=data.get("id") or "—",
                amount=texts.money(data["amount"]) if data.get("amount") is not None else "—",
                status=data.get("status") or "—",
                reason=trouble,
            ),
            session=session,
        )
        await session.commit()
    return Response(status_code=200)


async def _forward_to_partner(body: bytes, headers: dict[str, str]) -> str:
    """Отдаёт чужой колбэк боту. Возвращает причину неудачи или пустую строку.

    Ошибку наверх не поднимает: провайдеру мы уже обязаны ответить 200, иначе
    он будет слать повторы вечно. Но и проглатывать её нельзя — у бота для
    этого провайдера опроса статуса нет («только webhook»), поэтому не дошло
    уведомление значит клиент заплатил и не получил ничего. Причину отдаём
    зовущему: он позовёт оператора.

    Заголовки подлинности передаём: бот проверяет их так же, как мы.
    Остальные (Host, Content-Length) выбрасываем — их подставит клиент.
    """
    url = settings.platega.partner_callback_url.strip()
    if not url:
        log.warning("webhook.partner_url_missing")
        return "адрес бота не задан"

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
        return f"бот не ответил ({exc.__class__.__name__})"
    if response.status_code >= 400:
        # Ответ бота — единственное подтверждение, что он уведомление принял.
        log.warning("webhook.forward_rejected", url=url, status=response.status_code)
        return f"бот ответил {response.status_code}"
    log.info("webhook.forwarded", url=url, status=response.status_code)
    return ""
