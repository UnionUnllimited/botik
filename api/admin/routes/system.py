"""Настройки, журнал действий и управление администраторами."""

from __future__ import annotations

import json
import secrets

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin import audit
from api.admin.auth import (
    Principal,
    destroy_sessions_of,
    form_value,
    require_section,
    verify_csrf,
)
from api.admin.templating import render
from api.deps import get_session, get_transaction
from core.enums import AdminRole
from core.models import AdminUser, AuditLog
from core.security import hash_password
from core.services import settings_service

router = APIRouter()
log = structlog.get_logger("admin.system")

PAGE_SIZE = 50


# ---------------------------------------------------------------- настройки


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(
    request: Request,
    principal: Principal = Depends(require_section("settings")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    values: dict[str, str] = {}
    for key in settings_service.DEFAULTS:
        value = await settings_service.get_setting(session, key)
        values[key] = (
            json.dumps(value, ensure_ascii=False, indent=2)
            if isinstance(value, (dict, list))
            else ("да" if value is True else "нет" if value is False else str(value))
        )
    return render(
        request,
        "settings.html",
        principal,
        values=values,
        descriptions=settings_service.DESCRIPTIONS,
        complex_keys={
            key for key, value in settings_service.DEFAULTS.items() if isinstance(value, (dict, list))
        },
        bool_keys={key for key, value in settings_service.DEFAULTS.items() if isinstance(value, bool)},
    )


@router.post("/settings", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def save_settings(
    request: Request,
    principal: Principal = Depends(require_section("settings")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    form = await request.form()
    changed: list[str] = []

    for key, default in settings_service.DEFAULTS.items():
        if key not in form:
            if isinstance(default, bool):
                # Чекбокс не приходит в форме, когда снят.
                current = await settings_service.get_setting(session, key)
                if current is not False:
                    await settings_service.set_setting(session, key, False, admin_id=principal.admin.id)
                    changed.append(key)
            continue

        raw = form_value(form, key)
        if isinstance(default, bool):
            value: object = raw == "on"
        elif isinstance(default, (dict, list)):
            try:
                value = json.loads(raw or "{}")
            except json.JSONDecodeError:
                return RedirectResponse(f"/admin/settings?err=Неверный+JSON+в+{key}", status_code=303)
        else:
            value = raw

        current = await settings_service.get_setting(session, key)
        if current != value:
            await settings_service.set_setting(session, key, value, admin_id=principal.admin.id)
            changed.append(key)

    if changed:
        audit.record(
            session,
            admin_id=principal.admin.id,
            action="settings.updated",
            entity_type="settings",
            new={"keys": changed},
            request=request,
        )
    return RedirectResponse(f"/admin/settings?ok=Сохранено+параметров:+{len(changed)}", status_code=303)


# ------------------------------------------------------------------- журнал


@router.get("/audit", response_class=HTMLResponse, include_in_schema=False)
async def audit_page(
    request: Request,
    principal: Principal = Depends(require_section("audit")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    action_filter = request.query_params.get("action", "").strip()
    entity_filter = request.query_params.get("entity", "").strip()
    page = max(int(request.query_params.get("page", "1") or 1), 1)

    query = select(AuditLog)
    counter = select(func.count()).select_from(AuditLog)
    if action_filter:
        query = query.where(AuditLog.action.ilike(f"%{action_filter}%"))
        counter = counter.where(AuditLog.action.ilike(f"%{action_filter}%"))
    if entity_filter:
        query = query.where(AuditLog.entity_type == entity_filter)
        counter = counter.where(AuditLog.entity_type == entity_filter)

    total = await session.scalar(counter) or 0
    entries = list(
        await session.scalars(
            query.order_by(AuditLog.id.desc()).limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)
        )
    )
    admins = {admin.id: admin.login for admin in await session.scalars(select(AdminUser))}

    return render(
        request,
        "audit.html",
        principal,
        entries=entries,
        admins=admins,
        action_filter=action_filter,
        entity_filter=entity_filter,
        page=page,
        pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        total=total,
    )


# ------------------------------------------------------------- администраторы


@router.get("/admins", response_class=HTMLResponse, include_in_schema=False)
async def admins_page(
    request: Request,
    principal: Principal = Depends(require_section("admins")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    admins = list(await session.scalars(select(AdminUser).order_by(AdminUser.id)))
    return render(
        request,
        "admins.html",
        principal,
        admins=admins,
        roles=list(AdminRole),
        created_password=request.query_params.get("password"),
    )


@router.post("/admins", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def create_admin(
    request: Request,
    principal: Principal = Depends(require_section("admins")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    form = await request.form()
    login = form_value(form, "login").lower()
    if not login:
        return RedirectResponse("/admin/admins?err=Укажите+логин", status_code=303)
    exists = await session.scalar(select(AdminUser).where(AdminUser.login == login))
    if exists is not None:
        return RedirectResponse("/admin/admins?err=Такой+логин+занят", status_code=303)

    password = secrets.token_urlsafe(12)
    tg_id_raw = form_value(form, "tg_id")
    admin = AdminUser(
        login=login,
        password_hash=hash_password(password),
        full_name=form_value(form, "full_name"),
        tg_id=int(tg_id_raw) if tg_id_raw.isdigit() else None,
        role=AdminRole(form_value(form, "role", AdminRole.SUPPORT.value)),
        is_active=True,
    )
    session.add(admin)
    await session.flush()

    audit.record(
        session,
        admin_id=principal.admin.id,
        action="admin.created",
        entity_type="admin_user",
        entity_id=admin.id,
        new={"login": login, "role": str(admin.role)},
        request=request,
    )
    log.info("admin.created", login=login, by=principal.admin.login)
    # Пароль показывается один раз — в базе только хеш.
    return RedirectResponse(f"/admin/admins?password={password}", status_code=303)


@router.post("/admins/{admin_id}", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def update_admin(
    admin_id: int,
    request: Request,
    principal: Principal = Depends(require_section("admins")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    admin = await session.get(AdminUser, admin_id)
    if admin is None:
        return RedirectResponse("/admin/admins?err=Не+найден", status_code=303)

    form = await request.form()
    action = form_value(form, "action")
    before = {"role": str(admin.role), "is_active": admin.is_active, "totp": admin.totp_enabled}

    if action == "role":
        if admin.id == principal.admin.id:
            return RedirectResponse("/admin/admins?err=Нельзя+менять+свою+роль", status_code=303)
        admin.role = AdminRole(form_value(form, "role", str(admin.role)))
    elif action == "toggle":
        if admin.id == principal.admin.id:
            return RedirectResponse("/admin/admins?err=Нельзя+отключить+себя", status_code=303)
        admin.is_active = not admin.is_active
        if not admin.is_active:
            await destroy_sessions_of(admin.id)
    elif action == "reset_2fa":
        admin.totp_enabled = False
        admin.totp_secret_enc = None
        await destroy_sessions_of(admin.id)
    elif action == "reset_password":
        password = secrets.token_urlsafe(12)
        admin.password_hash = hash_password(password)
        admin.failed_attempts = 0
        admin.locked_until = None
        await destroy_sessions_of(admin.id)
        audit.record(
            session,
            admin_id=principal.admin.id,
            action="admin.password_reset",
            entity_type="admin_user",
            entity_id=admin.id,
            request=request,
        )
        return RedirectResponse(f"/admin/admins?password={password}", status_code=303)
    else:
        return RedirectResponse("/admin/admins?err=Неизвестное+действие", status_code=303)

    after = {"role": str(admin.role), "is_active": admin.is_active, "totp": admin.totp_enabled}
    old_changed, new_changed = audit.diff(before, after)
    audit.record(
        session,
        admin_id=principal.admin.id,
        action=f"admin.{action}",
        entity_type="admin_user",
        entity_id=admin.id,
        old=old_changed,
        new=new_changed | {"login": admin.login},
        request=request,
    )
    return RedirectResponse("/admin/admins?ok=Изменения+сохранены", status_code=303)
