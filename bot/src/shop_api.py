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
from urllib.parse import urlsplit

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


def landing_url(configured: str = "") -> str:
    """Адрес витрины для кнопки в меню.

    Настройка оператора важнее: витрина переедет на свой домен, а ходить
    к ручкам мы будем по прежнему адресу. Пока она пуста, берём корень
    того же домена, где живёт API, — витрина стоит там же и настраивать
    ничего не нужно.

    Значение настройки приходит аргументом: этот модуль читают и бот,
    и админка, а до их таблицы настроек ему дела нет.
    """
    custom = (configured or "").strip()
    if custom:
        return custom.rstrip("/")
    base, _token = _config()
    if not base:
        return ""
    parts = urlsplit(base)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


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


async def download(path: str, params: dict | None = None) -> tuple[bytes, str]:
    """Файл как есть: содержимое и текст ошибки — одно из двух всегда пустое.

    Отдельно от `get`, потому что тот разбирает ответ как JSON. Выгрузку надо
    отдать браузеру байт в байт: перекодировав её по дороге, мы сломаем и BOM,
    на который смотрит Excel, и переносы строк внутри адресов.
    """
    base, token = _config()
    if not base or not token:
        return b"", NO_CONFIG
    try:
        async with httpx.AsyncClient(timeout=WRITE_TIMEOUT_SEC) as client:
            response = await client.get(
                f"{base}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params or {},
            )
    except httpx.HTTPError as exc:
        return b"", f"Основное приложение не ответило: {exc}"
    if response.status_code != 200:
        return b"", _explain(response, path)
    return response.content, ""


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


async def upload_banner(filename: str, content: bytes, content_type: str) -> tuple[dict, str]:
    """Картинка над главным меню. Возвращает готовый адрес для настройки."""
    return await upload("/api/v1/catalog/manage/banner", {"banner": (filename, content, content_type)})


async def upload_landing_image(
    kind: str, filename: str, content: bytes, content_type: str
) -> tuple[dict, str]:
    """Логотип витрины («logo») или значок вкладки («favicon»)."""
    return await upload(
        f"/api/v1/catalog/manage/landing-image/{kind}",
        {"image": (filename, content, content_type)},
    )


async def landing_settings() -> tuple[dict, str]:
    """Что сейчас стоит у витрины: адреса логотипа и значка вкладки."""
    return await get("/api/v1/catalog/manage/landing")


async def delete_product(product_id: int) -> tuple[dict, str]:
    return await post(f"/api/v1/catalog/products/{product_id}/delete", {})


async def plans(*, include_hidden: bool = False) -> tuple[list[dict], str]:
    """Сроки подписки: их выбирают вместе с роутером."""
    data, error = await get("/api/v1/catalog/plans", {"all": "1"} if include_hidden else None)
    return data.get("plans", []), error


async def sync_plans(tariffs: list[dict]) -> tuple[dict, str]:
    """Отправляет тарифы бота в каталог: они там становятся сроками подписки."""
    return await post("/api/v1/catalog/plans/sync", {"tariffs": tariffs})


async def save_plan(plan_id: int, payload: dict) -> tuple[dict, str]:
    return await post(f"/api/v1/catalog/plans/{plan_id}", payload)


async def delete_plan(plan_id: int) -> tuple[dict, str]:
    return await post(f"/api/v1/catalog/plans/{plan_id}/delete", {})


async def delivery_options() -> tuple[dict, str]:
    """Варианты доставки: скорость и её описание, без цен.

    Цену называет оператор после оформления — она зависит от города
    и габаритов, и обещать её при заказе было бы нечестно."""
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


async def order_payment_link(order_id: int, tg_id: int) -> tuple[dict, str]:
    """Свежая ссылка на оплату самого заказа.

    Нужна и когда провайдер не ответил при оформлении (заказ принят без
    ссылки), и когда клиент вернулся к нему через час: ссылка живёт
    пятнадцать минут.
    """
    return await post(f"/api/v1/catalog/orders/{order_id}/payment", {"tg_id": tg_id})


async def delivery_payment_link(order_id: int, tg_id: int) -> tuple[dict, str]:
    """Свежая ссылка на оплату доставки: прежняя живёт пятнадцать минут."""
    return await post(f"/api/v1/catalog/orders/{order_id}/delivery-payment", {"tg_id": tg_id})


