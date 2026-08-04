"""Консоль роутера: журналы, готовые команды и произвольный ввод.

Команды выполняются по SSH через тот же обратный туннель, которым роутер
подключён к frps. Раздел доступен только владельцу и администратору:
это работа под root на устройстве клиента.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin import audit
from api.admin.auth import Principal, form_value, require_section, verify_csrf
from api.admin.templating import render
from api.deps import get_session, get_transaction
from core.models import Device
from core.services import router_shell
from core.services import routers as router_service

router = APIRouter(prefix="/console")
log = structlog.get_logger("admin.console")

# Команды, которые не должны выполняться из веб-консоли даже по ошибке.
FORBIDDEN = ("mkfs", "firstboot", "rm -rf /", "> /dev/mtd", "dd if=", "sysupgrade")


@router.get("/{device_id}", response_class=HTMLResponse, include_in_schema=False, response_model=None)
async def console_page(
    device_id: int,
    request: Request,
    principal: Principal = Depends(require_section("console")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    device = await session.scalar(select(Device).where(Device.id == device_id))
    if device is None:
        return RedirectResponse("/admin/fleet?err=Роутер+не+найден", status_code=303)

    return render(
        request,
        "console.html",
        principal,
        device=device,
        log_sources=router_shell.LOG_SOURCES,
        quick_commands=router_shell.QUICK_COMMANDS,
        result=None,
        ssh_port=device.frp_visitor_port and router_shell.ssh_port_for(device),
    )


@router.post(
    "/{device_id}",
    include_in_schema=False,
    dependencies=[Depends(verify_csrf)],
    response_model=None,
)
async def console_run(
    device_id: int,
    request: Request,
    principal: Principal = Depends(require_section("console")),
    session: AsyncSession = Depends(get_transaction),
) -> HTMLResponse | RedirectResponse:
    device = await session.scalar(select(Device).where(Device.id == device_id))
    if device is None:
        return RedirectResponse("/admin/fleet?err=Роутер+не+найден", status_code=303)

    form = await request.form()
    kind = form_value(form, "kind", "command")
    error: str | None = None
    result = None

    try:
        if kind == "log":
            source = form_value(form, "source", "system")
            result = await router_shell.read_log(device, source, lines=200)
            action = f"log:{source}"
        elif kind == "quick":
            name = form_value(form, "name")
            result = await router_shell.run_quick(device, name)
            action = f"quick:{name}"
        else:
            command = form_value(form, "command")
            if not command:
                error = "Введите команду"
                action = ""
            elif any(bad in command.lower() for bad in FORBIDDEN):
                # Перепрошивка и форматирование через веб-консоль — верный способ
                # получить кирпич у клиента.
                error = "Эта команда запрещена из веб-консоли"
                action = ""
                log.warning(
                    "console.forbidden_command",
                    device_id=device.id,
                    admin=principal.admin.login,
                    command=command[:120],
                )
            else:
                result = await router_shell.run(device, command)
                action = "command"
    except router_shell.ShellError as exc:
        error = str(exc)
        action = ""

    if result is not None:
        audit.record(
            session,
            admin_id=principal.admin.id,
            action="console.executed",
            entity_type="device",
            entity_id=device.id,
            new={"command": result.command[:500], "exit": result.exit_status, "kind": action},
            request=request,
        )
        router_service.add_event(
            session,
            device_id=device.id,
            mac=device.mac,
            level="info" if result.ok else "warning",
            message=f"Консоль: {result.command[:120]}",
            payload={"by": principal.admin.login, "exit": result.exit_status},
        )

    return render(
        request,
        "console.html",
        principal,
        device=device,
        log_sources=router_shell.LOG_SOURCES,
        quick_commands=router_shell.QUICK_COMMANDS,
        result=result,
        error=error,
        ssh_port=device.frp_visitor_port and router_shell.ssh_port_for(device),
    )
