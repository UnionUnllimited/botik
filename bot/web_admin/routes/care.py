"""Просьбы клиентов о сайтах и отзывы о роутерах.

Клиент жмёт в боте или в приложении «Не открывается сайт», мы проверяем,
и если сайта в списках нет — просьба приходит в топик «Роутеры» с кнопкой
«Добавить». Здесь те же просьбы списком: на случай, если сообщение в чате
пролистали, и чтобы видеть, что уже решено и кем.

Отзывы — оценки через неделю после включения. Низкие приходят в топик
сразу; здесь все, с текстом и средней.
"""

from markupsafe import escape
from quart import flash, redirect, render_template, request, url_for

from web_admin.routes.routers_fleet import _get as fleet_get
from web_admin.routes.routers_fleet import _post as fleet_post

ACTIONS = {
    "add": "Добавили {site} в свой список — клиенту написали.",
    "no": "Отметили: {site} не добавляем. Клиенту написали.",
    "done": "Отметили: с {site} разобрались.",
}


def attach_care_routes(admin_bp_instance):
    @admin_bp_instance.route("/care")
    async def care_page():
        tab = "reviews" if request.args.get("tab") == "reviews" else "sites"
        status = (request.args.get("status") or "").strip()
        status = status if status in ("new", "added", "declined", "done") else ""
        query = f"?status={status}" if status else ""
        sites, sites_error = await fleet_get(f"/api/v1/fleet/site-requests{query}")
        reviews, reviews_error = await fleet_get("/api/v1/fleet/reviews")
        return await render_template(
            "care.html",
            tab=tab,
            status_filter=status,
            requests=sites.get("items") or [],
            counts=sites.get("counts") or {},
            reviews=reviews.get("items") or [],
            summary=reviews.get("summary") or {},
            care_error=sites_error or reviews_error,
        )

    @admin_bp_instance.route("/care/site/<int:request_id>/<action>", methods=["POST"])
    async def care_site_resolve(request_id: int, action: str):
        if action not in ACTIONS:
            return redirect(url_for("admin.care_page"))
        from web_admin.run import current_user

        form = await request.form
        author = current_user.id or "админка"
        data, error = await fleet_post(
            f"/api/v1/fleet/site-requests/{request_id}/resolve",
            {"action": action, "author": f"админка ({author})"},
        )
        if error or not data.get("ok"):
            await flash(error or data.get("error") or "Не получилось.", "danger")
        else:
            # Флеши рисуются с `|safe`, а имя сайта пришло из формы — экранируем.
            await flash(ACTIONS[action].format(site=escape(form.get("site") or "сайт")), "success")
        return redirect(url_for("admin.care_page", status=request.args.get("status") or None))
