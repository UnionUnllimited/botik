"""Генерация конфига visitor-контейнера frpc.

Роутеры публикуют свои туннели на frps как STCP-прокси. Чтобы ходить к ним,
нужен visitor на каждый роутер со своим локальным портом. Конфиг собирается
из таблицы устройств и перечитывается через admin API frpc — без перезапуска
контейнера и без обрыва работающих туннелей.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx
import structlog
from sqlalchemy import select

from core.config import settings
from core.db import session_scope
from core.models import Device

log = structlog.get_logger("worker.frpc")

CONFIG_PATH = Path("/frpc/frpc.toml")
ADMIN_URL = "http://frpc:7400/api/reload"


def render_config(devices: list[Device]) -> str:
    """TOML для frpc: общий блок плюс visitor на каждый роутер."""
    frp = settings.frp
    lines = [
        "# Файл создаётся автоматически: worker/tasks/frpc_config.py",
        "# Правки руками потеряются при следующем обновлении.",
        f'serverAddr = "{frp.server_host}"',
        f"serverPort = {frp.server_port}",
        f'auth.token = "{frp.token.get_secret_value()}"',
        f"transport.tls.enable = {str(frp.tls_enabled).lower()}",
        "",
        'webServer.addr = "0.0.0.0"',
        "webServer.port = 7400",
        "",
        'log.to = "console"',
        'log.level = "info"',
        "",
    ]

    secret = frp.stcp_secret.get_secret_value()
    if not secret:
        # Без ключа STCP visitor подключиться не сможет: пишем только общий блок,
        # чтобы контейнер жил и не заполнял лог отказами.
        lines.append("# FRP_STCP_SECRET не задан — visitor'ы не создаются")
        return "\n".join(lines)

    for device in devices:
        if not device.frp_luci_name or not device.frp_visitor_port:
            continue
        safe_name = device.mac.replace(":", "").lower()
        # Веб-панель роутера: через неё снимаются показания и открывается LuCI.
        lines += [
            "[[visitors]]",
            f'name = "visitor_{safe_name}"',
            'type = "stcp"',
            f'serverName = "{device.frp_luci_name}"',
            f'secretKey = "{secret}"',
            'bindAddr = "0.0.0.0"',
            f"bindPort = {device.frp_visitor_port}",
            "",
        ]
        # SSH: порт панели плюс смещение — отдельная колонка в базе не нужна.
        if device.frp_ssh_name:
            lines += [
                "[[visitors]]",
                f'name = "visitor_ssh_{safe_name}"',
                'type = "stcp"',
                f'serverName = "{device.frp_ssh_name}"',
                f'secretKey = "{secret}"',
                'bindAddr = "0.0.0.0"',
                f"bindPort = {device.frp_visitor_port + frp.ssh_visitor_offset}",
                "",
            ]
    return "\n".join(lines)


async def sync_frpc_config() -> int:
    """Пересобирает конфиг, если состав роутеров изменился."""
    if not settings.frp.is_configured:
        log.info("frpc.not_configured", missing=settings.frp.missing_keys)
        return 0

    async with session_scope() as session:
        devices = list(
            await session.scalars(
                select(Device)
                .where(Device.frp_luci_name.is_not(None), Device.frp_visitor_port.is_not(None))
                .order_by(Device.frp_visitor_port)
            )
        )

    if not settings.frp.stcp_secret.get_secret_value():
        log.info(
            "frpc.no_stcp_secret",
            hint="Показания с роутеров не снимаются: заполните FRP_STCP_SECRET",
        )

    content = render_config(devices)
    digest = hashlib.sha256(content.encode()).hexdigest()

    def write_if_changed() -> bool:
        # Файловые операции синхронные — уводим их с цикла событий.
        if CONFIG_PATH.exists() and hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest() == digest:
            return False
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(content, encoding="utf-8")
        return True

    try:
        if not await asyncio.to_thread(write_if_changed):
            return 0
    except OSError as exc:
        log.error(
            "frpc.config_write_failed",
            path=str(CONFIG_PATH),
            error=str(exc),
            hint="Проверьте права на том frpc_config: он должен принадлежать пользователю app",
        )
        return 0

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(ADMIN_URL)
        reloaded = response.status_code < 400
    except Exception as exc:  # noqa: BLE001 — контейнера может не быть, конфиг всё равно записан
        log.warning("frpc.reload_failed", error=str(exc))
        reloaded = False

    log.info("frpc.config_updated", visitors=len(devices), reloaded=reloaded)
    return len(devices)
