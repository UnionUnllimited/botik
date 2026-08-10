"""Background polling of Remnawave hosts/nodes for text link balancing."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

from .link_host_stats import TextLinkHostCache, TextLinkHostStatsSettings, text_link_host_cache

logger = logging.getLogger(__name__)

SettingsLoader = Callable[[], Awaitable[TextLinkHostStatsSettings]]
RemnawaveLoader = Callable[[], Awaitable[tuple[str | None, str | None]]]


async def run_link_host_stats_loop(
    *,
    get_settings: SettingsLoader,
    get_remnawave: RemnawaveLoader,
    http_client: httpx.AsyncClient,
    cache: TextLinkHostCache | None = None,
) -> None:
    store = cache or text_link_host_cache
    while True:
        sleep_sec = 60
        try:
            settings = await get_settings()
            sleep_sec = max(30, int(settings.interval_sec or 60))
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
            logger.warning('[link-host-stats] loop error: %s', exc)
        await asyncio.sleep(sleep_sec)
