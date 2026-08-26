"""Манифест обновлений и приём образов.

Открыто и без токена — как и списки доменов: за манифестом приходит прошивка,
а не наш процесс, и класть в неё общий секрет значит раздать его вместе
с железом. Адрес зашит в прошивку открытым текстом и не меняется.

Манифест не подписан: роутер проверяет только sha256 образа. Это спасает
от битой закачки, но не от подмены самого манифеста — поэтому домен, хостинг
и сертификат должны быть под тем же контролем, что остальная инфраструктура.

Приём образов, наоборот, закрыт: разовым билетом, который выдаёт админка
по общему токену. Файл едет из браузера оператора прямо сюда, минуя её:
54 МБ через чужой процесс — это лишний перегон, лишняя память и второй таймаут.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session, get_transaction
from core.services import firmware

log = structlog.get_logger("api.firmware")

router = APIRouter(prefix=firmware.URL_PREFIX, tags=["firmware"], include_in_schema=False)

CACHE_SEC = 60
"""Роутер ходит сюда раз в сутки, так что кеш нужен не ему, а прокси. Минута —
чтобы экстренная остановка раскатки успевала подействовать: доля меняется
ползунком и должна применяться сразу."""


@router.get("/manifest.json")
async def manifest(session: AsyncSession = Depends(get_session)) -> Response:
    """Единственное, что роутер о нас знает. Формат менять нельзя — разбирает
    его прошивка, а обновить прошивку можно только этим же манифестом."""
    return JSONResponse(
        content=await firmware.manifest(session),
        headers={"Cache-Control": f"public, max-age={CACHE_SEC}"},
    )


@router.post("/upload")
async def upload(
    request: Request,
    ticket: str = Query(default=""),
    image: UploadFile = File(...),
    session: AsyncSession = Depends(get_transaction),
) -> Response:
    """Приём одного образа по разовому билету.

    Ответ — всегда JSON: страницу грузит скрипт админки, ему нужен результат,
    а не редирект. Заголовок с источником нужен на случай, если админку
    когда-нибудь уведут на другой домен: билет и так единственный пропуск,
    и куки здесь не участвуют.
    """
    headers = {"Access-Control-Allow-Origin": request.headers.get("Origin", "*")}

    target = await firmware.redeem_ticket(ticket)
    if target is None:
        # Билет одноразовый и живёт четверть часа: чаще всего это повторная
        # отправка той же формы или страница, открытая давно.
        return JSONResponse(
            {"ok": False, "error": "Ссылка на загрузку истекла — обновите страницу."},
            status_code=403,
            headers=headers,
        )

    release = await firmware.get_release(session, target.release_id)
    if release is None:
        return JSONResponse(
            {"ok": False, "error": "Выпуск не найден."}, status_code=404, headers=headers
        )

    try:
        saved = await firmware.save_upload(
            version=release.version,
            model_key=target.model_key,
            file_name=image.filename or "",
            source=image,
        )
        await firmware.attach_image(session, release, model_key=target.model_key, saved=saved)
    except firmware.FirmwareError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400, headers=headers)

    return JSONResponse(
        {
            "ok": True,
            "model": target.model_key,
            "file_name": saved.file_name,
            "sha256": saved.sha256,
            "size": saved.size_bytes,
        },
        headers=headers,
    )
