"""Раздел Remnawave: состояние панели и импорт её хостов в наши узлы.

Узлы заводятся в панели — там же, где живут сами серверы. Дублировать адреса
и ключи руками во второй раз значит гарантированно разъехаться, поэтому
импорт односторонний: панель → наши узлы. Обратно мы ничего не пишем.

Связь между записями держится в `Node.config["remnawave"]["host_uuid"]`:
отдельной колонки под это нет намеренно — миграция ради ссылки на внешнюю
систему, которую в проекте может и не быть, не окупается.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote_plus

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin import audit
from api.admin.auth import Principal, form_value, require_section, verify_csrf
from api.admin.templating import render
from api.deps import get_session, get_transaction
from core.config import settings
from core.dates import utcnow
from core.enums import NodeProtocol, NodeStatus
from core.models import Node
from core.services import remnawave

router = APIRouter()
log = structlog.get_logger("admin.remnawave")

MAX_REMARKS_LEN = 120


def _quote(text: str) -> str:
    """Сообщение уезжает в query-строку редиректа — как в остальных разделах."""
    return quote_plus(text[:200])


def host_uuid_of(node: Node) -> str:
    """UUID хоста панели, из которого узел импортирован. Пусто — узел свой."""
    link = (node.config or {}).get("remnawave")
    return str(link.get("host_uuid", "")) if isinstance(link, dict) else ""


def remarks_for(remark: str) -> str:
    """Имя узла в подписке: префикс обязателен, иначе роутер отбросит узел."""
    prefix = settings.subscription.node_prefix
    cleaned = " ".join(remark.split()) or "node"
    if not cleaned.startswith(prefix):
        cleaned = f"{prefix}{cleaned}"
    return cleaned[:MAX_REMARKS_LEN]


def protocol_for(config: dict[str, Any]) -> NodeProtocol:
    """Протокол по параметрам хоста: reality узнаём по ключам, остальное — ws+tls."""
    blob = json.dumps(config, ensure_ascii=False).lower()
    if "reality" in blob or "publickey" in blob or "public_key" in blob:
        return NodeProtocol.VLESS_REALITY
    return NodeProtocol.VLESS_WS_TLS


@router.get("/remnawave", response_class=HTMLResponse, include_in_schema=False)
async def panel(
    request: Request,
    principal: Principal = Depends(require_section("remnawave")),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    client = remnawave.client()
    status = await client.probe()

    nodes: list[remnawave.RemnaNode] = []
    hosts: list[remnawave.RemnaHost] = []
    if status.ok:
        # probe уже сходил за этими данными, но повторный запрос дешевле,
        # чем тащить их через объект статуса и путать его назначение.
        try:
            nodes = await client.nodes()
            hosts = await client.hosts()
        except remnawave.RemnawaveError as exc:
            status.ok = False
            status.error = str(exc)

    ours = list(await session.scalars(select(Node).order_by(Node.priority, Node.id)))
    imported = {host_uuid_of(node): node for node in ours if host_uuid_of(node)}

    return render(
        request,
        "remnawave.html",
        principal,
        status=status,
        nodes=nodes,
        hosts=hosts,
        imported=imported,
        our_nodes_total=len(ours),
        node_prefix=settings.subscription.node_prefix,
        base_url=settings.remnawave.base_url,
        paths={
            "статистика": settings.remnawave.stats_path,
            "узлы": settings.remnawave.nodes_path,
            "хосты": settings.remnawave.hosts_path,
        },
    )


@router.post("/remnawave/import", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def import_hosts(
    request: Request,
    principal: Principal = Depends(require_section("remnawave")),
    session: AsyncSession = Depends(get_transaction),
) -> RedirectResponse:
    """Создаёт или обновляет наши узлы по хостам панели."""
    form = await request.form()
    selected = {str(value) for value in form.getlist("host_uuid")}
    include_disabled = form_value(form, "include_disabled") == "on"

    try:
        hosts = await remnawave.client().hosts()
    except remnawave.RemnawaveError as exc:
        return RedirectResponse(f"/admin/remnawave?err={_quote(str(exc))}", status_code=303)

    if selected:
        hosts = [host for host in hosts if host.uuid in selected]
    if not include_disabled:
        hosts = [host for host in hosts if not host.is_disabled]
    if not hosts:
        return RedirectResponse("/admin/remnawave?err=Нечего+импортировать", status_code=303)

    ours = list(await session.scalars(select(Node)))
    by_host_uuid = {host_uuid_of(node): node for node in ours if host_uuid_of(node)}
    by_remarks = {node.remarks: node for node in ours}

    created = updated = skipped = 0
    for host in hosts:
        if not host.address:
            skipped += 1
            continue

        remarks = remarks_for(host.remark)
        node = by_host_uuid.get(host.uuid)
        if node is None:
            collision = by_remarks.get(remarks)
            if collision is not None and host_uuid_of(collision):
                # Имя занято узлом из другого хоста панели — не перетираем чужое.
                skipped += 1
                continue
            node = collision

        config = dict(host.connection_config)
        config["remnawave"] = {"host_uuid": host.uuid, "synced_at": utcnow().isoformat()}
        if host.inbound_uuid:
            config["remnawave"]["inbound_uuid"] = host.inbound_uuid

        if node is None:
            node = Node(
                remarks=remarks,
                host=host.address,
                port=host.port or 443,
                protocol=protocol_for(config),
                status=NodeStatus.DISABLED if host.is_disabled else NodeStatus.ACTIVE,
                config=config,
            )
            session.add(node)
            by_remarks[remarks] = node
            created += 1
        else:
            node.remarks = remarks
            node.host = host.address
            node.port = host.port or node.port
            node.config = config
            if host.is_disabled:
                node.status = NodeStatus.DISABLED
            updated += 1
        by_host_uuid[host.uuid] = node

    await session.flush()
    audit.record(
        session,
        admin_id=principal.admin.id,
        action="remnawave.hosts_imported",
        entity_type="node",
        new={"created": created, "updated": updated, "skipped": skipped},
        request=request,
    )
    log.info("admin.remnawave_import", created=created, updated=updated, skipped=skipped)

    message = f"Импорт: создано {created}, обновлено {updated}"
    if skipped:
        message += f", пропущено {skipped}"
    return RedirectResponse(f"/admin/remnawave?ok={_quote(message)}", status_code=303)
