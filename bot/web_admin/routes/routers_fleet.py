"""Вкладка «Роутеры»: парк устройств из основного приложения.

Данные лежат в Postgres основного проекта, а эта админка — отдельный процесс
со своим venv и без драйвера Postgres, да и база наружу не опубликована.
Поэтому всё идёт по HTTP на его ручки `/api/v1/fleet/*`.

Действия — опрос, активация, продление, привязка клиента — исполняет он же,
а не мы: туннель к роутеру держит его контейнер, и дотянуться до роутера может
только процесс в его сети. Мы отсюда лишь нажимаем кнопку.
"""

import os

import httpx
from quart import flash, redirect, render_template, request, url_for

FLEET_TIMEOUT_SEC = 8
ACTION_TIMEOUT_SEC = 90
"""Активация идёт до самого роутера по SSH и занимает до минуты."""


def _fleet_config() -> tuple[str, str]:
    """Адрес API и токен. Задаются переменными окружения службы админки."""
    base = (os.getenv("FLEET_API_URL") or "").strip().rstrip("/")
    token = (os.getenv("FLEET_API_TOKEN") or "").strip()
    return base, token


def _explain(response: httpx.Response) -> str:
    if response.status_code == 404:
        return "Ручка парка выключена: в основном приложении пуст API_FLEET_TOKEN."
    if response.status_code == 401:
        return "Токен не подошёл: FLEET_API_TOKEN здесь и API_FLEET_TOKEN там должны совпадать."
    return f"Основное приложение ответило {response.status_code}."


