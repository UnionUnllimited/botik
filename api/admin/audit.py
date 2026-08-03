"""Аудит действий администраторов.

Пишется на каждое изменение денег, подписок, прав и настроек: кто, что,
когда, было и стало. Запись идёт в той же транзакции, что и само изменение,
поэтому «действие есть, а записи нет» невозможно.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import client_ip
from core.enums import ActorType
from core.models import AuditLog


def _jsonable(value: Any) -> Any:
    """Приводит значения к тому, что ложится в jsonb без потерь смысла."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def record(
    session: AsyncSession,
    *,
    admin_id: int | None,
    action: str,
    entity_type: str = "",
    entity_id: str | int = "",
    old: dict[str, Any] | None = None,
    new: dict[str, Any] | None = None,
    comment: str | None = None,
    request: Request | None = None,
    actor: ActorType = ActorType.ADMIN,
) -> AuditLog:
    entry = AuditLog(
        actor_type=actor,
        admin_id=admin_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        old_value=_jsonable(old or {}),
        new_value=_jsonable(new or {}),
        comment=comment,
        ip=client_ip(request) if request is not None else None,
        user_agent=(request.headers.get("user-agent", "")[:255] if request is not None else None),
    )
    session.add(entry)
    return entry


def diff(before: dict[str, Any], after: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Оставляет в записи только изменившиеся поля — журнал читаемый, а не простыня."""
    changed_before: dict[str, Any] = {}
    changed_after: dict[str, Any] = {}
    for key, new_value in after.items():
        old_value = before.get(key)
        if old_value != new_value:
            changed_before[key] = old_value
            changed_after[key] = new_value
    return changed_before, changed_after
