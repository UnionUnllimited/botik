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
from quart import flash, jsonify, redirect, render_template, request, url_for

from src import shop_api

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
        # Клиенты для выбора при привязке. Пустой список — не беда: форма
        # переключится на ввод вручную и скажет об этом.
        clients, _ = await shop_api.clients()
        return await render_template(
            "router_card.html",
            device_id=device_id,
            card=data,
            clients=clients,
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

    # ── Списки доменов ────────────────────────────────────────────────────────

    @admin_bp_instance.route("/lists")
    async def domain_lists_page():
        """Источники, свой список и итог прошлой сборки.

        Всё считает основное приложение: сборка идёт в его worker'е, а списки
        отдаются с его домена. Здесь только экран.
        """
        data, error = await _get("/api/v1/fleet/lists")
        return await render_template(
            "domain_lists.html",
            fleet_error=error,
            sources=data.get("sources") or [],
            manual=data.get("manual") or {},
            last_build=data.get("last_build"),
            config=data.get("config") or {},
        )

    @admin_bp_instance.route("/lists/sources", methods=["POST"])
    async def domain_source_add():
        form = await request.form
        _, error = await _post(
            "/api/v1/fleet/lists/sources",
            {
                "url": (form.get("url") or "").strip(),
                "title": (form.get("title") or "").strip(),
                "kind": (form.get("kind") or "domain").strip(),
            },
        )
        await flash(error or "Источник добавлен.", "danger" if error else "success")
        return redirect(url_for("admin.domain_lists_page"))

    @admin_bp_instance.route("/lists/sources/<int:source_id>/<action>", methods=["POST"])
    async def domain_source_action(source_id: int, action: str):
        if action not in ("toggle", "delete"):
            return redirect(url_for("admin.domain_lists_page"))
        _, error = await _post(f"/api/v1/fleet/lists/sources/{source_id}/{action}", {})
        if error:
            await flash(error, "danger")
        return redirect(url_for("admin.domain_lists_page"))

    @admin_bp_instance.route("/lists/manual/<kind>", methods=["POST"])
    async def domain_manual_save(kind: str):
        """Сохраняет свой список. Автора берём из сессии — журнала действий нет,
        а «кто добавил домен» спросят первым делом."""
        from web_admin.run import current_user

        form = await request.form
        data, error = await _post(
            f"/api/v1/fleet/lists/manual/{kind}",
            {"body": form.get("body") or "", "author": getattr(current_user, "username", "") or ""},
        )
        if error:
            await flash(error, "danger")
        else:
            await flash(f"Сохранено, строк принято: {data.get('accepted', 0)}.", "success")
        return redirect(url_for("admin.domain_lists_page"))

    @admin_bp_instance.route("/lists/config", methods=["POST"])
    async def domain_lists_config():
        form = await request.form
        _, error = await _post(
            "/api/v1/fleet/lists/config",
            {key: (form.get(key) or "") for key in (
                "lists_poll_interval_min", "lists_s3_bucket", "lists_s3_endpoint",
                "lists_s3_region", "lists_s3_prefix", "lists_s3_access_key",
                "lists_s3_secret_key",
            )},
        )
        await flash(error or "Настройки сохранены.", "danger" if error else "success")
        return redirect(url_for("admin.domain_lists_page"))

    @admin_bp_instance.route("/lists/build", methods=["POST"])
    async def domain_lists_build():
        data, error = await _post("/api/v1/fleet/lists/build", {})
        if error or not data.get("ok"):
            await flash(error or data.get("error") or "Сборка не удалась.", "danger")
        else:
            failed = data.get("failed_sources") or 0
            note = f", источников не ответило: {failed}" if failed else ""
            await flash(
                f"Собрано: доменов {data.get('domains', 0)}, подсетей {data.get('ips', 0)}{note}.",
                "warning" if failed else "success",
            )
        return redirect(url_for("admin.domain_lists_page"))

    @admin_bp_instance.route("/routers/<int:device_id>/ssh-password", methods=["POST"])
    async def router_ssh_password(device_id: int):
        """Пароль root — по кнопке, ответом на запрос страницы.

        Отдаём JSON, а не рисуем в карточке: пароль не должен лежать в HTML
        у всех, кто открыл список. Считает его основное приложение — соль
        только у него.
        """
        data, error = await _post(f"/api/v1/fleet/routers/{device_id}/ssh-password", {})
        if error:
            return jsonify({"ok": False, "error": error}), 502
        return jsonify({"ok": True, "password": data.get("password") or ""})

    @admin_bp_instance.route("/users/<int:telegram_id>/routers.json")
    async def client_routers_json(telegram_id: int):
        """Роутеры клиента для модальной карточки.

        Карточка рисуется на стороне браузера и данные берёт запросами, поэтому
        отдаём JSON. Ошибку возвращаем текстом в том же ответе — модалка покажет
        её в своём блоке и не станет молча пустой.
        """
        data, error = await shop_api.client_routers(telegram_id)
        return jsonify(
            {
                "ok": not error,
                "error": error,
                "routers": data.get("routers", []),
                "free": data.get("free", []),
                "subscription": data.get("subscription", {}),
                "panel_used_bytes": data.get("panel_used_bytes"),
            }
        )

    @admin_bp_instance.route("/users/<int:telegram_id>/router/bind", methods=["POST"])
    async def client_router_bind(telegram_id: int):
        """Привязка роутера из карточки клиента.

        Та же операция, что в карточке роутера и в заказе, но со стороны
        человека: оператор чаще открывает клиента, чем ищет устройство по MAC.
        """
        form = await request.form
        data, error = await _post(
            f"/api/v1/fleet/clients/{telegram_id}/routers",
            {"mac": (form.get("mac") or "").strip(), "model": (form.get("model") or "").strip()},
        )
        await flash(error or f"Роутер {data.get('mac', '')} привязан.", "danger" if error else "success")
        return redirect(url_for("admin.user_details", telegram_id=telegram_id))

    @admin_bp_instance.route("/users/<int:telegram_id>/router/<int:device_id>/unbind", methods=["POST"])
    async def client_router_unbind(telegram_id: int, device_id: int):
        _, error = await _post(
            f"/api/v1/fleet/clients/{telegram_id}/routers/{device_id}/unbind", {}
        )
        await flash(error or "Роутер отвязан.", "danger" if error else "success")
        return redirect(url_for("admin.user_details", telegram_id=telegram_id))

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