async def _get(path: str) -> tuple[dict, str]:
    """Читает ручку парка. Возвращает данные и текст ошибки — одно из двух пустое."""
    base, token = _fleet_config()
    if not base or not token:
        return {}, (
            "Не заданы FLEET_API_URL и FLEET_API_TOKEN в окружении админки. "
            "Токен берётся из API_FLEET_TOKEN основного приложения."
        )

    try:
        async with httpx.AsyncClient(timeout=FLEET_TIMEOUT_SEC) as client:
            response = await client.get(
                f"{base}{path}", headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError as exc:
        return {}, f"Основное приложение не ответило: {exc}"

    if response.status_code != 200:
        return {}, _explain(response)
    try:
        return response.json(), ""
    except ValueError:
        return {}, "Основное приложение вернуло не JSON."


async def _post(path: str, payload: dict) -> tuple[dict, str]:
    base, token = _fleet_config()
    if not base or not token:
        return {}, "Не заданы FLEET_API_URL и FLEET_API_TOKEN в окружении админки."

    try:
        async with httpx.AsyncClient(timeout=ACTION_TIMEOUT_SEC) as client:
            response = await client.post(
                f"{base}{path}", json=payload, headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError as exc:
        return {}, f"Основное приложение не ответило: {exc}"

    if response.status_code != 200:
        return {}, _explain(response)
    try:
        data = response.json()
    except ValueError:
        return {}, "Основное приложение вернуло не JSON."
    # Отказ по делу — не ошибка связи: текст уже написан для человека.
    return (data, "") if data.get("ok") else ({}, data.get("error") or "Не получилось.")


async def fetch_fleet() -> tuple[dict, str]:
    return await _get("/api/v1/fleet/routers")


def _days_from(form) -> int:
    try:
        return max(min(int(form.get("days", 30)), 3650), 1)
    except (TypeError, ValueError):
        return 30


def attach_routers_fleet_routes(admin_bp_instance, query_db_func, execute_db_func):
    @admin_bp_instance.route("/routers")
    async def routers_fleet():
        data, error = await fetch_fleet()
        options, _ = await _get("/api/v1/fleet/settings")
        return await render_template(
            "routers_fleet.html",
            fleet=data,
            routers=data.get("routers", []),
            fleet_error=error,
            auto_enabled=options.get("auto_enabled", False),
            auto_days=options.get("auto_days", 30),
        )

    @admin_bp_instance.route("/routers/settings", methods=["POST"])
    async def routers_fleet_settings():
        form = await request.form
        _, error = await _post(
            "/api/v1/fleet/settings",
            {"auto_enabled": form.get("auto_enabled") == "on", "auto_days": form.get("auto_days", 30)},
        )
        await flash(error or "Настройки сохранены.", "danger" if error else "success")
        return redirect(url_for("admin.routers_fleet"))

    async def _render_card(device_id: int, *, console_output: str = "", console_command: str = ""):
        data, error = await _get(f"/api/v1/fleet/routers/{device_id}")
        return await render_template(
            "router_card.html",
            device_id=device_id,
            card=data,
            router=data.get("router", {}),
            client=data.get("client", {}),
            subscription=data.get("subscription", {}),
            panel=data.get("panel", {}),
            events=data.get("events", []),
            fleet_error=error,
            console_output=console_output,
            console_command=console_command,
        )

    @admin_bp_instance.route("/routers/<int:device_id>")
    async def router_card(device_id: int):
        return await _render_card(device_id)

    @admin_bp_instance.route("/routers/<int:device_id>/console", methods=["POST"])
    async def router_console(device_id: int):
        """Вывод показываем на той же странице, а не редиректом: он длинный,
        и при перезагрузке терялся бы вместе с ответом роутера."""
        form = await request.form
        command = (form.get("command") or "").strip()
        data, error = await _post(f"/api/v1/fleet/routers/{device_id}/console", {"command": command})
        if error:
            await flash(error, "danger")
            return await _render_card(device_id, console_command=command)
        return await _render_card(
            device_id, console_output=data.get("output") or "(пусто)", console_command=command
        )

    async def _act(device_id: int, path: str, payload: dict, ok_message: str):
        data, error = await _post(f"/api/v1/fleet/routers/{device_id}{path}", payload)
        await flash(error or ok_message, "danger" if error else "success")
        return redirect(url_for("admin.router_card", device_id=device_id))

    @admin_bp_instance.route("/routers/<int:device_id>/panel", methods=["POST"])
    async def router_panel(device_id: int):
        """Веб-панель роутера.

        Проксирует её основное приложение и будет проксировать дальше: туннель
        держит его контейнер `frpc`, и до роутера отсюда не дотянуться никак.
        Переехал вход — оно выдаёт разовую ссылку, а мы отправляем по ней браузер.
        Вход в его админку для этого не нужен.
        """
        data, error = await _post(f"/api/v1/fleet/routers/{device_id}/panel-ticket", {})
        if error:
            await flash(error, "danger")
            return redirect(url_for("admin.router_card", device_id=device_id))
        return redirect(data.get("url") or url_for("admin.router_card", device_id=device_id))

    @admin_bp_instance.route("/routers/<int:device_id>/poll", methods=["POST"])
    async def router_poll(device_id: int):
        return await _act(device_id, "/poll", {}, "Показания обновлены.")

    @admin_bp_instance.route("/routers/<int:device_id>/activate", methods=["POST"])
    async def router_activate(device_id: int):
        form = await request.form
        days = _days_from(form)
        return await _act(device_id, "/activate", {"days": days}, f"Роутер активирован на {days} дн.")

    @admin_bp_instance.route("/routers/<int:device_id>/extend", methods=["POST"])
    async def router_extend(device_id: int):
        form = await request.form
        days = _days_from(form)
        return await _act(device_id, "/extend", {"days": days}, f"Срок продлён на {days} дн.")

    @admin_bp_instance.route("/routers/<int:device_id>/bind", methods=["POST"])
    async def router_bind(device_id: int):
        form = await request.form
        return await _act(
            device_id, "/bind", {"client": form.get("client", "")}, "Клиент привязан."
        )

    @admin_bp_instance.route("/routers/<int:device_id>/unbind", methods=["POST"])
    async def router_unbind(device_id: int):
        return await _act(device_id, "/unbind", {}, "Клиент отвязан.")
