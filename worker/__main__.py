"""Точка входа воркера: python -m worker"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import structlog
from aiohttp import web

from core.config import settings
from core.db import check_database, dispose_engine
from core.logging import configure_logging
from core.metrics import METRICS_CONTENT_TYPE, render_metrics
from core.notifications import close_bot
from core.payments import close_providers
from core.redis_client import check_redis, close_redis
from core.sentry import init_sentry
from worker.scheduler import create_scheduler

log = structlog.get_logger("worker")

HEALTH_PORT = 8082


async def healthz(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def readyz(_: web.Request) -> web.Response:
    db_ok = await check_database()
    redis_ok = await check_redis()
    ready = db_ok and redis_ok
    return web.json_response(
        {"status": "ok" if ready else "degraded", "database": db_ok, "redis": redis_ok},
        status=200 if ready else 503,
    )


async def metrics(_: web.Request) -> web.Response:
    return web.Response(body=render_metrics(), content_type=METRICS_CONTENT_TYPE.split(";")[0])


async def main() -> int:
    configure_logging("worker")
    init_sentry("worker")

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/metrics", metrics)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=HEALTH_PORT).start()  # noqa: S104

    scheduler = create_scheduler()
    scheduler.start()
    log.info("worker.started", env=settings.app.env, health_port=HEALTH_PORT)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stop_event.set)

    try:
        await stop_event.wait()
    finally:
        scheduler.shutdown(wait=True)
        await runner.cleanup()
        await close_providers()
        await close_bot()
        await close_redis()
        await dispose_engine()
        log.info("worker.stopped")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0) from None