async def subscriptions_snapshot() -> tuple[list[dict], str]:
    """Все подписки клиентов — для зеркала в нашей базе."""
    data, error = await get("/api/v1/catalog/subscriptions")
    return data.get("subscriptions", []), error


async def renew_state(tg_id: int) -> tuple[dict, str]:
    """Текущий срок и периоды для экрана продления."""
    return await get("/api/v1/catalog/renew", {"tg_id": tg_id})


async def renew_start(tg_id: int, plan_id: int) -> tuple[dict, str]:
    return await post("/api/v1/catalog/renew", {"tg_id": tg_id, "plan_id": plan_id})


async def orders_of(tg_id: int, limit: int = 10) -> tuple[list[dict], str]:
    data, error = await get("/api/v1/catalog/orders", {"tg_id": tg_id, "limit": limit})
    return data.get("orders", []), error


async def order_card(order_id: int, tg_id: int) -> tuple[dict, str]:
    data, error = await get(f"/api/v1/catalog/orders/{order_id}", {"tg_id": tg_id})
    return data, error


async def cancel_order(order_id: int, tg_id: int) -> tuple[dict, str]:
    return await post(f"/api/v1/catalog/orders/{order_id}/cancel", {"tg_id": tg_id})


# --- Очередь сообщений -------------------------------------------------------


async def outbox(limit: int = 20) -> tuple[dict, str]:
    """Что основное приложение просит отправить клиентам."""
    return await get("/api/v1/catalog/outbox", {"limit": limit})


async def outbox_ack(
    message_id: int,
    *,
    ok: bool,
    error: str = "",
    blocked: bool = False,
    thread_id: int = 0,
):
    """Отчёт о судьбе сообщения: без него оно будет предложено снова.

    `thread_id` — номер только что созданного топика заказа. Его запоминает
    основное приложение: без этого следующее сообщение завело бы заказу
    второй топик, и переписка разъехалась бы на две ветки.
    """
    payload = {"ok": ok, "error": error, "blocked": blocked}
    if thread_id:
        payload["thread_id"] = thread_id
    return await post(f"/api/v1/catalog/outbox/{message_id}/ack", payload)


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


async def my_router(tg_id: int, device_id: int = 0) -> tuple[dict, str]:
    """Экран роутера. `device_id` выбирает, чьи показания разворачивать:
    роутеров у клиента может быть несколько."""
    params = {"tg_id": tg_id}
    if device_id:
        params["device_id"] = device_id
    return await get("/api/v1/catalog/my-router", params)


async def update_router(tg_id: int, device_id: int = 0) -> tuple[dict, str]:
    """Просит роутер обновить прошивку, не дожидаясь суточного круга.

    Ответ приходит сразу: команда на устройстве уходит в фон, и «запущено» —
    это всё, что известно в пределах запроса.
    """
    payload = {"tg_id": tg_id}
    if device_id:
        payload["device_id"] = device_id
    return await post("/api/v1/catalog/my-router/update", payload)


async def my_router_available(tg_id: int) -> bool:
    """Показывать ли кнопку «Мой роутер» — есть ли у клиента роутер или заказ.

    Спрашивается на каждой отрисовке главного меню, поэтому ходит в отдельную
    лёгкую ручку, а не в полный экран: тот ждёт у панели срок подписки.

    При ошибке кнопка показывается. Спрятать её из-за недоступного API значит
    отобрать у владельца роутера единственный вход к его устройству; лишняя
    кнопка у того, кто ничего не покупал, — заметно меньшая беда.
    """
    data, error = await get("/api/v1/catalog/my-router/available", {"tg_id": tg_id})
    if error:
        return True
    return bool(data.get("show"))


# --- Роутеры клиента ---------------------------------------------------------


async def clients() -> tuple[list[dict], str]:
    """Клиенты для выпадающего списка привязки."""
    data, error = await get("/api/v1/fleet/clients")
    return data.get("clients", []), error


async def routers_of_clients(tg_ids: list[int]) -> tuple[dict, dict, dict, str]:
    """Роутеры, подписки и трафик по списку клиентов — одним запросом.

    Всё это их колонки заполнить не могут: подписка и роутер живут в основном
    приложении, а трафик писала служба аналитики, которой на сервере нет.
    Возвращает три карты по tg_id и текст ошибки.
    """
    if not tg_ids:
        return {}, {}, {}, ""
    data, error = await get(
        "/api/v1/fleet/clients/routers", {"tg_ids": ",".join(str(item) for item in tg_ids)}
    )
    return data.get("routers", {}), data.get("subscriptions", {}), data.get("traffic", {}), error


