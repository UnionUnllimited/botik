"""Раздел «Заказы»: покупки роутеров из каталога.

Своих заказов у этого продукта нет и не было — он продаёт подписку, а не железо
с посылкой и трек-номером. Поэтому раздел перенесён целиком из нашей админки:
список с поиском и фильтром, карточка, статусы, доставка, привязка роутера.

Данные и вся логика переходов — в основном приложении, ходим по HTTP.
Сообщение клиенту о смене статуса шлёт наш бот, а не оно: клиент разговаривает
с нами, и сообщение от другого бота он в лучшем случае не узнает.
"""

from datetime import datetime

from quart import flash, redirect, render_template, request, url_for

from src import shop_api
from tg_sender import send_telegram_message

STATUS_TITLES = {
    "new": "Новый",
    "awaiting_payment": "Ждёт оплаты",
    "paid": "Оплачен",
    "packing": "Собираем",
    "shipped": "Отправлен",
    "delivered": "Доставлен",
    "done": "Завершён",
    "cancelled": "Отменён",
    "refunded": "Возврат",
}


def attach_orders_shop_routes(admin_bp_instance, query_db_func, execute_db_func):
    @admin_bp_instance.route("/orders")
    async def orders_shop():
        status = (request.args.get("status") or "").strip()
        query = (request.args.get("q") or "").strip()
        try:
            page = max(int(request.args.get("page", 1)), 1)
        except (TypeError, ValueError):
            page = 1

        data, error = await shop_api.manage_orders(status=status, query=query, page=page)
        if error:
            await flash(error, "danger")
        return await render_template(
            "orders_shop.html",
            orders=data.get("orders", []),
            statuses=data.get("statuses", []),
            status_titles=STATUS_TITLES,
            status_filter=status,
            query=query,
            total=data.get("total", 0),
            page=data.get("page", page),
            pages=data.get("pages", 1),
            orders_error=error,
        )

    @admin_bp_instance.route("/orders/export")
    async def orders_shop_export():
        """Выгрузка в CSV — ровно то, что сейчас на экране: фильтр и поиск те же.

        Файл собирает основное приложение, мы только передаём его браузеру.
        Разбирать и пересобирать его здесь значило бы поломать BOM, на который
        смотрит Excel, и переносы строк внутри адресов.
        """
        status = (request.args.get("status") or "").strip()
        query = (request.args.get("q") or "").strip()
        content, error = await shop_api.export_orders(status=status, query=query)
        if error:
            await flash(error, "danger")
            return redirect(url_for("admin.orders_shop", status=status, q=query))
        stamp = datetime.now().strftime("%Y-%m-%d")
        return content, 200, {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="orders-{stamp}.csv"',
        }

    @admin_bp_instance.route("/orders/<int:order_id>")
    async def order_shop_card(order_id: int):
        data, error = await shop_api.manage_order(order_id)
        if error:
            await flash(error, "danger")
            return redirect(url_for("admin.orders_shop"))
        return await render_template(
            "orders_shop_card.html",
            order=data.get("order", {}),
            delivery=data.get("delivery", {}),
            payments=data.get("payments", []),
            devices=data.get("devices", []),
            free_devices=data.get("free_devices", []),
            next_statuses=data.get("next_statuses", []),
            status_titles=STATUS_TITLES,
        )

    def _back(order_id: int):
        return redirect(url_for("admin.order_shop_card", order_id=order_id))

    @admin_bp_instance.route("/orders/<int:order_id>/status", methods=["POST"])
    async def order_shop_status(order_id: int):
        form = await request.form
        data, error = await shop_api.set_order_status(
            order_id, (form.get("status") or "").strip(), (form.get("reason") or "").strip()
        )
        if error:
            await flash(error, "danger")
            return _back(order_id)

        await flash("Статус обновлён.", "success")
        # Сообщение клиенту — отдельным шагом и необязательное: заказ уже
        # переведён, и молчащий Telegram не повод откатывать статус.
        if form.get("notify") == "on" and data.get("tg_id") and data.get("notice"):
            try:
                await send_telegram_message(int(data["tg_id"]), data["notice"])
                await flash("Клиенту отправлено уведомление.", "success")
            except Exception as exc:  # noqa: BLE001 — причину показываем оператору
                await flash(f"Статус сохранён, но уведомление не ушло: {exc}", "warning")
        return _back(order_id)

    @admin_bp_instance.route("/orders/<int:order_id>/shipping", methods=["POST"])
    async def order_shop_shipping(order_id: int):
        form = await request.form
        _, error = await shop_api.set_order_tracking(order_id, (form.get("tracking_number") or "").strip())
        await flash(error or "Трек-номер сохранён.", "danger" if error else "success")
        return _back(order_id)

    @admin_bp_instance.route("/orders/<int:order_id>/device", methods=["POST"])
    async def order_shop_device(order_id: int):
        form = await request.form
        _, error = await shop_api.attach_order_device(
            order_id, (form.get("mac") or "").strip(), (form.get("model") or "").strip()
        )
        await flash(error or "Роутер привязан к заказу.", "danger" if error else "success")
        return _back(order_id)

    @admin_bp_instance.route("/orders/<int:order_id>/note", methods=["POST"])
    async def order_shop_note(order_id: int):
        form = await request.form
        _, error = await shop_api.set_order_note(order_id, (form.get("note") or "").strip())
        await flash(error or "Заметка сохранена.", "danger" if error else "success")
        return _back(order_id)
