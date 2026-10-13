"""Ручки для работы оператора: дела на сегодня, состояние системы, поиск.

Читает их админка бота — тем же токеном, что и парк. Логика в сервисах
(`core/services/today.py`, `system_health.py`, `search.py`), здесь только
приём запроса: разложи её по ручкам, и вторая страница, показывающая то же,
однажды посчитала бы по-своему.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from api.service_auth import require_token
from core.services import search, system_health, today

router = APIRouter(prefix="/api/v1/fleet", tags=["ops"], include_in_schema=False)


@router.get("/today", dependencies=[Depends(require_token)])
async def today_view(session: AsyncSession = Depends(get_session)) -> dict:
    """Что требует руки оператора — для главной страницы админки."""
    return await today.build(session)


@router.get("/health", dependencies=[Depends(require_token)])
async def health_view() -> dict:
    """Состояние каждого звена. Сессию не берёт: у каждой проверки своя,
    они идут разом."""
    checks = await system_health.collect()
    return {"overall": system_health.overall(checks), "checks": checks}


@router.get("/search", dependencies=[Depends(require_token)])
async def search_view(
    q: str = Query("", max_length=120),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return {"query": q, "results": await search.find(session, q)}
