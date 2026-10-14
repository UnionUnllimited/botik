"""Главная «на сегодня», состояние системы и общий поиск.

Данные считает основное приложение (`/api/v1/fleet/today|health|search`):
заказы, парк и подписки лежат у него. Отсюда — только отрисовка и то, что
видно лишь с этой машины: живы ли службы бота и сколько места на диске.

«На сегодня» отдаётся куском разметки, который главная подгружает сама:
ждать основное приложение при открытии главной нельзя — она нужна как раз
тогда, когда что-то не отвечает.
"""

import asyncio
import shutil
from urllib.parse import urlencode

from quart import redirect, render_template, request, url_for

from web_admin.routes.routers_fleet import _get as fleet_get

LOCAL_SERVICES = (
    ("router-bot", "Бот в Telegram", "Без него не уходят сообщения клиентам и не работают кнопки в рабочем чате."),
    ("router-webadmin", "Эта админка", ""),
)

DISK_WARN_PCT = 85
DISK_FAIL_PCT = 95
"""Место на witty-dragon. Кончится — встанут база, журналы и бэкапы разом."""


async def _service_state(unit: str) -> str:
    """`active`, `failed`, `inactive`… или `unknown`, если systemd не спросить."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", unit,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return (out or b"").decode("utf-8", "replace").strip() or "unknown"
    except (FileNotFoundError, asyncio.TimeoutError, OSError):
        return "unknown"


async def local_checks() -> list[dict]:
    checks = []
    for unit, title, hint in LOCAL_SERVICES:
        state = await _service_state(unit)
        status = {"active": "ok", "unknown": "off"}.get(state, "fail")
        checks.append({
            "key": unit,
            "title": title,
            "status": status,
            "detail": {"active": "работает", "unknown": "не спросить systemd"}.get(state, state),
            "hint": f"{hint} systemctl status {unit}".strip() if status == "fail" else "",
        })

    usage = shutil.disk_usage("/")
    pct = round(usage.used * 100 / usage.total) if usage.total else 0
    free_gb = usage.free / 1024**3
    status = "fail" if pct >= DISK_FAIL_PCT else ("warn" if pct >= DISK_WARN_PCT else "ok")
    checks.append({
        "key": "disk",
        "title": "Диск witty-dragon",
        "status": status,
        "detail": f"занято {pct} %, свободно {free_gb:.1f} ГБ",
        "hint": "Почистите старые образы: docker image prune -a" if status != "ok" else "",
    })
    return checks


def _worst(checks: list[dict]) -> str:
    states = {check.get("status") for check in checks}
    return "fail" if "fail" in states else ("warn" if "warn" in states else "ok")


def link_of(kind: str, ident: int) -> str:
    """Карточка по виду и номеру. Адреса знает только эта админка —
    у неё свой секретный путь, — поэтому ручка отдаёт вид, а не ссылку."""
    if kind == "order":
        return url_for("admin.order_shop_card", order_id=ident)
    if kind == "router":
        return url_for("admin.router_card", device_id=ident)
    if kind == "client" and ident:
        return url_for("admin.user_details", telegram_id=ident)
    if kind == "site":
        return url_for("admin.care_page")
    return ""


def attach_ops_routes(admin_bp_instance):
    @admin_bp_instance.route("/today")
    async def today_fragment():
        data, error = await fleet_get("/api/v1/fleet/today")
        return await render_template(
            "_partials/today.html",
            today=data,
            counters=data.get("counters") or {},
            groups=data.get("groups") or [],
            today_error=error,
            link_of=link_of,
        )

    # Не «/system»: страж прав ищет префикс вхождением, и «/system» поймал бы
    # «/api/system_stats» — показания главной, которые видит любой модератор.
    @admin_bp_instance.route("/system-health")
    async def system_health():
        data, error = await fleet_get("/api/v1/fleet/health")
        local = await local_checks()
        checks = data.get("checks") or []
        overall = _worst(checks + local) if not error else "fail"
        return await render_template(
            "system_health.html",
            checks=checks,
            local=local,
            overall=overall,
            health_error=error,
        )

    @admin_bp_instance.route("/search")
    async def search():
        query = (request.args.get("q") or "").strip()
        results, error = [], ""
        if query:
            data, error = await fleet_get(f"/api/v1/fleet/search?{urlencode({'q': query})}")
            results = data.get("results") or []
        # Одна находка — сразу в карточку: искали ради неё, а не ради списка.
        if len(results) == 1 and not error:
            target = link_of(results[0]["kind"], results[0]["id"])
            if target:
                return redirect(target)
        return await render_template(
            "search.html",
            query=query,
            results=results,
            search_error=error,
            link_of=link_of,
        )
