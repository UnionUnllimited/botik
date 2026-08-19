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
from src import shop_api, shop_sync
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
    @admin_bp_instance.after_request
    async def _push_tariffs_after_change(response):
        """Правка тарифа уезжает в каталог сразу.

        Ловим здесь, а не в десяти обработчиках тарифов: их добавление,
        правка, удаление, массовые операции и переключение — всё это POST
        в один раздел, и перечислять их поимённо значит забыть один при
        следующем обновлении их кода.

        Отправку не ждём: сохранение тарифа не должно тормозить из-за похода
        по сети, а не доехавшее подберёт круг синхронизации.
        """
        if request.method == "POST" and "/tariffs" in request.path:
            asyncio.create_task(shop_sync.sync_once())
        return response

    @admin_bp_instance.route("/catalog")
    async def catalog_shop():
        """Только модели. Доставка и настройки — отдельными страницами:
        одним полотном это не читалось, а правки разного рода мешались."""
        products, error = await shop_api.products(include_hidden=True)
        if error:
            await flash(error, "danger")
        return await render_template("catalog_shop.html", products=products, catalog_error=error)

    @admin_bp_instance.route("/catalog/delivery")
    async def catalog_delivery():
        delivery, error = await shop_api.delivery_settings()
        if error:
            await flash(error, "danger")
        return await render_template(
            "catalog_delivery.html",
            delivery=delivery.get("options", []),
            free_from=delivery.get("free_from", "0"),
            delivery_error=error,
        )

    @admin_bp_instance.route("/catalog/settings")
    async def catalog_settings():
        return await render_template(
            "catalog_settings.html",
            catalog_enabled=str(app_conf.get("catalog_enabled", "1")) == "1",
            specs_limit=app_conf.get("catalog_specs_limit", 8),
        )

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
        return redirect(url_for("admin.catalog_delivery"))

    @admin_bp_instance.route("/catalog/delivery/zones")
    async def catalog_delivery_zones():
        """Цены по тарифным зонам: до Тольятти и до Владивостока везут по-разному.

        Отдельной страницей от общих настроек: там переключатели перевозчиков,
        здесь длинные списки городов, и вместе они не читались.
        """
        data, error = await shop_api.delivery_zones()
        if error:
            await flash(error, "danger")
        return await render_template(
            "catalog_delivery_zones.html",
            zones=data.get("zones", []),
            methods=data.get("methods", []),
            unknown=data.get("unknown", []),
            zones_error=error,
        )

    @admin_bp_instance.route("/catalog/delivery/zones", methods=["POST"])
    async def catalog_delivery_zones_save():
        form = await request.form
        methods = form.getlist("method")
        zones = {
            zone_id: {
                "cities": form.get(f"cities_{zone_id}", ""),
                "days": (form.get(f"days_{zone_id}") or "").strip(),
                "prices": {
                    method: {
                        "pvz": _decimal_text(form.get(f"pvz_{zone_id}_{method}", "")),
                        "courier": _decimal_text(form.get(f"courier_{zone_id}_{method}", "")),
                    }
                    for method in methods
                },
            }
            for zone_id in form.getlist("zone")
        }
        _, error = await shop_api.save_delivery_zones(zones)
        await flash(error or "Зоны сохранены.", "danger" if error else "success")
        return redirect(url_for("admin.catalog_delivery_zones"))

    @admin_bp_instance.route("/catalog/delivery/cities/<int:city_id>", methods=["POST"])
    async def catalog_delivery_city(city_id: int):
        """Разбор неопознанного города: в зону или из списка вон."""
        form = await request.form
        zone_id = _form_int(form, "zone_id", 0, low=0, high=10**9)
        data, error = await shop_api.resolve_unknown_city(city_id, zone_id)
        if error:
            await flash(error, "danger")
        elif not zone_id:
            await flash("Город убран из списка.", "success")
        elif data.get("added"):
            await flash(f"Город добавлен в зону «{data.get('zone', '')}».", "success")
        else:
            await flash("Город уже был в этой зоне.", "success")
        return redirect(url_for("admin.catalog_delivery_zones"))

    @admin_bp_instance.route("/catalog/promos")
    async def catalog_promos():
        """Промокоды на железо. У бота свои, на подписку, — это разные скидки."""
        items, error = await shop_api.promos()
        if error:
            await flash(error, "danger")
        return await render_template("catalog_promos.html", promos=items, promo_error=error)

    @admin_bp_instance.route("/catalog/promos", methods=["POST"])
    async def catalog_promo_create():
        form = await request.form
        _, error = await shop_api.promo_create(
            {
                "code": form.get("code", ""),
                "description": form.get("description", ""),
                "discount_type": form.get("discount_type", "percent"),
                "value": form.get("value", "0"),
                "min_amount": form.get("min_amount", "0"),
                "max_uses": form.get("max_uses", "0"),
                "per_user_limit": form.get("per_user_limit", "1"),
                "valid_until": form.get("valid_until", ""),
                "new_clients_only": form.get("new_clients_only") == "on",
            }
        )
        await flash(error or "Промокод заведён.", "danger" if error else "success")
        return redirect(url_for("admin.catalog_promos"))

    @admin_bp_instance.route("/catalog/promos/<int:promo_id>/<action>", methods=["POST"])
    async def catalog_promo_action(promo_id: int, action: str):
        if action not in ("toggle", "delete"):
            return redirect(url_for("admin.catalog_promos"))
        _, error = await shop_api.promo_action(promo_id, action)
        if error:
            await flash(error, "danger")
        return redirect(url_for("admin.catalog_promos"))

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
        return redirect(url_for("admin.catalog_settings"))
