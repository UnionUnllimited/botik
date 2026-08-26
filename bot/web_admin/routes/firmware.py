"""Раздел «Обновление роутеров»: манифест прошивки и образы к нему.

Роутеры обновляются сами. Раз в сутки каждый берёт по постоянному адресу один
JSON, сравнивает номер версии со своим и, если он выше, качает образ своей
модели и проверяет sha256. Ни ручек, ни отчёта обратно: панель не знает
и не может знать, сколько роутеров обновилось.

Всё, что тут делает оператор, исполняет основное приложение — образы лежат
в его томе, манифест отдаётся с его домена. Здесь только экран.

Сам файл через эту службу не идёт: браузер отправляет его прямо туда
по разовой ссылке, которую мы просим по общему токену. Образ весит 27–54 МБ,
и перегон через нас означал бы его же в памяти и второй таймаут по дороге.
"""

from quart import flash, jsonify, redirect, render_template, request, url_for

from src import shop_api


def attach_firmware_routes(admin_bp_instance):
    @admin_bp_instance.route("/firmware")
    async def firmware_page():
        data, error = await shop_api.firmware_state()
        if error:
            await flash(error, "danger")
        return await render_template(
            "firmware_updates.html",
            firmware_error=error,
            models=data.get("models") or [],
            rollout_steps=data.get("rollout_steps") or [0, 100],
            rollout_warning=data.get("rollout_warning") or "",
            manifest_url=data.get("manifest_url") or "",
            image_suffix=data.get("image_suffix") or "-sysupgrade.bin",
            max_mb=data.get("max_mb") or 0,
            next_version=data.get("next_version") or 1,
            current=data.get("current"),
            draft=data.get("draft"),
            releases=data.get("releases") or [],
        )

    @admin_bp_instance.route("/firmware/releases", methods=["POST"])
    async def firmware_release_create():
        """Заводит черновик. Номер проверяет основное приложение: разъедься
        проверки, форма пропустила бы то, что база потом не примет."""
        from web_admin.run import current_user

        form = await request.form
        _, error = await shop_api.firmware_create_release(
            (form.get("version") or "").strip(),
            (form.get("notes") or "").strip(),
            getattr(current_user, "username", "") or "",
        )
        await flash(
            error or "Черновик создан — загрузите образы.", "danger" if error else "success"
        )
        return redirect(url_for("admin.firmware_page"))

    @admin_bp_instance.route("/firmware/ticket", methods=["POST"])
    async def firmware_upload_ticket():
        """Разовая ссылка для отправки одного образа. Зовётся скриптом страницы
        перед каждой загрузкой — билет одноразовый, и одного на страницу
        не хватило бы на четыре модели."""
        payload = await request.get_json(silent=True) or {}
        try:
            release_id = int(payload.get("release_id") or 0)
        except (TypeError, ValueError):
            release_id = 0
        if not release_id:
            return jsonify({"ok": False, "error": "Не указан выпуск."}), 400

        data, error = await shop_api.firmware_upload_ticket(
            release_id, str(payload.get("model") or "")
        )
        if error:
            return jsonify({"ok": False, "error": error}), 502
        return jsonify({"ok": True, "url": data.get("url", "")})

    @admin_bp_instance.route("/firmware/releases/<int:release_id>/rollout", methods=["POST"])
    async def firmware_rollout(release_id: int):
        """Доля парка. Применяется сразу: манифест собирается из базы."""
        form = await request.form
        data, error = await shop_api.firmware_set_rollout(
            release_id, (form.get("rollout") or "0").strip()
        )
        if error:
            await flash(error, "danger")
        elif int(data.get("rollout", 0)) == 0:
            await flash(
                "Раздача остановлена: новые роутеры обновление не получат. "
                "Уже обновившиеся остаются на новой версии — роутер ставит "
                "только версии выше своей.",
                "warning",
            )
        else:
            await flash(f"Раскатка: {data.get('rollout', 0)} % парка.", "success")
        return redirect(url_for("admin.firmware_page"))

    @admin_bp_instance.route("/firmware/releases/<int:release_id>/publish", methods=["POST"])
    async def firmware_publish(release_id: int):
        form = await request.form
        data, error = await shop_api.firmware_publish(
            release_id, (form.get("rollout") or "0").strip()
        )
        if error:
            await flash(error, "danger")
        else:
            release = data.get("release") or {}
            await flash(
                f"Выпуск {release.get('version', '')} опубликован, "
                f"раскатка {release.get('rollout', 0)} % парка.",
                "success",
            )
        return redirect(url_for("admin.firmware_page"))

    @admin_bp_instance.route("/firmware/releases/<int:release_id>/image-delete", methods=["POST"])
    async def firmware_image_delete(release_id: int):
        """Убирает модель из выпуска: роутеры этой модели ничего делать не будут."""
        form = await request.form
        _, error = await shop_api.firmware_delete_image(
            release_id, (form.get("model") or "").strip()
        )
        await flash(
            error or "Модель убрана из выпуска — её роутеры обновляться не будут.",
            "danger" if error else "success",
        )
        return redirect(url_for("admin.firmware_page"))

    @admin_bp_instance.route("/firmware/releases/<int:release_id>/delete", methods=["POST"])
    async def firmware_release_delete(release_id: int):
        _, error = await shop_api.firmware_delete_release(release_id)
        await flash(error or "Выпуск удалён вместе с образами.", "danger" if error else "success")
        return redirect(url_for("admin.firmware_page"))
