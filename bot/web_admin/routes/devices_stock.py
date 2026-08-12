"""Раздел «Склад»: устройства до отгрузки.

Те же роутеры, что во вкладке «Роутеры», но взгляд другой: там показания
и туннели, здесь MAC, серийник, состояние и заметка кладовщика. Коробок бывают
сотни, а на связи из них единицы, поэтому список с поиском и постранично.

Данные — в Postgres основного приложения, ходим по HTTP: своя база у нас
не про роутеры, а до чужой отсюда не дотянуться.

Команды устройству не перенесены намеренно: их забирает heartbeat, а API
устройств ещё не написан — очередь копилась бы, и никто бы её не разобрал.
"""

from quart import flash, redirect, render_template, request, url_for

from src import shop_api

STATUS_TITLES = {
    "new": "На складе",
    "assigned": "Отгружено",
    "active": "Работает",
    "revoked": "Отвязано",
    "blocked": "Заблокировано",
}


def attach_devices_stock_routes(admin_bp_instance, query_db_func, execute_db_func):
    @admin_bp_instance.route("/stock")
    async def devices_stock():
        query = (request.args.get("q") or "").strip()
        try:
            page = max(int(request.args.get("page", 1)), 1)
        except (TypeError, ValueError):
            page = 1

        show_all = request.args.get("all") == "1"
        data, error = await shop_api.stock(query=query, page=page, show_all=show_all)
        if error:
            await flash(error, "danger")
        return await render_template(
            "devices_stock.html",
            devices=data.get("devices", []),
            statuses=data.get("statuses", []),
            status_titles=STATUS_TITLES,
            total=data.get("total", 0),
            page=data.get("page", page),
            pages=data.get("pages", 1),
            query=query,
            show_all=show_all,
            stock_error=error,
        )

    def _back(query: str, page: int):
        return redirect(url_for("admin.devices_stock", q=query or None, page=page if page > 1 else None))

    def _paging(form) -> tuple[str, int]:
        """Поиск и страница возвращаются как были: иначе после каждой правки
        кладовщик оказывался бы в начале списка."""
        try:
            page = max(int(form.get("page", 1)), 1)
        except (TypeError, ValueError):
            page = 1
        return (form.get("q") or "").strip(), page

    @admin_bp_instance.route("/stock/new", methods=["POST"])
    async def devices_stock_add():
        form = await request.form
        query, page = _paging(form)
        _, error = await shop_api.stock_add(
            (form.get("mac") or "").strip(),
            (form.get("model") or "").strip(),
            (form.get("serial") or "").strip(),
        )
        await flash(error or "Устройство заведено.", "danger" if error else "success")
        return _back(query, page)

    @admin_bp_instance.route("/stock/<int:device_id>/status", methods=["POST"])
    async def devices_stock_status(device_id: int):
        form = await request.form
        query, page = _paging(form)
        _, error = await shop_api.set_device_status(device_id, (form.get("status") or "").strip())
        await flash(error or "Состояние изменено.", "danger" if error else "success")
        return _back(query, page)

    @admin_bp_instance.route("/stock/<int:device_id>/note", methods=["POST"])
    async def devices_stock_note(device_id: int):
        form = await request.form
        query, page = _paging(form)
        _, error = await shop_api.set_device_note(device_id, (form.get("note") or "").strip())
        await flash(error or "Заметка сохранена.", "danger" if error else "success")
        return _back(query, page)
