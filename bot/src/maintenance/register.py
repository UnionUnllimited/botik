"""Подключение middleware сервисного режима к Dispatcher."""
from __future__ import annotations

from aiogram import Dispatcher

from src.maintenance.middleware import MaintenanceMiddleware

_middleware: MaintenanceMiddleware | None = None


def register_maintenance(dp: Dispatcher) -> None:
    """
    Регистрирует middleware на message, callback_query и pre_checkout_query.

    Вызывать после throttling / UserExists — в aiogram 3 последний
    зарегистрированный middleware выполняется первым на входящем апдейте.
    """
    global _middleware
    if _middleware is None:
        _middleware = MaintenanceMiddleware()

    dp.message.middleware(_middleware)
    dp.callback_query.middleware(_middleware)
    dp.pre_checkout_query.middleware(_middleware)
