"""Сообщения клиенту из процессов без диспетчера (api, worker).

Своего бота у нас нет: клиент разговаривает с ботом стороннего продукта,
и токен есть только у него. Слать напрямую мы не можем — и не должны,
даже если бы могли: сообщение от постороннего бота человек не узнает,
а половина клиентов его вовсе не получит.

Поэтому здесь очередь, а не отправка. Мы кладём готовый текст в таблицу
`notifications`, бот раз в несколько секунд забирает пачку через
`/api/v1/catalog/outbox`, отправляет своим токеном и отчитывается. Очередь
переживает и его перезапуск, и обрыв связи: напоминание об окончании подписки
не должно теряться из-за того, что бота в этот момент обновляли.

Кнопки — только ссылки. Callback обрабатывает конкретный бот, а он сменный:
кнопка с уехавшим обработчиком молча перестала бы работать.
"""

from __future__ import annotations

import structlog
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.models import Notification

log = structlog.get_logger("notifications")


def _buttons_of(markup: InlineKeyboardMarkup | None) -> list[dict[str, str]]:
    if markup is None:
        return []
    return [
        {"text": button.text, "url": button.url}
        for row in markup.inline_keyboard
        for button in row
        if button.url
    ]


async def send_message(
    chat_id: int | None,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    session: AsyncSession | None = None,
    kind: str = "",
) -> bool:
    """Ставит сообщение в очередь на отправку ботом.

    `False` значит «отправлять некому или некуда»: без сессии записать в очередь
    нельзя, а без chat_id — незачем. Успешная постановка ещё не доставка:
    результат появится в `notifications.sent_at` после того, как бот отчитается.
    """
    if not chat_id:
        return False
    if session is None:
        # Раньше здесь молча уходило в Telegram. Теперь без сессии сообщение
        # просто некуда деть, и терять его тихо нельзя.
        log.error("notify.no_session", chat_id=chat_id, text=text[:120])
        return False

    session.add(
        Notification(
            tg_id=chat_id,
            text=text,
            buttons=_buttons_of(reply_markup),
            kind=kind or "message",
        )
    )
    log.info("notify.queued", chat_id=chat_id, kind=kind or "message")
    return True


async def notify_admins(
    text: str,
    *,
    session: AsyncSession | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Служебное сообщение в админ-канал; если он не задан — владельцу."""
    chat_id = settings.bot.alerts_chat_id or settings.bot.owner_id
    if not chat_id:
        log.warning("notify.no_admin_chat", text=text[:120])
        return
    await send_message(chat_id, text, reply_markup=reply_markup, session=session, kind="admin")


async def close_bot() -> None:
    """Осталось ради вызова при остановке api: закрывать больше нечего."""
    return None