async def client_routers(tg_id: int) -> tuple[dict, str]:
    return await get(f"/api/v1/fleet/clients/{tg_id}/routers")


async def bind_client_router(tg_id: int, mac: str, model: str = "") -> tuple[dict, str]:
    return await post(f"/api/v1/fleet/clients/{tg_id}/routers", {"mac": mac, "model": model})


async def unbind_client_router(tg_id: int, device_id: int) -> tuple[dict, str]:
    return await post(f"/api/v1/fleet/clients/{tg_id}/routers/{device_id}/unbind", {})


# --- Заказы: сторона оператора -----------------------------------------------


async def manage_orders(
    *, status: str = "", query: str = "", page: int = 1, sort: str = "", direction: str = ""
) -> tuple[dict, str]:
    return await get(
        "/api/v1/catalog/manage/orders",
        {"status": status, "q": query, "page": page, "sort": sort, "dir": direction},
    )


async def manage_payments(*, status: str = "", query: str = "", page: int = 1) -> tuple[dict, str]:
    """Наши платежи: за роутеры и доставку. У бота свой раздел — там подписка."""
    return await get(
        "/api/v1/catalog/manage/payments", {"status": status, "q": query, "page": page}
    )


async def cancel_payment(payment_id: int) -> tuple[dict, str]:
    """Гасит висящий платёж. Оплаченный основное приложение не отдаст."""
    return await post(f"/api/v1/catalog/manage/payments/{payment_id}/cancel", {})


async def export_orders(*, status: str = "", query: str = "") -> tuple[bytes, str]:
    """Выгрузка заказов в CSV — под теми же фильтрами, что и список."""
    return await download(
        "/api/v1/catalog/manage/orders/export", {"status": status, "q": query}
    )


async def manage_order(order_id: int) -> tuple[dict, str]:
    return await get(f"/api/v1/catalog/manage/orders/{order_id}")


async def set_order_status(order_id: int, status: str, reason: str) -> tuple[dict, str]:
    """Перевод заказа оператором.

    `force` — потому что это человек: схема переходов писана для автоматики,
    а у него на руках возврат, отказ или заказ, закрытый раньше времени.
    """
    return await post(
        f"/api/v1/catalog/manage/orders/{order_id}/status",
        {"status": status, "reason": reason, "force": True},
    )


async def quote_delivery(
    order_id: int, price: str, days: str, method: str = "", speed: str = ""
) -> tuple[dict, str]:
    """Называет цену доставки: заводит счёт клиенту и текст уведомления.

    Перевозчик передаётся здесь же: клиент выбирал скорость, а кем везти —
    решает оператор, когда видит адрес и вес.
    """
    return await post(
        f"/api/v1/catalog/manage/orders/{order_id}/delivery-quote",
        {"price": price.replace(",", "."), "days": days, "method": method, "speed": speed},
    )


async def delete_order(order_id: int) -> tuple[dict, str]:
    """Удаляет заказ насовсем. Оплаченные основное приложение не отдаст."""
    return await post(f"/api/v1/catalog/manage/orders/{order_id}/delete", {})


async def set_order_tracking(order_id: int, track: str) -> tuple[dict, str]:
    return await post(
        f"/api/v1/catalog/manage/orders/{order_id}/shipping", {"tracking_number": track}
    )


async def attach_order_device(order_id: int, mac: str, model: str) -> tuple[dict, str]:
    return await post(
        f"/api/v1/catalog/manage/orders/{order_id}/device", {"mac": mac, "model": model}
    )


async def set_order_customer(
    order_id: int, *, name: str, phone: str, city: str, address: str, reason: str
) -> tuple[dict, str]:
    """Правка данных получателя. Причина обязательна — её спрашивает ручка."""
    return await post(
        f"/api/v1/catalog/manage/orders/{order_id}/customer",
        {
            "name": name,
            "phone": phone,
            "city": city,
            "address": address,
            "reason": reason,
        },
    )


async def set_order_note(order_id: int, note: str) -> tuple[dict, str]:
    return await post(f"/api/v1/catalog/manage/orders/{order_id}/note", {"note": note})


# --- Склад устройств ---------------------------------------------------------


