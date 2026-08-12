"""Раздел «Каталог»: роутеры, которые бот продаёт.

Товары лежат в Postgres основного приложения — там же, где заказы, промокоды
и расчёт цены. Второй каталог в нашей SQLite развёл бы цены по двум местам,
поэтому здесь только форма: читаем и пишем по HTTP через `src/shop_api.py`.

В нашей базе живёт лишь то, что относится к боту: тумблер раздела и сколько
характеристик показывать в карточке. Тексты экранов правятся там же, где
остальные, — на странице текстов.
"""

import asyncio
import json

from quart import flash, redirect, render_template, request, url_for

from app_config import app_conf
from src import shop_api
from src.shop_texts import CATALOG_SETTINGS

SPECS_LIMIT_MAX = 30


def _form_int(form, name: str, default: int, *, low: int, high: int) -> int:
    try:
        return max(min(int(form.get(name, default)), high), low)
    except (TypeError, ValueError):
        return default


def _decimal_text(raw: str) -> str:
    """Запятая в цене — обычное дело для русской раскладки."""
    return (raw or "").replace(",", ".").strip()


def _specs_from_form(raw: str) -> tuple[str, str]:
    """Характеристики вводятся строками «Порты: 3 LAN» — так их пишут люди,
    а JSON в форме превращает опечатку в скобке в потерянную карточку.
    Возвращает JSON-строку и текст ошибки."""
    specs: dict[str, str] = {}
    for number, line in enumerate((raw or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            return "", f"Строка {number}: нужен формат «Название: значение»."
        name, value = line.split(":", 1)
        if not name.strip():
            return "", f"Строка {number}: пустое название характеристики."
        specs[name.strip()] = value.strip()
    return json.dumps(specs, ensure_ascii=False), ""


def specs_to_text(specs: dict) -> str:
    """Обратно в строки для формы."""
    return "\n".join(f"{name}: {value}" for name, value in (specs or {}).items())


def attach_catalog_shop_routes(admin_bp_instance, query_db_func, execute_db_func):
    @admin_bp_instance.route("/catalog")
    async def catalog_shop():
        products, error = await shop_api.products(include_hidden=True)
        if error:
            await flash(error, "danger")
        # Цены доставки живут там же, где заказы: их читает расчёт суммы.
        delivery, delivery_error = await shop_api.delivery_settings()
        # Сроки подписки: их выбирают вместе с роутером, поэтому правятся здесь же.
        periods, periods_error = await shop_api.plans(include_hidden=True)
        return await render_template(
            "catalog_shop.html",
            products=products,
            catalog_error=error,
            delivery=delivery.get("options", []),
            free_from=delivery.get("free_from", "0"),
            delivery_error=delivery_error,
            plans=periods,
            plans_error=periods_error,
            catalog_enabled=str(app_conf.get("catalog_enabled", "1")) == "1",
            specs_limit=app_conf.get("catalog_specs_limit", 8),
        )

    @admin_bp_instance.route("/catalog/plans/<int:plan_id>/save", methods=["POST"])
    async def catalog_plan_save(plan_id: int):
        """Срок подписки. `plan_id = 0` — создание нового.

        Сроки продаются вместе с роутером: от выбранного зависит и цена заказа,
        и то, на сколько включится подписка, когда роутер доедет до клиента.
        """
        form = await request.form
        payload = {
            "slug": (form.get("slug") or "").strip(),
            "title": (form.get("title") or "").strip(),
            "description": (form.get("description") or "").strip(),
            "months": _form_int(form, "months", 1, low=0, high=120),
            "extra_days": _form_int(form, "extra_days", 0, low=0, high=3650),
            "price": _decimal_text(form.get("price", "")),
            "old_price": _decimal_text(form.get("old_price", "")),
            "sort_order": _form_int(form, "sort_order", 100, low=0, high=10000),
            "is_active": form.get("is_active") == "on",
        }
        _, error = await shop_api.save_plan(plan_id, payload)
        await flash(error or "Срок сохранён.", "danger" if error else "success")
        return redirect(url_for("admin.catalog_shop"))

    @admin_bp_instance.route("/catalog/plans/<int:plan_id>/delete", methods=["POST"])
    async def catalog_plan_delete(plan_id: int):
        _, error = await shop_api.delete_plan(plan_id)
        await flash(error or "Срок удалён.", "danger" if error else "success")
        return redirect(url_for("admin.catalog_shop"))

    @admin_bp_instance.route("/catalog/delivery", methods=["POST"])
    async def catalog_delivery_save():
        form = await request.form
        options: dict[str, dict] = {}
        for method in form.getlist("method"):
            options[method] = {
                "title": (form.get(f"title_{method}") or "").strip(),
                "pvz": _decimal_text(form.get(f"pvz_{method}", "")),
                "courier": _decimal_text(form.get(f"courier_{method}", "")),
                "days": (form.get(f"days_{method}") or "").strip(),
                "enabled": form.get(f"enabled_{method}") == "on",
            }
        _, error = await shop_api.save_delivery_settings(
            {"options": options, "free_from": _decimal_text(form.get("free_from", ""))}
        )
        await flash(error or "Доставка сохранена.", "danger" if error else "success")
        return redirect(url_for("admin.catalog_shop"))

    @admin_bp_instance.route("/catalog/new")
    async def catalog_product_new():
        return await render_template(
            "catalog_shop_form.html", product={}, specs_text="", title="Новая модель"
        )

    @admin_bp_instance.route("/catalog/<int:product_id>")
    async def catalog_product_form(product_id: int):
        product, error = await shop_api.product(product_id)
        if error:
            await flash(error, "danger")
            return redirect(url_for("admin.catalog_shop"))
        return await render_template(
            "catalog_shop_form.html",
            product=product,
            specs_text=specs_to_text(product.get("specs")),
            title=product.get("title") or "Модель",
        )

    @admin_bp_instance.route("/catalog/<int:product_id>/save", methods=["POST"])
    async def catalog_product_save(product_id: int):
        """`product_id = 0` — создание новой карточки."""
        form = await request.form
        specs_json, specs_error = _specs_from_form(form.get("specs", ""))
        if specs_error:
            await flash(specs_error, "danger")
            return redirect(
                url_for("admin.catalog_product_form", product_id=product_id)
                if product_id
                else url_for("admin.catalog_product_new")
            )

        payload = {
            "slug": (form.get("slug") or "").strip(),
            "title": (form.get("title") or "").strip(),
            "subtitle": (form.get("subtitle") or "").strip(),
            "description": (form.get("description") or "").strip(),
            "model_code": (form.get("model_code") or "").strip(),
            "price": _decimal_text(form.get("price", "")),
            "old_price": _decimal_text(form.get("old_price", "")),
            "stock": _form_int(form, "stock", 0, low=0, high=100000),
            "sort_order": _form_int(form, "sort_order", 100, low=0, high=10000),
            "is_active": form.get("is_active") == "on",
            "allow_preorder": form.get("allow_preorder") == "on",
            "specs": specs_json,
        }

        data, error = await shop_api.save_product(product_id, payload)
        if error:
            await flash(error, "danger")
            return redirect(
                url_for("admin.catalog_product_form", product_id=product_id)
                if product_id
                else url_for("admin.catalog_product_new")
            )

        saved = data.get("product", {})
        # Картинка уходит отдельным запросом: файл нельзя положить в JSON,
        # а у новой карточки до сохранения ещё нет номера.
        files = await request.files
        upload = files.get("photo")
        if upload is not None and upload.filename:
            # FileStorage.read() синхронный — не держим им цикл событий.
            content = await asyncio.to_thread(upload.read)
            _, photo_error = await shop_api.upload_photo(
                saved.get("id"), upload.filename, content, upload.content_type or ""
            )
            if photo_error:
                await flash(f"Карточка сохранена, но картинка — нет: {photo_error}", "warning")
                return redirect(url_for("admin.catalog_product_form", product_id=saved.get("id")))

        await flash("Модель сохранена.", "success")
        return redirect(url_for("admin.catalog_shop"))

    @admin_bp_instance.route("/catalog/<int:product_id>/delete", methods=["POST"])
    async def catalog_product_delete(product_id: int):
        """Прошлые заказы не пострадают: в них лежит снимок названия и цены."""
        _, error = await shop_api.delete_product(product_id)
        await flash(error or "Модель удалена.", "danger" if error else "success")
        return redirect(url_for("admin.catalog_shop"))

    @admin_bp_instance.route("/catalog/settings", methods=["POST"])
    async def catalog_settings_save():
        form = await request.form
        values = {
            "catalog_enabled": "1" if form.get("catalog_enabled") == "on" else "0",
            "catalog_specs_limit": str(_form_int(form, "catalog_specs_limit", 8, low=1, high=SPECS_LIMIT_MAX)),
        }
        descriptions = {key: description for key, _, description in CATALOG_SETTINGS}

        for key, value in values.items():
            # Не UPDATE: на базе, созданной до появления каталога, строки нет,
            # и обновление молча не сделало бы ничего.
            await execute_db_func(
                "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value, descriptions.get(key, "")),
            )

        await app_conf.load_settings()
        from web_admin.routes.settings import reload_bot_settings

        await reload_bot_settings()
        await flash("Настройки каталога сохранены.", "success")
        return redirect(url_for("admin.catalog_shop"))
