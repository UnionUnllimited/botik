import json
from quart import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from loguru import logger

# Все методы оплаты, для которых в БД хранятся строки tariffs.
# Третий элемент — ключ show_payment_* в settings (если есть).
TARIFF_PAYMENT_METHODS = [
    ('yookassa',  'YooKassa',       'show_payment_yookassa',  '1'),
    ('tgstar',    'Telegram Stars', 'show_payment_tgstar',    '1'),
    ('cryptobot', 'CryptoBot',      'show_payment_cryptobot', '1'),
    ('yoomoney',  'YooMoney',       'show_payment_yoomoney',  '1'),
    ('platega',   'Platega (СБП)',  'show_payment_platega',   '0'),
    ('wata',      'Wata',           'show_payment_wata',      '0'),
]
TARIFF_REFERENCE_METHOD = 'yookassa'
ALL_TARIFF_METHOD_SLUGS = [slug for slug, *_ in TARIFF_PAYMENT_METHODS]
_TRUTHY = frozenset({'1', 'true', 'yes', 'on'})


async def _build_tariff_dashboard(query_db_func) -> dict:
    """Сводка линеек по сроку (days) и статус синхронизации между всеми методами."""
    all_methods = ALL_TARIFF_METHOD_SLUGS

    rows = await query_db_func(
        "SELECT * FROM tariffs ORDER BY days, limit_ip, sort_order, id"
    ) or []
    tariffs = [dict(r) for r in rows]

    method_counts = {slug: 0 for slug, *_ in TARIFF_PAYMENT_METHODS}
    for t in tariffs:
        pm = (t.get('payment_method') or TARIFF_REFERENCE_METHOD).strip()
        method_counts[pm] = method_counts.get(pm, 0) + 1

    # days -> limit_ip -> method -> row
    by_line: dict[int, dict[int, dict[str, dict]]] = {}
    for t in tariffs:
        days = int(t.get('days') or 0)
        limit_ip = int(t.get('limit_ip') or 0)
        pm = (t.get('payment_method') or TARIFF_REFERENCE_METHOD).strip()
        if days <= 0:
            continue
        by_line.setdefault(days, {}).setdefault(limit_ip, {})[pm] = t

    lines = []
    for days in sorted(by_line.keys()):
        slots_raw = by_line[days]
        ref_slots = slots_raw
        if TARIFF_REFERENCE_METHOD in {pm for slot in slots_raw.values() for pm in slot}:
            ref_limit_ips = sorted(
                lip for lip, per_m in slots_raw.items() if TARIFF_REFERENCE_METHOD in per_m
            )
        else:
            ref_limit_ips = sorted(slots_raw.keys())

        slots = []
        methods_with_line = set()
        price_mismatch_methods = set()
        ref_prices = []

        for limit_ip in ref_limit_ips:
            per_method = slots_raw.get(limit_ip, {})
            ref_row = per_method.get(TARIFF_REFERENCE_METHOD) or next(iter(per_method.values()), None)
            if not ref_row:
                continue
            ref_price = float(ref_row.get('price') or 0)
            ref_prices.append(ref_price)
            methods_with_line.update(per_method.keys())
            for pm, row in per_method.items():
                if float(row.get('price') or 0) != ref_price:
                    price_mismatch_methods.add(pm)

            slots.append({
                'limit_ip': limit_ip,
                'price': ref_price,
                'traffic_gb': int(ref_row.get('traffic_gb') or 0),
                'name': ref_row.get('name') or '',
                'is_active': bool(ref_row.get('is_active')),
                'method_count': len(per_method),
            })

        methods_missing = [m for m in all_methods if m not in methods_with_line]
        all_synced = (
            not methods_missing
            and not price_mismatch_methods
            and len(methods_with_line) == len(all_methods)
        )

        lines.append({
            'days': days,
            'slot_count': len(slots),
            'slots': slots,
            'price_min': min(ref_prices) if ref_prices else 0,
            'price_max': max(ref_prices) if ref_prices else 0,
            'methods_count': len(methods_with_line),
            'all_methods_count': len(all_methods),
            'methods_missing': methods_missing,
            'price_mismatch_methods': sorted(price_mismatch_methods),
            'all_synced': all_synced,
            'total_rows': sum(len(per_m) for per_m in slots_raw.values()),
        })

    empty_methods = [
        {'slug': slug, 'label': label, 'ref_count': method_counts.get(TARIFF_REFERENCE_METHOD, 0)}
        for slug, label, _k, _d in TARIFF_PAYMENT_METHODS
        if method_counts.get(slug, 0) == 0
    ]

    return {
        'lines': lines,
        'method_counts': method_counts,
        'all_methods_count': len(all_methods),
        'empty_methods': empty_methods,
        'total_tariffs': len(tariffs),
        'reference_method': TARIFF_REFERENCE_METHOD,
        'method_labels': {slug: label for slug, label, *_ in TARIFF_PAYMENT_METHODS},
    }


