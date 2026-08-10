"""
Лёгкие healthcheck'и инфраструктуры для pre-flight проверок (Remnawave).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class RemnawaveHealth:
    ok: bool
    error: Optional[str]
    ping_ms: Optional[float]


async def check_remnawave(timeout: float = 8.0) -> RemnawaveHealth:
    """Лёгкий ping Remnawave: вызываем `system.get_stats()` с таймаутом."""
    start = time.perf_counter()
    try:
        from remnawave_manager import remnawave_manager_instance  # noqa: WPS433

        await remnawave_manager_instance._ensure_initialized()
        sdk = remnawave_manager_instance._sdk
        if sdk is None:
            return RemnawaveHealth(ok=False, error='SDK не инициализирован', ping_ms=None)

        async def _ping():
            return await sdk.system.get_stats()

        await asyncio.wait_for(_ping(), timeout=timeout)
        ping_ms = round((time.perf_counter() - start) * 1000, 0)
        return RemnawaveHealth(ok=True, error=None, ping_ms=ping_ms)
    except asyncio.TimeoutError:
        return RemnawaveHealth(ok=False, error=f'timeout > {int(timeout)}s', ping_ms=None)
    except ValueError as e:
        return RemnawaveHealth(ok=False, error=f'не настроен: {e}', ping_ms=None)
    except Exception as e:
        return RemnawaveHealth(ok=False, error=f'{type(e).__name__}: {str(e)[:200]}', ping_ms=None)
