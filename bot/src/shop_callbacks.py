"""Приём чужих уведомлений об оплате из очереди основного приложения.

Платёжный провайдер шлёт колбэки по одному адресу на мерчанта, и адрес этот
магазина. Наши платежи — за подписку — он там не узнаёт: у него в базе их нет.
Раньше магазин передавал их нам HTTP-запросом, и это не работало.

Не работало не по недосмотру, а по устройству: магазин живёт в контейнере,
мы — службой на хосте и слушаем `127.0.0.1:8081`. Для процесса внутри
контейнера `127.0.0.1` — это он сам. Публичного адреса у нас нет, а открывать
наши вебхуки в интернет ради этого нельзя: защита там только заголовками
мерчанта.

Поэтому ходим мы. Это направление работает всегда — и переживает пересоздание
контейнера, и смену прокси, и переезд адреса.

Забранный колбэк отдаём своему же обработчику, по петле. Разбирать его здесь
значило бы завести вторую копию логики зачисления: она разъедется с первой
на первой же правке, и разъедется молча.
"""

from __future__ import annotations

import asyncio

import aiohttp
from loguru import logger

from src import shop_api

POLL_INTERVAL_SEC = 10
"""Столько же, сколько у очереди сообщений. Оплату ждёт живой человек,
но секунда против десяти на включении подписки незаметна, а частый опрос
чужого сервиса — лишний шум в его логах."""

BATCH = 10

ENDPOINTS = {
    "platega": "http://127.0.0.1:8081/platega/callback",
}
"""Куда отдать колбэк каждого провайдера. Адрес петлевой: мы и обработчик —
один процесс, и снаружи он не виден."""

TIMEOUT_SEC = 20
"""Обработчик успевает сходить в панель и выдать подписку. Пятнадцать секунд
там встречаются, поэтому порог с запасом."""


async def deliver_once() -> int:
    """Одна пачка. Возвращает число принятых колбэков."""
    data, error = await shop_api.partner_callbacks(limit=BATCH)
    if error:
        logger.debug(f"[PARTNER] очередь недоступна: {error}")
        return 0

    taken = 0
    for item in data.get("callbacks", []):
        callback_id = item.get("id")
        url = ENDPOINTS.get(str(item.get("provider", "")).lower())
        if not url:
            # Провайдер, которого мы не умеем принимать. Отчитываемся отказом,
            # а не молчим: молчание вернуло бы его в следующую пачку, и так
            # до конца попыток — а причина всё это время осталась бы в нашем
            # коде, а не в связи.
            await shop_api.partner_callback_ack(
                callback_id, ok=False, error=f"неизвестный провайдер {item.get('provider')}"
            )
            logger.warning(f"[PARTNER] колбэк {callback_id}: провайдер не поддержан")
            continue

        headers = {str(key): str(value) for key, value in (item.get("headers") or {}).items()}
        body = str(item.get("body") or "")

        try:
            timeout = aiohttp.ClientTimeout(total=TIMEOUT_SEC)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=body.encode("utf-8"), headers=headers) as answer:
                    status = answer.status
                    if status >= 400:
                        raise RuntimeError(f"обработчик ответил {status}")
        except Exception as exc:  # noqa: BLE001 — один колбэк не должен ронять круг
            await shop_api.partner_callback_ack(callback_id, ok=False, error=str(exc)[:300])
            logger.warning(f"[PARTNER] колбэк {callback_id} не принят: {exc}")
            continue

        await shop_api.partner_callback_ack(callback_id, ok=True)
        taken += 1
        logger.info(
            f"[PARTNER] принят колбэк {callback_id}, транзакция {item.get('transaction_id')}"
        )

    return taken


async def callbacks_loop() -> None:
    """Вечный цикл. Любая ошибка — пауза и следующий круг: очередь подождёт."""
    logger.info("[PARTNER] приём чужих колбэков из очереди магазина запущен")
    while True:
        try:
            await deliver_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — цикл переживает что угодно
            logger.error(f"[PARTNER] круг приёма упал: {exc}")
        await asyncio.sleep(POLL_INTERVAL_SEC)


def start_partner_callbacks() -> asyncio.Task:
    return asyncio.create_task(callbacks_loop())
