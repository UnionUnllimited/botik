"""Доступ к каталогу, заказам и складу основного приложения по HTTP.

Товары, цены и заказы лежат в его Postgres. Ни бот, ни эта админка до него
не дотягиваются: они отдельные процессы на хосте, со своими venv и без
драйвера Postgres, а база наружу не опубликована. Общение только через
его ручки `/api/v1/catalog/*`.

Адрес и токен те же, что у вкладки «Роутеры»: `FLEET_API_URL`
и `FLEET_API_TOKEN` в окружении службы. Имена остались от парка роутеров —
пара «наш процесс ↔ его API» одна и та же, и второй секрет пришлось бы
раздавать дважды ради того же доверия.

Модуль общий для бота и веб-админки: правила разбора ответа и тексты ошибок
должны совпадать, иначе одна и та же поломка выглядит в двух местах по-разному.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

READ_TIMEOUT_SEC = 8
WRITE_TIMEOUT_SEC = 25
"""Оформление заказа идёт до провайдера оплаты и занимает дольше чтения."""

NO_CONFIG = (
    "Каталог не подключён: в окружении службы нет FLEET_API_URL и FLEET_API_TOKEN. "
    "Токен берётся из API_FLEET_TOKEN основного приложения."
)


def is_configured() -> bool:
    base, token = _config()
    return bool(base and token)


def _config() -> tuple[str, str]:
    base = (os.getenv("FLEET_API_URL") or "").strip().rstrip("/")
    token = (os.getenv("FLEET_API_TOKEN") or "").strip()
    return base, token


MISSING = {
    "/api/v1/catalog/products/": "Эта модель больше не продаётся.",
    "/api/v1/catalog/orders/": "Заказ не найден.",
}
"""Ручка с пустым токеном тоже отвечает 404 — отличить её от пропавшей записи
по коду нельзя. Поэтому на путях к конкретной записи 404 читается как «нет
записи»: список товаров клиент открывает раньше карточки и про выключенный
токен узнаёт там."""


def _explain(response: httpx.Response, path: str) -> str:
    if response.status_code == 404:
        for prefix, message in MISSING.items():
            if path.startswith(prefix):
                return message
        return "Ручка каталога выключена: в основном приложении пуст API_FLEET_TOKEN."
    if response.status_code == 401:
        return "Токен не подошёл: FLEET_API_TOKEN здесь и API_FLEET_TOKEN там должны совпадать."
    return f"Основное приложение ответило {response.status_code}."


async def _request(method: str, path: str, **kwargs: Any) -> tuple[dict, str]:
    """Возвращает данные и текст ошибки — одно из двух всегда пустое."""
    base, token = _config()
    if not base or not token:
        return {}, NO_CONFIG

    timeout = READ_TIMEOUT_SEC if method == "GET" else WRITE_TIMEOUT_SEC
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method, f"{base}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
    except httpx.HTTPError as exc:
        return {}, f"Основное приложение не ответило: {exc}"

    if response.status_code != 200:
        return {}, _explain(response, path)
    try:
        return response.json(), ""
    except ValueError:
        return {}, "Основное приложение вернуло не JSON."


async def get(path: str, params: dict | None = None) -> tuple[dict, str]:
    return await _request("GET", path, params=params or {})


async def post(path: str, payload: dict) -> tuple[dict, str]:
    """Отказ по делу отделяем от обрыва связи: текст в `error` уже написан
    для человека, и добавлять к нему «сервис недоступен» неправильно."""
    data, error = await _request("POST", path, json=payload)
    if error:
        return {}, error
    if data.get("ok") is False:
        return {}, data.get("error") or "Не получилось."
    return data, ""


async def upload(path: str, files: dict) -> tuple[dict, str]:
    data, error = await _request("POST", path, files=files)
    if error:
        return {}, error
    if data.get("ok") is False:
        return {}, data.get("error") or "Не получилось."
    return data, ""


# --- Каталог -----------------------------------------------------------------


async def products(*, include_hidden: bool = False) -> tuple[list[dict], str]:
    data, error = await get("/api/v1/catalog/products", {"all": "1"} if include_hidden else None)
    return data.get("products", []), error


async def product(product_id: int) -> tuple[dict, str]:
    data, error = await get(f"/api/v1/catalog/products/{product_id}")
    return data.get("product", {}), error


async def save_product(product_id: int, payload: dict) -> tuple[dict, str]:
    return await post(f"/api/v1/catalog/products/{product_id}", payload)


async def upload_photo(product_id: int, filename: str, content: bytes, content_type: str):
    return await upload(
        f"/api/v1/catalog/products/{product_id}/photo",
        {"photo": (filename, content, content_type)},
    )


async def delete_product(product_id: int) -> tuple[dict, str]:
    return await post(f"/api/v1/catalog/products/{product_id}/delete", {})


async def delivery_options() -> tuple[dict, str]:
    return await get("/api/v1/catalog/delivery")


# --- Заказы ------------------------------------------------------------------


async def validate_field(field: str, value: str) -> tuple[str, str]:
    """Возвращает причёсанное значение и текст претензии — одно из двух пустое."""
    data, error = await _request("POST", "/api/v1/catalog/validate", json={"field": field, "value": value})
    if error:
        return "", error
    if not data.get("ok"):
        return "", data.get("error") or "Неверное значение."
    return str(data.get("value", "")), ""


async def quote(payload: dict) -> tuple[dict, str]:
    return await post("/api/v1/catalog/orders/quote", payload)


async def create_order(payload: dict) -> tuple[dict, str]:
    return await post("/api/v1/catalog/orders", payload)


async def orders_of(tg_id: int, limit: int = 10) -> tuple[list[dict], str]:
    data, error = await get("/api/v1/catalog/orders", {"tg_id": tg_id, "limit": limit})
    return data.get("orders", []), error


async def order_card(order_id: int, tg_id: int) -> tuple[dict, str]:
    data, error = await get(f"/api/v1/catalog/orders/{order_id}", {"tg_id": tg_id})
    return data, error


async def cancel_order(order_id: int, tg_id: int) -> tuple[dict, str]:
    return await post(f"/api/v1/catalog/orders/{order_id}/cancel", {"tg_id": tg_id})


# --- Клиент и его роутер -----------------------------------------------------


async def register_client(tg_id: int, username: str, first_name: str) -> tuple[dict, str]:
    """Отмечает клиента в базе основного приложения.

    Нужно до заказа: роутер привязывает оператор по MAC при отгрузке, и строка
    в `users` должна к тому моменту существовать.
    """
    return await post(
        "/api/v1/catalog/clients",
        {"tg_id": tg_id, "username": username, "first_name": first_name},
    )


async def my_router(tg_id: int) -> tuple[dict, str]:
    return await get("/api/v1/catalog/my-router", {"tg_id": tg_id})


# --- Заказы: сторона оператора -----------------------------------------------


async def manage_orders(*, status: str = "", query: str = "", page: int = 1) -> tuple[dict, str]:
    return await get(
        "/api/v1/catalog/manage/orders", {"status": status, "q": query, "page": page}
    )


async def manage_order(order_id: int) -> tuple[dict, str]:
    return await get(f"/api/v1/catalog/manage/orders/{order_id}")


async def set_order_status(order_id: int, status: str, reason: str) -> tuple[dict, str]:
    return await post(
        f"/api/v1/catalog/manage/orders/{order_id}/status", {"status": status, "reason": reason}
    )


async def set_order_tracking(order_id: int, track: str) -> tuple[dict, str]:
    return await post(
        f"/api/v1/catalog/manage/orders/{order_id}/shipping", {"tracking_number": track}
    )


async def attach_order_device(order_id: int, mac: str, model: str) -> tuple[dict, str]:
    return await post(
        f"/api/v1/catalog/manage/orders/{order_id}/device", {"mac": mac, "model": model}
    )


async def set_order_note(order_id: int, note: str) -> tuple[dict, str]:
    return await post(f"/api/v1/catalog/manage/orders/{order_id}/note", {"note": note})


async def delivery_settings() -> tuple[dict, str]:
    return await get("/api/v1/catalog/manage/delivery")


async def save_delivery_settings(payload: dict) -> tuple[dict, str]:
    return await post("/api/v1/catalog/manage/delivery", payload)


# --- Склад устройств ---------------------------------------------------------


async def stock(*, query: str = "", page: int = 1) -> tuple[dict, str]:
    return await get("/api/v1/fleet/devices", {"q": query, "page": page})


async def stock_add(mac: str, model: str, serial: str) -> tuple[dict, str]:
    return await post("/api/v1/fleet/devices", {"mac": mac, "model": model, "serial": serial})


async def set_device_status(device_id: int, status: str) -> tuple[dict, str]:
    return await post(f"/api/v1/fleet/routers/{device_id}/status", {"status": status})


async def set_device_note(device_id: int, note: str) -> tuple[dict, str]:
    return await post(f"/api/v1/fleet/routers/{device_id}/note", {"note": note})
