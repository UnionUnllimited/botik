"""Middleware: блокирует клиентов при включённом сервисном режиме."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, TelegramObject

from src.maintenance.service import (
    get_maintenance_message,
    is_admin_user,
    is_maintenance_enabled,
)

logger = logging.getLogger(__name__)

_PRECHECKOUT_ERROR_MAX = 255


def _user_id_from_event(event: TelegramObject) -> Optional[int]:
    if isinstance(event, CallbackQuery):
        return event.from_user.id if event.from_user else None
    if isinstance(event, Message):
        return event.from_user.id if event.from_user else None
    if isinstance(event, PreCheckoutQuery):
        return event.from_user.id if event.from_user else None
    from_user = getattr(event, 'from_user', None)
    return from_user.id if from_user else None


class MaintenanceMiddleware(BaseMiddleware):
    """
    При bot_maintenance_enabled=1 отвечает клиентам заглушкой и не вызывает handlers.
    Админы (admin_ids) не ограничиваются.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not is_maintenance_enabled():
            return await handler(event, data)

        user_id = _user_id_from_event(event)
        if user_id and is_admin_user(user_id):
            return await handler(event, data)

        text = get_maintenance_message()
        alert_text = text if len(text) <= _PRECHECKOUT_ERROR_MAX else text[: _PRECHECKOUT_ERROR_MAX - 1] + '…'

        if isinstance(event, CallbackQuery):
            try:
                await event.answer(alert_text, show_alert=True)
            except Exception as e:
                logger.debug('[MAINTENANCE] callback answer failed: %s', e)
            return

        if isinstance(event, Message):
            # Оплата Stars могла начаться до включения режима — не теряем платёж.
            if event.successful_payment:
                return await handler(event, data)
            try:
                await event.answer(text)
            except Exception as e:
                logger.debug('[MAINTENANCE] message answer failed: %s', e)
            return

        if isinstance(event, PreCheckoutQuery):
            try:
                await event.answer(ok=False, error_message=alert_text)
            except Exception as e:
                logger.debug('[MAINTENANCE] pre_checkout answer failed: %s', e)
            return

        return await handler(event, data)
