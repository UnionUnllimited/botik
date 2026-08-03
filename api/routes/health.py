"""Служебные маршруты: liveness, readiness, метрики."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from core.db import check_database
from core.metrics import METRICS_CONTENT_TYPE, refresh_business_gauges, render_metrics
from core.redis_client import check_redis

router = APIRouter(tags=["service"])


@router.get("/healthz", summary="Процесс жив")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Готов обслуживать запросы")
async def readyz(response: Response) -> dict[str, object]:
    db_ok = await check_database()
    redis_ok = await check_redis()
    ready = db_ok and redis_ok
    if not ready:
        response.status_code = 503
    return {"status": "ok" if ready else "degraded", "database": db_ok, "redis": redis_ok}


@router.get("/metrics", summary="Метрики Prometheus", include_in_schema=False)
async def metrics(session: AsyncSession = Depends(get_session)) -> Response:
    await refresh_business_gauges(session)
    return Response(content=render_metrics(), media_type=METRICS_CONTENT_TYPE)
