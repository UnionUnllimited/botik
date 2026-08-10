"""Общие SSH-утилиты (TCP-проба, проверка ОС) для установки Remnawave-нод."""

import asyncio
from typing import Tuple

import asyncssh


async def tcp_probe(host: str, port: int, timeout: float = 5.0) -> Tuple[bool, str]:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port)),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True, f"TCP {host}:{port} доступен"
    except Exception as e:
        return False, f"TCP {host}:{port} недоступен: {e}"


async def check_os(conn: asyncssh.SSHClientConnection) -> Tuple[bool, str]:
    result = await conn.run("uname -s 2>/dev/null || echo unknown", check=False)
    os_name = (result.stdout or "").strip() or "unknown"
    if result.exit_status != 0 and os_name == "unknown":
        return False, "Не удалось определить ОС на сервере"
    return True, os_name
