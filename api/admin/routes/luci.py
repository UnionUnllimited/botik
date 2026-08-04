"""Проксирование веб-панели роутера в браузер администратора.

LuCI строит абсолютные ссылки (`/cgi-bin/luci`, `/luci-static/...`), поэтому
проксируются именно корневые пути, а нужный роутер запоминается в сессии
администратора — так же, как это делала прежняя панель. Открыт может быть
один роутер за раз; переключение — кнопкой в его карточке.
"""

from __future__ import annotations

import httpx
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from api.admin.auth import load_session
from core.config import settings

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


async def _proxy(request: Request, path: str) -> Response:
    loaded = await load_session(request)
    if loaded is None or not loaded[1].mfa_passed:
        return RedirectResponse("/admin/login", status_code=303)

    session_data = loaded[1]
    if not session_data.can("console"):
        return HTMLResponse("Недостаточно прав", status_code=403)
    if not session_data.router_port:
        return HTMLResponse(NOT_SELECTED, status_code=409)

    target = f"http://{settings.frp.visitor_host}:{session_data.router_port}{path}"
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
        log.warning("luci.proxy_failed", mac=session_data.router_mac, error=str(exc))
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
