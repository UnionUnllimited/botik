"""Что молча не работает, если настройку забыли.

Проверка базы и Redis при старте уже есть: без них ничего не поднимется, и
это видно сразу. Хуже те настройки, без которых всё поднимается и выглядит
живым, а магазин при этом не работает:

* нет канала тревог — предупреждения уходят в никуда, и молчащий роутер вы
  узнаёте от клиента;
* не настроен приём денег — покупатель доходит до оплаты и упирается;
* приложение закрыто списком — покупатель не откроет витрину вовсе.

Ни одно из этого не повод не стартовать: сервер может подниматься до того,
как дописали `.env`, и падать в перезапуск из-за пустой строки хуже, чем
работать наполовину. Поэтому не исключение, а громкая запись в журнале —
её видно и в `docker compose logs`, и в Sentry.
"""

from __future__ import annotations

import structlog

from core.config import settings
from core.enums import PaymentProviderName
from core.payments import get_provider

log = structlog.get_logger("preflight")


def sale_blockers() -> list[str]:
    """Чего не хватает, чтобы продать роутер незнакомому человеку.

    Пустой список — всё на месте. Порядок от «совсем нельзя продавать» к
    «продать можно, но вы не узнаете о беде».
    """
    missing: list[str] = []

    if not settings.miniapp.is_configured:
        missing.append(
            "приложение не открывается ни у кого: задайте MINIAPP_OPEN_TO_ALL=1 "
            "или перечислите MINIAPP_ALLOWED_TG_IDS"
        )

    try:
        paid = get_provider(PaymentProviderName.PLATEGA).is_configured
    except Exception as exc:  # noqa: BLE001 — провайдера может не быть вовсе
        log.warning("preflight.provider_unreadable", error=str(exc))
        paid = False
    if not paid:
        missing.append("приём денег не настроен: PLATEGA_MERCHANT_ID и PLATEGA_SECRET")

    if not (settings.bot.alerts_chat_id or settings.bot.owner_id):
        missing.append(
            "тревоги уходят в никуда: задайте BOT_ALERTS_CHAT_ID или BOT_OWNER_ID"
        )

    return missing


def report(service: str) -> list[str]:
    """Пишет в журнал, чего не хватает. Возвращает тот же список."""
    missing = sale_blockers()
    if not missing:
        log.info("preflight.ready", service=service)
        return missing
    # Уровень «ошибка» намеренно: предупреждений при старте много, и это
    # затерялось бы среди них — а это единственная запись, из-за которой
    # магазин стоит, пока все графики зелёные.
    log.error("preflight.not_ready", service=service, missing=missing)
    return missing
