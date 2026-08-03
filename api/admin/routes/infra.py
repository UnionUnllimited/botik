"""Узлы, группы узлов и устройства.

Раздел устройств наполняется, когда роутеры начнут ходить в API: до этого
здесь видны только записи, заведённые вручную при отгрузке заказов.
"""

from __future__ import annotations

import datetime as dt
import json

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.admin import audit
from api.admin.auth import Principal, form_value, require_section, verify_csrf
from api.admin.templating import render
from api.deps import get_session, get_transaction
from core.config import settings
from core.dates import utcnow
from core.enums import CommandStatus, CommandType, DeviceStatus, NodeProtocol, NodeStatus
from core.models import Device, DeviceCommand, Node, NodeAssignment, NodeGroup
from core.security import normalize_mac

router = APIRouter()
log = structlog.get_logger("admin.infra")

PAGE_SIZE = 40


def _int(raw: str, default: int = 0) -> int:
    try:
        return int(raw.strip() or default)
    except ValueError:
        return default


# ------------------------------------------------------------------- узлы


@router.get("/nodes", response_class=HTMLResponse, include_in_schema=False)
async def node_list(
    request: Request,
    principal: Principal = Depends(require_section("nodes")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    nodes = list(
        await session.scalars(
            select(Node).order_by(Node.priority, Node.id).options(selectinload(Node.assignments))
        )
    )
    groups = list(await session.scalars(select(NodeGroup).order_by(NodeGroup.id)))
    return render(
        request,
        "nodes.html",
        principal,
        nodes=nodes,
        groups=groups,
        protocols=list(NodeProtocol),
        statuses=list(NodeStatus),
        node_prefix=settings.subscription.node_prefix,
    )


@router.post("/nodes/{node_id}", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def save_node(
    node_id: int,
    request: Request,
    principal: Principal = Depends(require_section("nodes")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    form = await request.form()
    node = await session.get(Node, node_id) if node_id else None
    creating = node is None

    remarks = form_value(form, "remarks")
    prefix = settings.subscription.node_prefix
    if remarks and not remarks.startswith(prefix):
        # Без префикса клиент на роутере отбросит узел фильтром — не даём сохранить.
        return RedirectResponse(f"/admin/nodes?err=Имя+должно+начинаться+с+{prefix}", status_code=303)

    if creating:
        if not remarks:
            return RedirectResponse("/admin/nodes?err=Укажите+имя+узла", status_code=303)
        exists = await session.scalar(select(Node).where(Node.remarks == remarks))
        if exists is not None:
            return RedirectResponse("/admin/nodes?err=Узел+с+таким+именем+есть", status_code=303)
        node = Node(remarks=remarks, host="", port=443)
        session.add(node)

    before = {"status": str(node.status), "host": node.host, "priority": node.priority}

    node.remarks = remarks or node.remarks
    node.location = form_value(form, "location")
    node.country_code = form_value(form, "country_code")[:2].upper()
    node.host = form_value(form, "host") or node.host
    node.port = _int(form_value(form, "port"), node.port)
    node.priority = _int(form_value(form, "priority"), node.priority)
    node.device_limit = _int(form_value(form, "device_limit"), node.device_limit)
    node.protocol = NodeProtocol(form_value(form, "protocol", str(node.protocol)))
    node.status = NodeStatus(form_value(form, "status", str(node.status)))
    node.admin_note = form_value(form, "admin_note") or None

    config_raw = form_value(form, "config")
    if config_raw:
        try:
            parsed = json.loads(config_raw)
        except json.JSONDecodeError:
            return RedirectResponse("/admin/nodes?err=Конфигурация:+неверный+JSON", status_code=303)
        if not isinstance(parsed, dict):
            return RedirectResponse("/admin/nodes?err=Конфигурация+должна+быть+объектом", status_code=303)
        node.config = parsed

    await session.flush()

    group_id = form_value(form, "group_id")
    if group_id.isdigit():
        exists = await session.scalar(
            select(NodeAssignment).where(
                NodeAssignment.node_id == node.id, NodeAssignment.group_id == int(group_id)
            )
        )
        if exists is None:
            session.add(NodeAssignment(node_id=node.id, group_id=int(group_id)))

    old_changed, new_changed = audit.diff(
        before, {"status": str(node.status), "host": node.host, "priority": node.priority}
    )
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="node.created" if creating else "node.updated",
        entity_type="node",
        entity_id=node.id,
        old=old_changed,
        new=new_changed | {"remarks": node.remarks},
        request=request,
    )
    log.info("admin.node_saved", node_id=node.id, status=str(node.status))
    return RedirectResponse("/admin/nodes?ok=Узел+сохранён", status_code=303)


@router.post("/nodes/groups/new", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def create_group(
    request: Request,
    principal: Principal = Depends(require_section("nodes")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    form = await request.form()
    slug = form_value(form, "slug")
    if not slug:
        return RedirectResponse("/admin/nodes?err=Укажите+slug+группы", status_code=303)
    exists = await session.scalar(select(NodeGroup).where(NodeGroup.slug == slug))
    if exists is not None:
        return RedirectResponse("/admin/nodes?err=Группа+уже+есть", status_code=303)

    group = NodeGroup(
        slug=slug,
        title=form_value(form, "title") or slug,
        description=form_value(form, "description"),
        max_nodes_per_device=_int(form_value(form, "max_nodes_per_device"), 5),
    )
    session.add(group)
    await session.flush()
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="node_group.created",
        entity_type="node_group",
        entity_id=group.id,
        new={"slug": slug},
        request=request,
    )
    return RedirectResponse("/admin/nodes?ok=Группа+создана", status_code=303)


# -------------------------------------------------------------- устройства


@router.get("/devices", response_class=HTMLResponse, include_in_schema=False)
async def device_list(
    request: Request,
    principal: Principal = Depends(require_section("devices")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    query_text = request.query_params.get("q", "").strip()
    page = max(int(request.query_params.get("page", "1") or 1), 1)

    query = select(Device).options(selectinload(Device.user))
    counter = select(func.count()).select_from(Device)
    if query_text:
        mac = normalize_mac(query_text)
        pattern = f"%{query_text}%"
        condition = or_(
            Device.mac == mac if mac else Device.mac.ilike(pattern),
            Device.model.ilike(pattern),
            Device.serial.ilike(pattern),
        )
        query = query.where(condition)
        counter = counter.where(condition)

    total = await session.scalar(counter) or 0
    devices = list(
        await session.scalars(
            query.order_by(Device.id.desc()).limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)
        )
    )
    online_threshold = settings.subscription.heartbeat_offline_min
    now = utcnow()

    return render(
        request,
        "devices.html",
        principal,
        devices=devices,
        query_text=query_text,
        page=page,
        pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        total=total,
        online=lambda device: device.is_online(threshold_min=online_threshold, now=now),
        commands=list(CommandType),
    )


@router.post("/devices/{device_id}/command", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def send_command(
    device_id: int,
    request: Request,
    principal: Principal = Depends(require_section("devices")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    """Команда кладётся в очередь — роутер заберёт её следующим heartbeat."""
    device = await session.get(Device, device_id)
    if device is None:
        return RedirectResponse("/admin/devices?err=Устройство+не+найдено", status_code=303)

    form = await request.form()
    try:
        command = CommandType(form_value(form, "command"))
    except ValueError:
        return RedirectResponse("/admin/devices?err=Неизвестная+команда", status_code=303)

    entry = DeviceCommand(
        device_id=device.id,
        command=command,
        status=CommandStatus.PENDING,
        created_by_admin_id=principal.admin.id,
        expires_at=utcnow() + dt.timedelta(hours=24),
    )
    session.add(entry)
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="device.command_queued",
        entity_type="device",
        entity_id=device.id,
        new={"command": str(command)},
        request=request,
    )
    return RedirectResponse("/admin/devices?ok=Команда+поставлена+в+очередь", status_code=303)


@router.post("/devices/{device_id}/status", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def change_device_status(
    device_id: int,
    request: Request,
    principal: Principal = Depends(require_section("devices")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    device = await session.get(Device, device_id)
    if device is None:
        return RedirectResponse("/admin/devices?err=Устройство+не+найдено", status_code=303)

    form = await request.form()
    action = form_value(form, "action")
    before = {"status": str(device.status), "user_id": device.user_id}

    if action == "block":
        device.status = DeviceStatus.BLOCKED
    elif action == "unblock":
        device.status = DeviceStatus.ACTIVE if device.activated_at else DeviceStatus.ASSIGNED
    elif action == "revoke":
        device.status = DeviceStatus.REVOKED
        device.user_id = None
        device.revoked_at = utcnow()
        device.sub_token_hash = None
    else:
        return RedirectResponse("/admin/devices?err=Неизвестное+действие", status_code=303)

    audit.record(
        session,
        admin_id=principal.admin.id,
        action=f"device.{action}",
        entity_type="device",
        entity_id=device.id,
        old=before,
        new={"status": str(device.status), "user_id": device.user_id},
        request=request,
    )
    log.info("admin.device_status", device_id=device.id, action=action)
    return RedirectResponse("/admin/devices?ok=Готово", status_code=303)


@router.post("/devices/{device_id}/note", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def device_note(
    device_id: int,
    request: Request,
    principal: Principal = Depends(require_section("devices")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    device = await session.get(Device, device_id)
    if device is None:
        return RedirectResponse("/admin/devices?err=Устройство+не+найдено", status_code=303)
    form = await request.form()
    device.admin_note = form_value(form, "admin_note") or None
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="device.note_saved",
        entity_type="device",
        entity_id=device.id,
        request=request,
    )
    return RedirectResponse("/admin/devices?ok=Заметка+сохранена", status_code=303)


@router.post("/devices/new", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def create_device(
    request: Request,
    principal: Principal = Depends(require_section("devices")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    """Заведение устройства на складе до отгрузки."""
    form = await request.form()
    mac = normalize_mac(form_value(form, "mac"))
    if not mac:
        return RedirectResponse("/admin/devices?err=Некорректный+MAC", status_code=303)
    exists = await session.scalar(select(Device).where(Device.mac == mac))
    if exists is not None:
        return RedirectResponse("/admin/devices?err=Такой+MAC+уже+заведён", status_code=303)

    device = Device(
        mac=mac,
        model=form_value(form, "model"),
        serial=form_value(form, "serial") or None,
        status=DeviceStatus.NEW,
    )
    session.add(device)
    await session.flush()
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="device.created",
        entity_type="device",
        entity_id=device.id,
        new={"mac": mac, "model": device.model},
        request=request,
    )
    return RedirectResponse("/admin/devices?ok=Устройство+заведено", status_code=303)
