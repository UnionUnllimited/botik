"""Проксирование веб-панели роутера в браузер администратора.

LuCI строит абсолютные ссылки (`/cgi-bin/luci`, `/luci-static/...`), поэтому
проксируются именно корневые пути, а нужный роутер запоминается на нашей
стороне. Открыт может быть один роутер за раз; переключение — кнопкой в его
карточке.

Кто выбрал роутер, знаем двумя способами. Первый — сессия нашей админки, как
было всегда. Второй — короткая сессия панели, заведённая разовым билетом из
админки бота (`core/services/panel_ticket.py`): туннель держит наш контейнер,
и проксировать панель из их процесса нечем, поэтому переехал вход, а не прокси.
Второй способ переживёт удаление нашей админки, первый уйдёт вместе с ней.
"""

from __future__ import annotations

import httpx
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from api.admin.auth import SESSION_COOKIE, load_session
from core.config import settings
from core.services import panel_ticket

router = APIRouter(include_in_schema=False)
log = structlog.get_logger("admin.luci")

# Заголовки, которые нельзя пробрасывать: их выставляет наш сервер.
SKIP_REQUEST_HEADERS = {"host", "connection", "content-length", "accept-encoding"}
SKIP_RESPONSE_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection"}

NOT_SELECTED = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Панель роутера</title></head>
<body style="margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1115;color:#e6e8ee;font:15px/1.6 -apple-system,'Segoe UI',Roboto,sans-serif">
<div style="max-width:460px;padding:26px;background:#171a21;border:1px solid #2a2f3a;border-radius:12px">
<h1 style="font-size:19px;margin:0 0 10px">Роутер не выбран</h1>
<p style="color:#99a0ae;margin:0 0 18px">Откройте панель кнопкой в карточке роутера —
адрес запоминается в вашей сессии.</p>
<a href="/admin/fleet" style="color:#4c8dff">← к списку роутеров</a>
</div></body></html>"""


EXPIRED = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Панель роутера</title></head>
<body style="margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1115;color:#e6e8ee;font:15px/1.6 -apple-system,'Segoe UI',Roboto,sans-serif">
<div style="max-width:460px;padding:26px;background:#171a21;border:1px solid #2a2f3a;border-radius:12px">
<h1 style="font-size:19px;margin:0 0 10px">Панель закрыта</h1>
<p style="color:#99a0ae;margin:0">Сессия панели истекла. Вернитесь в карточку роутера
в админке и нажмите «Открыть панель» ещё раз.</p>
</div></body></html>"""


async def _target(request: Request) -> tuple[int, str] | Response:
    """Порт туннеля и MAC выбранного роутера либо ответ с объяснением отказа."""
    # Билет из админки бота: короткий, явный и не требует входа к нам.
    opened = await panel_ticket.load(request.cookies.get(panel_ticket.COOKIE))
    if opened is not None:
        return opened.port, opened.mac

    if not request.cookies.get(SESSION_COOKIE):
        # Ни билета, ни нашей сессии: человек пришёл из чужой админки, и слать
        # его на нашу форму входа незачем — там ему делать нечего.
        return HTMLResponse(EXPIRED, status_code=409)

    loaded = await load_session(request)
    if loaded is None or not loaded[1].mfa_passed:
        return RedirectResponse("/admin/login", status_code=303)

    session_data = loaded[1]
    if not session_data.can("console"):
        return HTMLResponse("Недостаточно прав", status_code=403)
    if not session_data.router_port:
        return HTMLResponse(NOT_SELECTED, status_code=409)
    return session_data.router_port, session_data.router_mac or ""


@router.get("/panel/open")
async def open_panel(ticket: str = "") -> Response:
    """Обмен разового билета на сессию панели. Сюда ведёт кнопка в админке бота."""
    redeemed = await panel_ticket.redeem(ticket)
    if redeemed is None:
        return HTMLResponse(EXPIRED, status_code=409)

    cookie_value, target = redeemed
    response = RedirectResponse("/cgi-bin/luci/", status_code=303)
    response.set_cookie(
        panel_ticket.COOKIE,
        cookie_value,
        max_age=panel_ticket.SESSION_TTL_SEC,
        httponly=True,
        secure=settings.app.is_prod,
        samesite="lax",
        # Панель живёт на корневых путях — кука нужна на всём сайте.
        path="/",
    )
    log.info("luci.panel_opened_by_ticket", device_id=target.device_id, mac=target.mac)
    return response


async def _proxy(request: Request, path: str) -> Response:
    resolved = await _target(request)
    if isinstance(resolved, Response):
        return resolved
    router_port, router_mac = resolved

    target = f"http://{settings.frp.visitor_host}:{router_port}{path}"
    headers = {
        key: value for key, value in request.headers.items() if key.lower() not in SKIP_REQUEST_HEADERS
    }
    body = await request.body()

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.frp.router_http_timeout_sec),
            follow_redirects=False,
            verify=False,  # noqa: S501 — самоподписанный сертификат роутера внутри туннеля
        ) as client:
            upstream = await client.request(
                request.method,
                target,
                params=request.query_params,
                content=body or None,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        log.warning("luci.proxy_failed", mac=router_mac, error=str(exc))
        return HTMLResponse(f"Роутер не ответил: {exc}. Проверьте, что туннель поднят.", status_code=502)

    response_headers = {
        key: value for key, value in upstream.headers.items() if key.lower() not in SKIP_RESPONSE_HEADERS
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"]


@router.api_route("/cgi-bin/{path:path}", methods=PROXY_METHODS)
async def cgi_bin(request: Request, path: str) -> Response:
    return await _proxy(request, f"/cgi-bin/{path}")


@router.api_route("/luci-static/{path:path}", methods=PROXY_METHODS)
async def luci_static(request: Request, path: str) -> Response:
    return await _proxy(request, f"/luci-static/{path}")


@router.api_route("/luci/{path:path}", methods=PROXY_METHODS)
async def luci_path(request: Request, path: str) -> Response:
    return await _proxy(request, f"/luci/{path}")


@router.api_route("/ubus", methods=PROXY_METHODS)
async def ubus(request: Request) -> Response:
    return await _proxy(request, "/ubus")


@router.api_route("/ubus/{path:path}", methods=PROXY_METHODS)
async def ubus_path(request: Request, path: str) -> Response:
    return await _proxy(request, f"/ubus/{path}")