async def stock(*, query: str = "", page: int = 1, show_all: bool = False) -> tuple[dict, str]:
    """По умолчанию только то, что можно отгрузить: уехавшее к клиенту со склада ушло."""
    params = {"q": query, "page": page}
    if show_all:
        params["all"] = "1"
    return await get("/api/v1/fleet/devices", params)


async def stock_add(mac: str, model: str, serial: str) -> tuple[dict, str]:
    return await post("/api/v1/fleet/devices", {"mac": mac, "model": model, "serial": serial})


async def set_device_status(device_id: int, status: str) -> tuple[dict, str]:
    return await post(f"/api/v1/fleet/routers/{device_id}/status", {"status": status})


async def set_device_note(device_id: int, note: str) -> tuple[dict, str]:
    return await post(f"/api/v1/fleet/routers/{device_id}/note", {"note": note})


# --- Топики заказов в рабочем чате -------------------------------------------
#
# Карточку собирает основное приложение: она рисуется в двух местах — первым
# сообщением в топике и после каждого нажатия, — и разъехавшись, показывала бы
# оператору одно, а делала другое.


async def order_topics_settings() -> tuple[dict, str]:
    """Какой чат сейчас задан под топики заказов."""
    return await get("/api/v1/catalog/manage/order-topics")


async def save_order_topics_chat(chat_id: str) -> tuple[dict, str]:
    return await post("/api/v1/catalog/manage/order-topics", {"chat_id": chat_id})


async def order_topic_card(order_id: int) -> tuple[dict, str]:
    """Свежий текст и кнопки карточки заказа."""
    return await get(f"/api/v1/catalog/manage/orders/{order_id}/topic-card")


async def order_topic_push(order_id: int) -> tuple[dict, str]:
    """Отправить заказ в рабочий чат — для заказов старше самих топиков."""
    return await post(f"/api/v1/catalog/manage/orders/{order_id}/topic", {})


# --- Обновление роутеров -----------------------------------------------------
#
# Раздачей прошивки занимается основное приложение: манифест отдаётся с его
# домена, образы лежат в его томе. Отсюда только страница оператора.
#
# Сам файл сюда не попадает: браузер отправляет его прямо туда по разовой
# ссылке. Образ весит 27–54 МБ, и перегонять его через эту службу значило бы
# положить его в память дважды и упереться в WRITE_TIMEOUT_SEC.


async def firmware_state() -> tuple[dict, str]:
    """Всё для страницы: модели, черновик, что раздаётся сейчас, история."""
    return await get("/api/v1/fleet/firmware")


async def firmware_create_release(version: str, notes: str, author: str) -> tuple[dict, str]:
    return await post(
        "/api/v1/fleet/firmware/releases",
        {"version": version, "notes": notes, "author": author},
    )


async def firmware_upload_ticket(release_id: int, model: str) -> tuple[dict, str]:
    """Разовая ссылка для отправки образа прямо в основное приложение."""
    return await post(
        f"/api/v1/fleet/firmware/releases/{release_id}/upload-ticket", {"model": model}
    )


async def firmware_set_rollout(release_id: int, rollout: str) -> tuple[dict, str]:
    return await post(
        f"/api/v1/fleet/firmware/releases/{release_id}/rollout", {"rollout": rollout}
    )


async def firmware_publish(release_id: int, rollout: str) -> tuple[dict, str]:
    return await post(
        f"/api/v1/fleet/firmware/releases/{release_id}/publish", {"rollout": rollout}
    )


async def firmware_delete_image(release_id: int, model: str) -> tuple[dict, str]:
    return await post(
        f"/api/v1/fleet/firmware/releases/{release_id}/image-delete", {"model": model}
    )


async def firmware_delete_release(release_id: int) -> tuple[dict, str]:
    return await post(f"/api/v1/fleet/firmware/releases/{release_id}/delete", {})


# --- Промокоды каталога ------------------------------------------------------
#
# Скидки на железо считает основное приложение вместе с ценой заказа. У бота
# свои промокоды, на подписку, и это разные вещи: один даёт скидку на роутер
# в посылке, другой — дни к сроку.


async def promos() -> tuple[list[dict], str]:
    data, error = await get("/api/v1/catalog/manage/promos")
    return data.get("promos", []), error


async def promo_create(payload: dict) -> tuple[dict, str]:
    return await post("/api/v1/catalog/manage/promos", payload)


async def promo_action(promo_id: int, action: str) -> tuple[dict, str]:
    return await post(f"/api/v1/catalog/manage/promos/{promo_id}/{action}", {})