def attach_tariff_routes(admin_bp_instance, query_db_func, execute_db_func):

    async def _save_tariff_rows_for_methods(rows: list, payment_methods: list) -> dict:
        """Сохранить строки линейки во все выбранные методы оплаты."""
        updated = inserted = deleted = 0
        days = 0
        for src in rows:
            if int(src.get('days') or 0) > 0:
                days = int(src['days'])
                break

        for pm in payment_methods:
            for src in rows:
                try:
                    rid = src.get('id')
                    delete_flag = bool(src.get('_delete'))
                    name = str(src.get('name') or '').strip()
                    row_days = int(src.get('days') or days or 0)
                    price = float(src.get('price') or 0)
                    description = str(src.get('desc') or '').strip()
                    sort_order = int(src.get('sort') or 0)
                    limit_ip = int(src.get('devices') or 0)
                    traffic_gb = int(src.get('gb') or 0)
                    is_active = 1 if src.get('active', True) else 0

                    existing = None
                    if row_days > 0 and limit_ip > 0:
                        existing = await query_db_func(
                            "SELECT id FROM tariffs WHERE payment_method = ? AND days = ? AND limit_ip = ?",
                            (pm, row_days, limit_ip),
                            one=True,
                        )
                    # Фолбэк по id для собственного метода строки (легаси-тарифы
                    # без device-count, limit_ip=0, иначе не находились и не сохранялись).
                    if not existing and rid and str(src.get('payment_method') or '') == pm:
                        existing = await query_db_func(
                            "SELECT id FROM tariffs WHERE id = ?", (int(rid),), one=True,
                        )

                    if delete_flag:
                        if rid and str(src.get('payment_method') or pm) == pm:
                            await execute_db_func("DELETE FROM tariffs WHERE id = ?", (int(rid),))
                            deleted += 1
                        elif existing:
                            await execute_db_func(
                                "DELETE FROM tariffs WHERE payment_method = ? AND days = ? AND limit_ip = ?",
                                (pm, row_days, limit_ip),
                            )
                            deleted += 1
                        continue

                    if not name or row_days <= 0 or price <= 0:
                        continue
                    # 0 устройств допускаем только при обновлении найденной строки,
                    # чтобы не плодить пустые тарифы во всех методах.
                    if limit_ip <= 0 and not existing:
                        continue

                    if existing:
                        eid = int(dict(existing)['id'])
                        await execute_db_func('''
                            UPDATE tariffs
                            SET name = ?, days = ?, price = ?, currency = 'RUB',
                                description = ?, sort_order = ?, is_active = ?,
                                limit_ip = ?, traffic_gb = ?, payment_method = ?
                            WHERE id = ?
                        ''', (name, row_days, price, description, sort_order, is_active,
                              limit_ip, traffic_gb, pm, eid))
                        updated += 1
                    else:
                        await execute_db_func('''
                            INSERT INTO tariffs (name, days, price, currency, description,
                                                 sort_order, is_active, limit_ip, traffic_gb,
                                                 payment_method)
                            VALUES (?, ?, ?, 'RUB', ?, ?, ?, ?, ?, ?)
                        ''', (name, row_days, price, description, sort_order, is_active,
                              limit_ip, traffic_gb, pm))
                        inserted += 1
                except Exception as e:
                    logger.error(
                        f"[TARIFFS] save row failed pm={pm} days={src.get('days')} "
                        f"limit_ip={src.get('devices')}: {type(e).__name__}: {e}"
                    )
                    continue
        logger.info(
            f"[TARIFFS] save line: updated={updated} inserted={inserted} deleted={deleted} "
            f"methods={len(payment_methods)} rows={len(rows)}"
        )
        return {'updated': updated, 'inserted': inserted, 'deleted': deleted}

    @admin_bp_instance.route('/tariffs')
    async def tariffs_list():
        method = (request.args.get('method') or '').strip()
        view = (request.args.get('view') or '').strip()

        if method:
            tariffs = await query_db_func(
                "SELECT * FROM tariffs WHERE payment_method = ? ORDER BY sort_order, id",
                (method,),
            )
            return await render_template('tariffs_list.html', tariffs=tariffs, current_method=method)

        if view == 'methods':
            rows = await query_db_func(
                "SELECT payment_method, COUNT(*) AS cnt FROM tariffs GROUP BY payment_method"
            ) or []
            counts = {
                (r['payment_method'] or 'yookassa'): (r['cnt'] if 'cnt' in r.keys() else r[1])
                for r in rows
            }
            total_row = await query_db_func("SELECT COUNT(*) AS c FROM tariffs", (), one=True)
            total_tariffs = int((dict(total_row).get('c') if total_row else 0) or 0)
            enabled_row = await query_db_func(
                "SELECT value FROM settings WHERE key = 'device_upgrade_enabled'", (), one=True,
            )
            device_upgrade_enabled = bool(
                enabled_row and str(dict(enabled_row).get('value', '0')).strip() in _TRUTHY
            )
            return await render_template(
                'tariffs_methods.html',
                counts=counts,
                total_tariffs=total_tariffs,
                device_upgrade_enabled=device_upgrade_enabled,
            )

        dashboard = await _build_tariff_dashboard(query_db_func)
        enabled_row = await query_db_func(
            "SELECT value FROM settings WHERE key = 'device_upgrade_enabled'", (), one=True,
        )
        device_upgrade_enabled = bool(
            enabled_row and str(dict(enabled_row).get('value', '0')).strip() in _TRUTHY
        )
        return await render_template(
            'tariffs_dashboard.html',
            device_upgrade_enabled=device_upgrade_enabled,
            tariff_methods=TARIFF_PAYMENT_METHODS,
            **dashboard,
        )

    # ────────────────────────────────────────────────────────────────────
    # Настройки фичи "Расширение лимита устройств" (платный апгрейд)
    # ────────────────────────────────────────────────────────────────────
    @admin_bp_instance.route('/tariffs/add', methods=['GET', 'POST'])
    async def tariff_add():
        default_method = (request.args.get('payment_method') or '').strip()
        if request.method == 'POST':
            form = await request.form
            name = (form.get('name') or '').strip()
            days = int(form.get('days') or 0)
            price = float(form.get('price') or 0)
            description = (form.get('description') or '').strip()
            sort_order = int(form.get('sort_order') or 0)
            limit_ip = int(form.get('limit_ip') or 0)
            traffic_gb = int(form.get('traffic_gb') or 0)
            payment_method = form.get('payment_method') or (default_method or 'yookassa')

            if not name or days <= 0 or price <= 0:
                await flash('Пожалуйста, заполните все обязательные поля корректно.', 'danger')
                return await render_template('tariff_form.html', tariff={}, title="Добавить тариф")

            # Валюта в UI убрана — все тарифы хранятся как RUB. Для CryptoBot/TG Star
            # бот сам конвертит из рублей при создании инвойса.
            await execute_db_func('''
                INSERT INTO tariffs (name, days, price, currency, description, sort_order, is_active, limit_ip, traffic_gb, payment_method)
                VALUES (?, ?, ?, 'RUB', ?, ?, 1, ?, ?, ?)
            ''', (name, days, price, description, sort_order, limit_ip, traffic_gb, payment_method))

            await flash(f'Тариф "{name}" успешно добавлен!', 'success')
            # Возвращаемся на список текущего метода, если он выбран
            if payment_method:
                return redirect(url_for('admin.tariffs_list', method=payment_method))
            return redirect(url_for('admin.tariffs_list'))
        # Передаём дефолтный метод в форму (для предзаполнения селекта/поля)
        return await render_template('tariff_form.html', tariff={'payment_method': default_method} if default_method else {}, title="Добавить тариф")

    @admin_bp_instance.route('/tariffs/edit/<int:tariff_id>', methods=['GET', 'POST'])
    async def tariff_edit(tariff_id):
        tariff = await query_db_func("SELECT * FROM tariffs WHERE id = ?", (tariff_id,), one=True)
        if not tariff:
            await flash('Тариф не найден.', 'danger')
            return redirect(url_for('admin.tariffs_list'))

        if request.method == 'POST':
            form = await request.form
            name = (form.get('name') or '').strip()
            days = int(form.get('days') or 0)
            price = float(form.get('price') or 0)
            description = (form.get('description') or '').strip()
            sort_order = int(form.get('sort_order') or 0)
            is_active = bool(form.get('is_active'))
            limit_ip = int(form.get('limit_ip') or 0)
            traffic_gb = int(form.get('traffic_gb') or 0)
            payment_method = form.get('payment_method') or 'yookassa'

            if not name or days <= 0 or price <= 0:
                await flash('Пожалуйста, заполните все обязательные поля корректно.', 'danger')
                tariff = dict(tariff)
                return await render_template('tariff_form.html', tariff=tariff, title="Редактировать тариф")

            # Валюта всегда RUB — мигрируем при сохранении.
            await execute_db_func('''
                UPDATE tariffs 
                SET name = ?, days = ?, price = ?, currency = 'RUB', description = ?, 
                    sort_order = ?, is_active = ?, limit_ip = ?, traffic_gb = ?, payment_method = ?
                WHERE id = ?
            ''', (name, days, price, description, sort_order, int(is_active), limit_ip, traffic_gb, payment_method, tariff_id))

            await flash(f'Тариф "{name}" успешно обновлен!', 'success')
            # Если редактировали в контексте метода — вернёмся туда
            return redirect(url_for('admin.tariffs_list', method=payment_method))

        tariff = dict(tariff)
        return await render_template('tariff_form.html', tariff=tariff, title="Редактировать тариф")

    @admin_bp_instance.route('/tariffs/delete/<int:tariff_id>', methods=['POST'])
    async def tariff_delete(tariff_id):
        tariff = await query_db_func("SELECT * FROM tariffs WHERE id = ?", (tariff_id,), one=True)
        if not tariff:
            await flash('Тариф не найден.', 'danger')
            return redirect(url_for('admin.tariffs_list'))

        await execute_db_func("DELETE FROM tariffs WHERE id = ?", (tariff_id,))
        tariff = dict(tariff)
        await flash(f'Тариф "{tariff["name"]}" успешно удален!', 'success')
        return redirect(url_for('admin.tariffs_list', method=tariff.get('payment_method', '')))

    @admin_bp_instance.route('/tariffs/delete_line', methods=['POST'])
    async def tariff_line_delete():
        """Удалить линейку: все тарифы с данным days (и опционально одним payment_method)."""
        form = await request.form
        payment_method = (form.get('payment_method') or '').strip()
        all_methods = form.get('all_methods') in ('1', 'on', 'true')
        try:
            days = int(form.get('days') or 0)
        except (TypeError, ValueError):
            days = 0

        if days <= 0:
            await flash('Укажите срок линейки (дни).', 'danger')
            return redirect(url_for('admin.tariffs_list'))

        if all_methods or not payment_method:
            before = await query_db_func(
                "SELECT COUNT(*) AS c FROM tariffs WHERE days = ?",
                (days,),
                one=True,
            )
            cnt = int((dict(before).get('c') if before else 0) or 0)
            if cnt == 0:
                await flash('Такой линейки не найдено — нечего удалять.', 'warning')
                return redirect(url_for('admin.tariffs_list'))
            await execute_db_func("DELETE FROM tariffs WHERE days = ?", (days,))
            await flash(f'Линейка «{days} дн.» удалена во всех методах ({cnt} тарифов).', 'success')
            return redirect(url_for('admin.tariffs_list'))

        before = await query_db_func(
            "SELECT COUNT(*) AS c FROM tariffs WHERE payment_method = ? AND days = ?",
            (payment_method, days),
            one=True,
        )
        cnt = int((dict(before).get('c') if before else 0) or 0)
        if cnt == 0:
            await flash('Такой линейки не найдено — нечего удалять.', 'warning')
            return redirect(url_for('admin.tariffs_list', method=payment_method))

        await execute_db_func(
            "DELETE FROM tariffs WHERE payment_method = ? AND days = ?",
            (payment_method, days),
        )
        await flash(
            f'Линейка «{days} дн.» удалена для {payment_method} ({cnt} тарифов).',
            'success',
        )
        return redirect(url_for('admin.tariffs_list', method=payment_method))

    @admin_bp_instance.route('/tariffs/sync_method', methods=['POST'])
    async def tariff_sync_method():
        """Скопировать все тарифы продления из эталонного метода в целевой."""
        form = await request.form
        target = (form.get('target_method') or '').strip()
        source = (form.get('source_method') or TARIFF_REFERENCE_METHOD).strip()
        valid_slugs = {slug for slug, *_ in TARIFF_PAYMENT_METHODS}

        if target not in valid_slugs or source not in valid_slugs:
            await flash('Некорректный метод оплаты для синхронизации.', 'danger')
            return redirect(url_for('admin.tariffs_list'))
        if target == source:
            await flash('Источник и цель синхронизации совпадают.', 'warning')
            return redirect(url_for('admin.tariffs_list'))

        src_rows = await query_db_func(
            "SELECT * FROM tariffs WHERE payment_method = ? ORDER BY days, sort_order, id",
            (source,),
        ) or []
        if not src_rows:
            await flash(f'У метода {source} нет тарифов — нечего копировать.', 'warning')
            return redirect(url_for('admin.tariffs_list'))

        inserted = updated = 0
        for raw in src_rows:
            row = dict(raw)
            days = int(row.get('days') or 0)
            limit_ip = int(row.get('limit_ip') or 0)
            if days <= 0:
                continue
            existing = await query_db_func(
                "SELECT id FROM tariffs WHERE payment_method = ? AND days = ? AND limit_ip = ?",
                (target, days, limit_ip),
                one=True,
            )
            fields = (
                row.get('name') or '',
                days,
                float(row.get('price') or 0),
                row.get('description') or '',
                int(row.get('sort_order') or 0),
                int(row.get('is_active') or 1),
                limit_ip,
                int(row.get('traffic_gb') or 0),
                target,
            )
            if existing:
                await execute_db_func('''
                    UPDATE tariffs
                    SET name = ?, days = ?, price = ?, currency = 'RUB',
                        description = ?, sort_order = ?, is_active = ?,
                        limit_ip = ?, traffic_gb = ?, payment_method = ?
                    WHERE id = ?
                ''', (*fields, int(dict(existing)['id'])))
                updated += 1
            else:
                await execute_db_func('''
                    INSERT INTO tariffs (name, days, price, currency, description,
                                         sort_order, is_active, limit_ip, traffic_gb,
                                         payment_method)
                    VALUES (?, ?, ?, 'RUB', ?, ?, ?, ?, ?, ?)
                ''', fields)
                inserted += 1

        label_map = {slug: label for slug, label, *_ in TARIFF_PAYMENT_METHODS}
        await flash(
            f'Тарифы скопированы: {label_map.get(source, source)} → {label_map.get(target, target)} '
            f'(добавлено {inserted}, обновлено {updated}).',
            'success',
        )
        return redirect(url_for('admin.tariffs_list'))

    @admin_bp_instance.route('/tariffs/delete_all', methods=['POST'])
    async def tariff_delete_all():
        """Удалить все тарифы продления из таблицы tariffs."""
        from web_admin.run import current_user
        if not current_user.is_admin:
            await flash('Удаление всех тарифов доступно только администратору.', 'danger')
            return redirect(url_for('admin.tariffs_list'))

        before = await query_db_func("SELECT COUNT(*) AS c FROM tariffs", (), one=True)
        cnt = int((dict(before).get('c') if before else 0) or 0)
        if cnt == 0:
            await flash('Тарифов продления нет — нечего удалять.', 'warning')
            return redirect(url_for('admin.tariffs_list'))

        await execute_db_func("DELETE FROM tariffs")
        await flash(f'Удалены все тарифы продления ({cnt} шт.).', 'success')
        return redirect(url_for('admin.tariffs_list'))

    @admin_bp_instance.route('/tariffs/toggle/<int:tariff_id>', methods=['POST'])
    async def tariff_toggle(tariff_id):
        tariff = await query_db_func("SELECT * FROM tariffs WHERE id = ?", (tariff_id,), one=True)
        if not tariff:
            await flash('Тариф не найден.', 'danger')
            return redirect(url_for('admin.tariffs_list'))

        tariff = dict(tariff)
        new_status = not tariff['is_active']
        await execute_db_func("UPDATE tariffs SET is_active = ? WHERE id = ?", (int(new_status), tariff_id))

        status_text = "активирован" if new_status else "деактивирован"
        await flash(f'Тариф "{tariff["name"]}" {status_text}!', 'success')
        return redirect(url_for('admin.tariffs_list', method=tariff.get('payment_method', '')))

    @admin_bp_instance.route('/tariffs/bulk_create', methods=['GET', 'POST'])
    async def tariff_bulk_create():
        if request.method == 'POST':
            form = await request.form
            tariffs_json = (form.get('tariffs_json') or '').strip()

            try:
                tariff_rows = json.loads(tariffs_json)
            except Exception:
                await flash('Ошибка: не удалось разобрать данные тарифов.', 'danger')
                return redirect(url_for('admin.tariff_bulk_create'))

            if not tariff_rows:
                await flash('Нет тарифов для создания.', 'warning')
                return redirect(url_for('admin.tariff_bulk_create'))

            stats = await _save_tariff_rows_for_methods(tariff_rows, ALL_TARIFF_METHOD_SLUGS)
            created = stats['inserted'] + stats['updated']
            if created or stats['deleted']:
                parts = []
                if stats['inserted']: parts.append(f'добавлено: {stats["inserted"]}')
                if stats['updated']: parts.append(f'обновлено: {stats["updated"]}')
                if stats['deleted']: parts.append(f'удалено: {stats["deleted"]}')
                await flash(
                    f'Линейка сохранена ({", ".join(parts)}) во все {len(ALL_TARIFF_METHOD_SLUGS)} методов.',
                    'success',
                )
                return redirect(url_for('admin.tariffs_list'))
            await flash('Ни один тариф не был создан. Проверьте заполненность полей.', 'danger')
            return redirect(url_for('admin.tariff_bulk_create'))

        default_method = (request.args.get('method') or '').strip()
        try:
            preset_days = int(request.args.get('days') or 0)
        except (TypeError, ValueError):
            preset_days = 0
        return await render_template(
            'tariff_bulk_create.html',
            mode='create',
            existing_tariffs=[],
            default_method=default_method or TARIFF_REFERENCE_METHOD,
            preset_days=preset_days if preset_days > 0 else 0,
            all_methods_count=len(ALL_TARIFF_METHOD_SLUGS),
        )

    @admin_bp_instance.route('/tariffs/bulk_edit', methods=['GET', 'POST'])
    async def tariff_bulk_edit():
        """Массовое редактирование линейки тарифов (один срок, все методы оплаты)."""
        method = (request.args.get('method') or '').strip()

        if request.method == 'POST':
            form = await request.form
            tariffs_json = (form.get('tariffs_json') or '').strip()

            try:
                rows = json.loads(tariffs_json)
            except Exception:
                await flash('Ошибка: не удалось разобрать данные тарифов.', 'danger')
                return redirect(url_for('admin.tariffs_list'))

            stats = await _save_tariff_rows_for_methods(rows, ALL_TARIFF_METHOD_SLUGS)
            parts = []
            if stats['updated']: parts.append(f'обновлено: {stats["updated"]}')
            if stats['inserted']: parts.append(f'добавлено: {stats["inserted"]}')
            if stats['deleted']: parts.append(f'удалено: {stats["deleted"]}')
            await flash(
                'Линейка сохранена — ' + (', '.join(parts) if parts else 'без изменений')
                + f' (все {len(ALL_TARIFF_METHOD_SLUGS)} методов).',
                'success' if parts else 'info',
            )
            return redirect(url_for('admin.tariffs_list'))

        try:
            edit_days = int(request.args.get('days') or 0)
        except (TypeError, ValueError):
            edit_days = 0

        if edit_days <= 0:
            await flash('Укажите срок линейки (параметр days).', 'warning')
            return redirect(url_for('admin.tariffs_list'))

        ref_method = method or TARIFF_REFERENCE_METHOD
        existing = await query_db_func(
            "SELECT * FROM tariffs WHERE payment_method = ? AND days = ? ORDER BY sort_order, id",
            (ref_method, edit_days),
        ) or []
        if not existing:
            # Дашборд показывает линейку по ЛЮБОМУ payment_method, поэтому
            # грузим эталон из любого метода, где есть строки на этот срок
            # (включая методы вне известного списка слугов).
            distinct = await query_db_func(
                "SELECT DISTINCT payment_method FROM tariffs WHERE days = ?",
                (edit_days,),
            ) or []
            slug_order = ALL_TARIFF_METHOD_SLUGS + [
                (dict(r).get('payment_method') or '').strip()
                for r in distinct
                if (dict(r).get('payment_method') or '').strip() not in ALL_TARIFF_METHOD_SLUGS
            ]
            for slug in slug_order:
                if not slug:
                    continue
                alt = await query_db_func(
                    "SELECT * FROM tariffs WHERE payment_method = ? AND days = ? ORDER BY sort_order, id",
                    (slug, edit_days),
                ) or []
                if alt:
                    ref_method = slug
                    existing = alt
                    break
            if not existing:
                logger.warning(f"[TARIFFS] bulk_edit: нет тарифов для days={edit_days} ни в одном методе")

        existing = [dict(r) for r in existing]

        # Нормализуем типы для шаблона/JSON
        for row in existing:
            row['is_active'] = 1 if row.get('is_active') in (1, True, '1', 'true') else 0

        methods_present = set()
        methods_missing = []
        price_mismatch = []
        all_for_days = await query_db_func(
            "SELECT payment_method, limit_ip, price FROM tariffs WHERE days = ?",
            (edit_days,),
        ) or []
        ref_prices = {
            int(dict(r)['limit_ip']): float(dict(r)['price'])
            for r in existing
        }
        for raw in all_for_days:
            row = dict(raw)
            pm = row.get('payment_method') or ''
            methods_present.add(pm)
            lip = int(row.get('limit_ip') or 0)
            if lip in ref_prices and float(row.get('price') or 0) != ref_prices[lip]:
                price_mismatch.append(pm)
        price_mismatch = sorted(set(price_mismatch))
        for slug, *_ in TARIFF_PAYMENT_METHODS:
            if slug not in methods_present:
                methods_missing.append(slug)

        return await render_template(
            'tariff_bulk_create.html',
            mode='edit',
            existing_tariffs=existing,
            default_method=ref_method,
            preset_days=edit_days,
            methods_missing=methods_missing,
            price_mismatch_methods=price_mismatch,
            all_methods_count=len(ALL_TARIFF_METHOD_SLUGS),
        )

    # Роуты для тарифов докупки трафика
    @admin_bp_instance.route('/traffic_topup_tariffs')
    async def traffic_topup_tariffs_list():
        from web_admin.async_db import async_query_db
        tariffs = await async_query_db("SELECT * FROM traffic_topup_tariffs ORDER BY price ASC, traffic_gb ASC")
        return await render_template('traffic_topup_tariffs_list.html', tariffs=tariffs)

    @admin_bp_instance.route('/traffic_topup_tariffs/add', methods=['GET', 'POST'])
    async def traffic_topup_tariff_add():
        from web_admin.async_db import async_query_db, async_execute_db
        if request.method == 'POST':
            form = await request.form
            name = (form.get('name') or '').strip()
            traffic_gb = int(form.get('traffic_gb') or 0)
            price = float(form.get('price') or 0)
            description = (form.get('description') or '').strip()
            sort_order = int(form.get('sort_order') or 0)

            if not name or traffic_gb <= 0 or price <= 0:
                await flash('Пожалуйста, заполните все обязательные поля корректно.', 'danger')
                return await render_template('traffic_topup_tariff_form.html', tariff={}, title="Добавить тариф докупки")

            await async_execute_db('''
                INSERT INTO traffic_topup_tariffs (name, traffic_gb, price, description, sort_order, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
            ''', (name, traffic_gb, price, description, sort_order))

            await flash(f'Тариф докупки "{name}" успешно добавлен!', 'success')
            return redirect(url_for('admin.traffic_topup_tariffs_list'))
        
        return await render_template('traffic_topup_tariff_form.html', tariff={}, title="Добавить тариф докупки")

    @admin_bp_instance.route('/traffic_topup_tariffs/edit/<int:tariff_id>', methods=['GET', 'POST'])
    async def traffic_topup_tariff_edit(tariff_id):
        from web_admin.async_db import async_query_db, async_execute_db
        tariff = await async_query_db("SELECT * FROM traffic_topup_tariffs WHERE id = ?", (tariff_id,), one=True)
        if not tariff:
            await flash('Тариф не найден.', 'danger')
            return redirect(url_for('admin.traffic_topup_tariffs_list'))

        if request.method == 'POST':
            form = await request.form
            name = (form.get('name') or '').strip()
            traffic_gb = int(form.get('traffic_gb') or 0)
            price = float(form.get('price') or 0)
            description = (form.get('description') or '').strip()
            sort_order = int(form.get('sort_order') or 0)
            is_active = bool(form.get('is_active'))

            if not name or traffic_gb <= 0 or price <= 0:
                await flash('Пожалуйста, заполните все обязательные поля корректно.', 'danger')
                tariff = dict(tariff)
                return await render_template('traffic_topup_tariff_form.html', tariff=tariff, title="Редактировать тариф докупки")

            await async_execute_db('''
                UPDATE traffic_topup_tariffs 
                SET name = ?, traffic_gb = ?, price = ?, description = ?, 
                    sort_order = ?, is_active = ?
                WHERE id = ?
            ''', (name, traffic_gb, price, description, sort_order, int(is_active), tariff_id))

            await flash(f'Тариф докупки "{name}" успешно обновлен!', 'success')
            return redirect(url_for('admin.traffic_topup_tariffs_list'))

        tariff = dict(tariff)
        return await render_template('traffic_topup_tariff_form.html', tariff=tariff, title="Редактировать тариф докупки")

    @admin_bp_instance.route('/traffic_topup_tariffs/delete/<int:tariff_id>', methods=['POST'])
    async def traffic_topup_tariff_delete(tariff_id):
        from web_admin.async_db import async_query_db, async_execute_db
        tariff = await async_query_db("SELECT * FROM traffic_topup_tariffs WHERE id = ?", (tariff_id,), one=True)
        if not tariff:
            await flash('Тариф не найден.', 'danger')
            return redirect(url_for('admin.traffic_topup_tariffs_list'))

        tariff = dict(tariff)
        await async_execute_db("DELETE FROM traffic_topup_tariffs WHERE id = ?", (tariff_id,))
        await flash(f'Тариф докупки "{tariff["name"]}" успешно удален!', 'success')
        return redirect(url_for('admin.traffic_topup_tariffs_list'))

    @admin_bp_instance.route('/traffic_topup_tariffs/toggle/<int:tariff_id>', methods=['POST'])
    async def traffic_topup_tariff_toggle(tariff_id):
        from web_admin.async_db import async_query_db, async_execute_db
        tariff = await async_query_db("SELECT * FROM traffic_topup_tariffs WHERE id = ?", (tariff_id,), one=True)
        if not tariff:
            await flash('Тариф не найден.', 'danger')
            return redirect(url_for('admin.traffic_topup_tariffs_list'))

        tariff = dict(tariff)
        new_status = not tariff['is_active']
        await async_execute_db("UPDATE traffic_topup_tariffs SET is_active = ? WHERE id = ?", (int(new_status), tariff_id))

        status_text = "активирован" if new_status else "деактивирован"
        await flash(f'Тариф докупки "{tariff["name"]}" {status_text}!', 'success')
        return redirect(url_for('admin.traffic_topup_tariffs_list'))
