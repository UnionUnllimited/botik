"""Вход, второй фактор и выход."""

from __future__ import annotations

import base64
import io

import qrcode
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin import audit
from api.admin.auth import (
    AuthError,
    authenticate,
    check_totp_replay,
    clear_session_cookie,
    create_session,
    destroy_session,
    issue_totp_secret,
    load_session,
    set_session_cookie,
    totp_secret_for,
    totp_uri,
    update_session,
    verify_csrf,
    verify_totp,
)
from api.admin.templating import templates
from api.deps import client_ip, get_transaction
from core.models import AdminUser

router = APIRouter()
log = structlog.get_logger("admin.auth.routes")


def _login_page(
    request: Request,
    *,
    stage: str = "password",
    error: str | None = None,
    csrf_token: str = "",
) -> HTMLResponse:
    is_password = stage == "password"
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "stage": stage,
            "error": error,
            "csrf_token": csrf_token,
            "action": "/admin/login" if is_password else "/admin/login/2fa",
            "title": "Вход в админку" if is_password else "Подтверждение входа",
            "subtitle": ("Доступ только для сотрудников" if is_password else "Введите код из приложения"),
            "submit": "Войти" if is_password else "Подтвердить",
            "hint": "" if is_password else "Код обновляется каждые 30 секунд",
        },
    )


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_form(request: Request) -> HTMLResponse:
    loaded = await load_session(request)
    if loaded is not None and loaded[1].mfa_passed:
        return RedirectResponse("/admin/", status_code=303)
    return _login_page(request)


@router.post("/login", include_in_schema=False, response_model=None)
async def login_submit(
    request: Request, session: AsyncSession = Depends(get_transaction)
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    login = str(form.get("login", "")).strip().lower()
    password = str(form.get("password", ""))
    ip = client_ip(request)

    try:
        admin = await authenticate(session, login=login, password=password, ip=ip)
    except AuthError as exc:
        return _login_page(request, error=str(exc))

    token, _ = await create_session(admin, ip=ip, mfa_passed=False)
    audit.record(
        session,
        admin_id=admin.id,
        action="admin.login",
        entity_type="admin_user",
        entity_id=admin.id,
        request=request,
    )

    target = "/admin/login/2fa" if admin.totp_enabled else "/admin/2fa/setup"
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(response, token)
    return response


@router.get("/login/2fa", response_class=HTMLResponse, include_in_schema=False, response_model=None)
async def totp_form(request: Request) -> HTMLResponse | RedirectResponse:
    loaded = await load_session(request)
    if loaded is None:
        return RedirectResponse("/admin/login", status_code=303)
    _, data = loaded
    if data.mfa_passed:
        return RedirectResponse("/admin/", status_code=303)
    return _login_page(request, stage="totp", csrf_token=data.csrf)


@router.post("/login/2fa", include_in_schema=False, dependencies=[Depends(verify_csrf)], response_model=None)
async def totp_submit(
    request: Request, session: AsyncSession = Depends(get_transaction)
) -> HTMLResponse | RedirectResponse:
    loaded = await load_session(request)
    if loaded is None:
        return RedirectResponse("/admin/login", status_code=303)
    session_id, data = loaded

    admin = await session.get(AdminUser, data.admin_id)
    if admin is None or not admin.is_active:
        await destroy_session(session_id)
        return RedirectResponse("/admin/login", status_code=303)

    secret = totp_secret_for(admin)
    form = await request.form()
    code = str(form.get("code", ""))

    if secret is None or not verify_totp(secret, code):
        log.warning("admin.totp.failed", login=admin.login, ip=client_ip(request))
        return _login_page(request, stage="totp", error="Неверный код", csrf_token=data.csrf)

    if not await check_totp_replay(admin.id, code):
        return _login_page(request, stage="totp", error="Этот код уже использован", csrf_token=data.csrf)

    data.mfa_passed = True
    await update_session(session_id, data)
    log.info("admin.login.completed", login=admin.login)
    return RedirectResponse("/admin/", status_code=303)


@router.get("/2fa/setup", response_class=HTMLResponse, include_in_schema=False, response_model=None)
async def totp_setup_form(
    request: Request, session: AsyncSession = Depends(get_transaction)
) -> HTMLResponse | RedirectResponse:
    loaded = await load_session(request)
    if loaded is None:
        return RedirectResponse("/admin/login", status_code=303)
    _, data = loaded

    admin = await session.get(AdminUser, data.admin_id)
    if admin is None:
        return RedirectResponse("/admin/login", status_code=303)
    if admin.totp_enabled and not data.mfa_passed:
        return RedirectResponse("/admin/login/2fa", status_code=303)

    secret = totp_secret_for(admin) if not admin.totp_enabled else None
    if secret is None:
        secret = issue_totp_secret(admin)

    return templates.TemplateResponse(
        request,
        "totp_setup.html",
        {
            "request": request,
            "secret": secret,
            "qr_data_uri": _qr_data_uri(totp_uri(admin, secret)),
            "csrf_token": data.csrf,
            "error": request.query_params.get("err"),
        },
    )


@router.post("/2fa/setup", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def totp_setup_submit(
    request: Request, session: AsyncSession = Depends(get_transaction)
) -> RedirectResponse:
    loaded = await load_session(request)
    if loaded is None:
        return RedirectResponse("/admin/login", status_code=303)
    session_id, data = loaded

    admin = await session.get(AdminUser, data.admin_id)
    if admin is None:
        return RedirectResponse("/admin/login", status_code=303)

    secret = totp_secret_for(admin)
    form = await request.form()
    code = str(form.get("code", ""))

    if secret is None or not verify_totp(secret, code):
        return RedirectResponse("/admin/2fa/setup?err=Неверный+код", status_code=303)

    admin.totp_enabled = True
    data.mfa_passed = True
    await update_session(session_id, data)
    audit.record(
        session,
        admin_id=admin.id,
        action="admin.2fa_enabled",
        entity_type="admin_user",
        entity_id=admin.id,
        request=request,
    )
    log.info("admin.2fa.enabled", login=admin.login)
    return RedirectResponse("/admin/?ok=Второй+фактор+подключён", status_code=303)


@router.post("/logout", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def logout(request: Request) -> RedirectResponse:
    loaded = await load_session(request)
    if loaded is not None:
        await destroy_session(loaded[0])
    response = RedirectResponse("/admin/login", status_code=303)
    clear_session_cookie(response)
    return response


def _qr_data_uri(uri: str) -> str:
    """QR рисуем сами: внешние генераторы получили бы наш TOTP-секрет."""
    image = qrcode.make(uri)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
