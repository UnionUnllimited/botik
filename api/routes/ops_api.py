"""Ручки для работы оператора: дела на сегодня, состояние системы, поиск.

Читает их админка бота — тем же токеном, что и парк. Логика в сервисах
(`core/services/today.py`, `system_health.py`, `search.py`), здесь только
приём запроса: разложи её по ручкам, и вторая страница, показывающая то же,
однажды посчитала бы по-своему.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session, get_transaction
from api.service_auth import require_token
from core.models import Device
from core.services import activation, router_topics, search, system_health, today

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


# ── Роутеры в рабочем чате ───────────────────────────────────────────────────
# Карточку и её кнопки рисует бот в топике «Роутеры». Текст собираем здесь —
# тот же, что ушёл первым сообщением, — иначе обновление по кнопке показало
# бы одно, а первое сообщение говорило другое.


async def _device(session: AsyncSession, device_id: int) -> Device:
    device = await session.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return device


@router.get("/routers/{device_id}/chat-card", dependencies=[Depends(require_token)])
async def router_chat_card(device_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    return await router_topics.card(session, await _device(session, device_id))


@router.post("/routers/{device_id}/retry-activation", dependencies=[Depends(require_token)])
async def router_retry_activation(
    device_id: int, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """«Попробовать активировать» из рабочего чата — с причиной, если не вышло.

    Обход присутствия пробует то же самое каждую минуту, но молча. Оператору,
    нажавшему кнопку, нужен ответ: заказ не отгружен, панель не ответила,
    SSH до роутера не достучался. Выключенная автоактивация здесь не мешает:
    она запрещает обходу, а нажал человек.
    """
    device = await _device(session, device_id)
    activated, reason = await activation.try_activate_shipped(session, device, respect_toggle=False)
    return {"ok": True, "activated": activated, "message": reason}
