"""Просьба оценить роутер через неделю после включения.

Логика — в `core/services/reviews.py`; здесь только круг: раз в полчаса,
а сам сервис решает, день ли сейчас и кого пора спросить.
"""

from __future__ import annotations

from core.db import session_scope
from core.services import reviews


async def ask_for_reviews() -> int:
    async with session_scope() as session:
        return await reviews.ask_due(session)
