"""Сборка роутеров. Порядок важен: fallback всегда последний."""

from aiogram import Router

from bot.handlers import common, errors, fallback


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(errors.router)
    root.include_router(common.router)
    root.include_router(fallback.router)
    return root


__all__ = ["build_router"]
