"""Раздача собранных списков роутерам.

Открыто и без токена: за списком приходит прошивка, а не наш процесс, и класть
в неё общий секрет — значит раздать его вместе с железом. Содержимое и так
публичное: те же файлы лежат в открытых репозиториях, откуда мы их и берём.

Отдаётся с диска, а не из базы: за списком ходит весь парк, и вычитывать
пару мегабайт из Postgres на каждый запрос незачем.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from core.models import ListKind
from core.services import domain_lists

router = APIRouter(prefix="/lists", tags=["lists"])

CACHE_SEC = 300
"""Пять минут. Сборка идёт раз в час, но при ручном обновлении оператор ждёт
результата на роутерах, а не следующего часа."""


def _plain(body: str) -> Response:
    return Response(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": f"public, max-age={CACHE_SEC}"},
    )


@router.get("/proxy-domains.lst")
async def proxy_domains() -> Response:
    """Домены через туннель — `gfwlist_url` при `gfwlist_update '1'`.

    Пустой ответ означает, что сборки ещё не было. Пустое, а не 404: прошивка
    на 404 может решить, что адрес сменился, и записать себе пустой список —
    разницы в итоге никакой, а разбираться с этим на роутере у клиента куда
    сложнее, чем посмотреть сюда.
    """
    return _plain(domain_lists.read_list(ListKind.PROXY_DOMAIN))


@router.get("/proxy-ip.lst")
async def proxy_ip() -> Response:
    """Сети через туннель. Штатной настройки у PassWall под них нет —
    адрес прописывается в прошивке вручную."""
    return _plain(domain_lists.read_list(ListKind.PROXY_IP))
