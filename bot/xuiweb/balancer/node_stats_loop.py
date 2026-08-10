"""Background polling of Remnawave node stats for xuiweb."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

from .node_stats import NodeStatsCache, NodeStatsSettings, node_stats_cache

logger = logging.getLogger(__name__)

SettingsLoader = Callable[[], Awaitable[NodeStatsSettings]]
RemnawaveLoader = Callable[[], Awaitable[tuple[str | None, str | None]]]


async def run_node_stats_loop(
    *,
    get_settings: SettingsLoader,
    get_remnawave: RemnawaveLoader,
    http_client: httpx.AsyncClient,
    cache: NodeStatsCache | None = None,
) -> None:
    """Poll /api/nodes/ while node_stats is enabled in DB settings."""
    store = cache or node_stats_cache
    while True:
        sleep_sec = 120
        try:
            settings = await get_settings()
            sleep_sec = max(30, int(settings.interval_sec or 120))
            if settings.enabled:
                base_url, token = await get_remnawave()
                if base_url and token:
                    await store.refresh(
                        rw_base_url=base_url,
                        rw_token=token,
                        http_client=http_client,
                        settings=settings,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('[node-stats] loop error: %s', exc)
        await asyncio.sleep(sleep_sec)
