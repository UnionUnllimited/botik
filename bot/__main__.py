"""Точка входа бота: python -m bot

Режим переключается BOT_MODE: polling (dev) или webhook (prod).
В обоих режимах поднимается внутренний HTTP-сервер с /healthz и /metrics —
на него смотрит healthcheck докера и Prometheus.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import structlog
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from bot.loader import create_bot, create_dispatcher
from core.config import settings
from core.db import check_database, dispose_engine
from core.logging import configure_logging
from core.metrics import METRICS_CONTENT_TYPE, render_metrics
from core.redis_client import check_redis, close_redis
from core.sentry import init_sentry

log = structlog.get_logger("bot")


async def healthz(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def readyz(_: web.Request) -> web.Response:
    db_ok = await check_database()
    redis_ok = await check_redis()
    ready = db_ok and redis_ok
    payload = {"status": "ok" if ready else "degraded", "database": db_ok, "redis": redis_ok}
    return web.json_response(payload, status=200 if ready else 503)


async def metrics(_: web.Request) -> web.Response:
    return web.Response(body=render_metrics(), content_type=METRICS_CONTENT_TYPE.split(";")[0])


def build_internal_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/metrics", metrics)
    return app


async def start_internal_server(app: web.Application) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.bot.internal_host, port=settings.bot.internal_port)
    await site.start()
    return runner


async def on_startup(bot: Bot) -> None:
    me = await bot.get_me()
    log.info("bot.started", username=me.username, mode=settings.bot.mode, env=settings.app.env)
    if settings.bot.mode == "webhook":
        await bot.set_webhook(
            url=settings.bot.webhook_url,
            secret_token=settings.bot.webhook_secret.get_secret_value(),
            drop_pending_updates=settings.bot.drop_pending_updates,
            allowed_updates=["message", "edited_message", "callback_query", "my_chat_member"],
        )
        log.info("bot.webhook_set", url=settings.bot.webhook_url)


async def on_shutdown(_: Bot) -> None:
    await close_redis()
    await dispose_engine()
    log.info("bot.stopped")


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    """SIGTERM от docker stop должен приводить к штатному завершению, а не к kill."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stop_event.set)


async def run_webhook(bot: Bot, dp: Dispatcher, stop_event: asyncio.Event) -> None:
    app = build_internal_app()
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.bot.webhook_secret.get_secret_value(),
    ).register(app, path=settings.bot.webhook_path)
    setup_application(app, dp, bot=bot)  # он же вызовет startup/shutdown диспетчера

    runner = await start_internal_server(app)
    log.info("bot.webhook_listening", port=settings.bot.internal_port, path=settings.bot.webhook_path)
    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()


async def run_polling(bot: Bot, dp: Dispatcher, stop_event: asyncio.Event) -> None:
    runner = await start_internal_server(build_internal_app())
    log.info("bot.polling_started", health_port=settings.bot.internal_port)
    await bot.delete_webhook(drop_pending_updates=settings.bot.drop_pending_updates)
    polling = asyncio.create_task(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()))
    stop_waiter = asyncio.create_task(stop_event.wait())
    try:
        await asyncio.wait({polling, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
        if not polling.done():
            await dp.stop_polling()
            with contextlib.suppress(asyncio.CancelledError):
                await polling
    finally:
        stop_waiter.cancel()
        await runner.cleanup()


async def main() -> int:
    configure_logging("bot")
    init_sentry("bot")
    if not settings.bot.token.get_secret_value():
        log.error("bot.token_missing", hint="Задайте BOT_TOKEN в .env")
        return 1

    bot = create_bot()
    dp = create_dispatcher()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

    try:
        if settings.bot.mode == "webhook":
            await run_webhook(bot, dp, stop_event)
        else:
            await run_polling(bot, dp, stop_event)
    finally:
        await bot.session.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0) from None
