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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tg_sender import send_telegram_message

STATUS_TITLES = {
    "new": "Новый",
    "awaiting_payment": "Ждёт оплаты",
    "paid": "Оплачен",
    "packing": "Собираем",
    "shipped": "Отправлен",
    "delivered": "Доставлен",
    "activated": "Активирован",
    "done": "Завершён",
    "cancelled": "Отменён",
    "refunded": "Возврат",
}

DELIVERY_STATE_TITLES = {
    "not_quoted": "Доставка не посчитана",
    "awaiting_payment": "Ждёт оплату доставки",
    "paid": "Доставка оплачена",
}
"""Состояние доставки показывается рядом со статусом заказа, а не вместо него:
заказ к этому моменту «Оплачен» — роутер куплен, — и ждёт денег только
за перевозку. Словарь свой, как и у статусов: админка в другом процессе
и до `core/texts.py` не дотягивается."""

SPEED_TITLES = {
    "fast": "Быстрая",
    "weekly": "Раз в неделю",
}
"""Скорость выбирал клиент при заказе. Оператору она нужна на случай, когда
доставки у заказа нет вовсе — тогда он заводит её здесь целиком."""

CARRIER_TITLES = {
    "cdek": "СДЭК",
    "post": "Почта России",
    "yandex": "Яндекс Go",
}
"""Кем отправляем. Клиент выбирал скорость, перевозчика ставит оператор,
когда видит адрес и вес. Набор тот же, что в `OFFERED_DELIVERY_METHODS`
основного приложения — оно и проверяет присланное значение."""


def attach_orders_shop_routes(admin_bp_instance, query_db_func, execute_db_func):
    @admin_bp_instance.route("/orders")
    async def orders_shop():
        status = (request.args.get("status") or "").strip()
        query = (request.args.get("q") or "").strip()
        sort = (request.args.get("sort") or "").strip()
        direction = (request.args.get("dir") or "").strip()
        try:
            page = max(int(request.args.get("page", 1)), 1)
        except (TypeError, ValueError):
            page = 1

        data, error = await shop_api.manage_orders(
            status=status, query=query, page=page, sort=sort, direction=direction
        )
        if error:
            await flash(error, "danger")
        return await render_template(
            "orders_shop.html",
            orders=data.get("orders", []),
            statuses=data.get("statuses", []),
            status_titles=STATUS_TITLES,
            delivery_titles=DELIVERY_STATE_TITLES,
            delivery_filters=data.get("delivery_filters", []),
            status_filter=status,
            query=query,
            sort=data.get("sort", ""),
            sort_dir=data.get("dir", "desc"),
            total=data.get("total", 0),
            page=data.get("page", page),
            pages=data.get("pages", 1),
            orders_error=error,
        )

    @admin_bp_instance.route("/payments-shop")
    async def payments_shop():
        """Наши платежи: роутеры и доставка.

        У продукта есть свой раздел платежей, но там подписка для телефона —
        оплата железа идёт через нас и в его базу не попадает вовсе. Оператор
        искал платёж там и не находил.
        """
        status = (request.args.get("status") or "").strip()
        query = (request.args.get("q") or "").strip()
        try:
            page = max(int(request.args.get("page", 1)), 1)
        except (TypeError, ValueError):
            page = 1

        data, error = await shop_api.manage_payments(status=status, query=query, page=page)
        if error:
            await flash(error, "danger")
        return await render_template(
            "payments_shop.html",
            payments=data.get("payments", []),
            statuses=data.get("statuses", []),
            status_filter=status,
            query=query,
            total=data.get("total", 0),
            earned=data.get("earned", "0"),
            page=data.get("page", page),
            pages=data.get("pages", 1),
            payments_error=error,
        )

    @admin_bp_instance.route("/payments-shop/<int:payment_id>/cancel", methods=["POST"])
    async def payment_shop_cancel(payment_id: int):
        """Гасим висящий платёж. Проверку у провайдера делает основное
        приложение — оплаченный платёж оно отменить не даст."""
        _, error = await shop_api.cancel_payment(payment_id)
        await flash(error or "Платёж отменён.", "danger" if error else "success")
        return redirect(url_for("admin.payments_shop", **dict(request.args)))

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
            all_statuses=data.get("all_statuses", []),
            status_titles=STATUS_TITLES,
            delivery_titles=DELIVERY_STATE_TITLES,
            carriers=CARRIER_TITLES,
            speeds=SPEED_TITLES,
        )

    @admin_bp_instance.route("/orders/<int:order_id>/delete", methods=["POST"])
    async def order_shop_delete(order_id: int):
        """Удаление заказа. Оплаченные основное приложение не отдаёт — они
        уже история: платёж, чек и, скорее всего, уехавшее железо."""
        data, error = await shop_api.delete_order(order_id)
        if error:
            await flash(error, "danger")
            return redirect(url_for("admin.order_shop_card", order_id=order_id))
        await flash(f"Заказ {data.get('number') or order_id} удалён.", "success")
        return redirect(url_for("admin.orders_shop"))

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

    @admin_bp_instance.route("/orders/<int:order_id>/delivery-quote", methods=["POST"])
    async def order_shop_delivery_quote(order_id: int):
        """Оператор назвал цену доставки — клиенту уходит счёт.

        Ссылка на оплату идёт кнопкой в том же сообщении: адрес платёжной
        страницы длинный, и строкой в тексте её никто не нажмёт.
        """
        form = await request.form
        data, error = await shop_api.quote_delivery(
            order_id,
            (form.get("price") or "0").strip(),
            (form.get("days") or "").strip(),
            (form.get("method") or "").strip(),
            (form.get("speed") or "").strip(),
        )
        if error:
            await flash(error, "danger")
            return _back(order_id)

        await flash("Цена доставки сохранена.", "success")
        if data.get("tg_id") and data.get("notice"):
            markup = None
            if data.get("pay_url"):
                markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="💳 Оплатить доставку", url=data["pay_url"])]
                    ]
                )
            try:
                await send_telegram_message(int(data["tg_id"]), data["notice"], markup)
                await flash("Клиенту отправлен счёт на доставку.", "success")
            except Exception as exc:  # noqa: BLE001 — причину показываем оператору
                await flash(f"Цена сохранена, но сообщение не ушло: {exc}", "warning")
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
