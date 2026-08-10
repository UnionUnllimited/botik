"""Вкладка «Роутеры»: парк устройств из основного приложения.

Данные лежат в Postgres основного проекта, а эта админка — отдельный процесс
со своим venv и без драйвера Postgres, да и база наружу не опубликована.
Поэтому список берётся по HTTP с ручки /api/v1/fleet/routers, а не запросом
в базу: это единственный способ, которым мы вообще можем её спросить.

Только чтение. Всё, что трогает роутер — опрос, консоль, панель LuCI, —
живёт в основной админке: туннели держит её контейнер, и отсюда их не достать.
Поэтому на карточку роутера уходит ссылка туда.
"""

import os

import httpx
from quart import render_template

FLEET_TIMEOUT_SEC = 8


def _fleet_config() -> tuple[str, str]:
    """Адрес API и токен. Задаются переменными окружения службы админки."""
    base = (os.getenv("FLEET_API_URL") or "").strip().rstrip("/")
    token = (os.getenv("FLEET_API_TOKEN") or "").strip()
    return base, token


async def fetch_fleet() -> tuple[dict, str]:
    """Возвращает данные парка и текст ошибки. Одно из двух всегда пустое."""
    base, token = _fleet_config()
    if not base or not token:
        return {}, (
            "Не заданы FLEET_API_URL и FLEET_API_TOKEN в окружении админки. "
            "Токен берётся из API_FLEET_TOKEN основного приложения."
        )

    try:
        async with httpx.AsyncClient(timeout=FLEET_TIMEOUT_SEC) as client:
            response = await client.get(
                f"{base}/api/v1/fleet/routers",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        return {}, f"Основное приложение не ответило: {exc}"

    if response.status_code == 404:
        return {}, "Ручка парка выключена: в основном приложении пуст API_FLEET_TOKEN."
    if response.status_code == 401:
        return {}, "Токен не подошёл: FLEET_API_TOKEN здесь и API_FLEET_TOKEN там должны совпадать."
    if response.status_code != 200:
        return {}, f"Основное приложение ответило {response.status_code}."

    try:
        return response.json(), ""
    except ValueError:
        return {}, "Основное приложение вернуло не JSON."


def attach_routers_fleet_routes(admin_bp_instance, query_db_func, execute_db_func):
    @admin_bp_instance.route("/routers")
    async def routers_fleet():
        data, error = await fetch_fleet()
        return await render_template(
            "routers_fleet.html",
            fleet=data,
            routers=data.get("routers", []),
            fleet_error=error,
        )
