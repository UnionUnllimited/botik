"""Общий секрет для ручек, которыми ходит сторонний бот и его админка.

Сессии тут не годятся: на том конце процесс, а не человек в браузере.
Один и тот же токен закрывает и парк роутеров, и каталог — это одна и та же
пара «наш API ↔ их службы», и второй секрет пришлось бы заводить и раздавать
дважды ради того же доверия.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from core.config import settings


async def require_token(authorization: str = Header(default="")) -> None:
    expected = settings.api.fleet_token.get_secret_value()
    if not expected:
        # Токен не задан — ручки как будто нет. Иначе выключенная возможность
        # молча раздавала бы данные всем.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")

    presented = authorization.removeprefix("Bearer ").strip()
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad_token")
