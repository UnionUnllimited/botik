"""Маршруты партнёрской программы.

Партнёрские приглашения (`invited_by_method='partner'`) живут отдельно от
органической рефералки (см. /referrals). Здесь:
  - список партнёров с агрегатами (приглашено, успешных платежей, баланс)
  - детальная карточка партнёра: %, ссылка, корректировка баланса (= история
    действий админа), история начислений с фильтрами, sparkline динамики
  - CSV-экспорт начислений (общий и для конкретного партнёра)
"""
from datetime import datetime, timedelta, timezone
import csv
import io
import os
import time

import pytz
from quart import current_app, flash, jsonify, redirect, render_template, request, Response, url_for


def attach_partners_routes(admin_bp_instance, query_db_func, execute_db_func):
    # Lazy-индексы: создаются один раз за процесс на первом обращении.
    # Без них агрегаты и сортировка по приглашениям/платежам пилят полные
    # сканы, что больно при большом числе клиентов.
    _partners_idx_ensured = {'done': False}

    async def _ensure_partners_indexes():
        if _partners_idx_ensured['done']:
            return
        from web_admin.async_db import async_execute_db
        try:
            await async_execute_db(
                "CREATE INDEX IF NOT EXISTS idx_users_invited_by ON users(invited_by)"
            )
            await async_execute_db(
                "CREATE INDEX IF NOT EXISTS idx_users_invited_by_method "
                "ON users(invited_by, invited_by_method)"
            )
            # Покрывающий индекс под CTE-агрегаты: фильтр по методу + группировка по invited_by.
            # Без него SQLite сканирует всю users при WHERE invited_by_method = 'partner'.
            await async_execute_db(
                "CREATE INDEX IF NOT EXISTS idx_users_method_invited "
                "ON users(invited_by_method, invited_by)"
            )
            await async_execute_db(
                "CREATE INDEX IF NOT EXISTS idx_partner_accruals_partner "
                "ON partner_accruals(partner_id, created_at)"
            )
            await async_execute_db(
                "CREATE INDEX IF NOT EXISTS idx_payments_telegram_status "
                "ON payments(telegram_id, status)"
            )
        except Exception as e:
            current_app.logger.warning(f"[PARTNERS] не удалось создать индексы: {e}")
        finally:
            _partners_idx_ensured['done'] = True

    # ---------- helpers ----------

    def _msk_format(iso_str: str | None) -> str:
        """ISO → MSK 'дд.мм.гггг чч:мм'. На некорректных входах вернёт исходник."""
        if not iso_str:
            return ''
        try:
            dt = datetime.fromisoformat(str(iso_str).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=pytz.UTC)
            return dt.astimezone(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')
        except Exception:
            return str(iso_str)

    def _classify_accrual(row: dict) -> str:
        """Тип записи в partner_accruals по существующим полям (без миграции).

        - 'auto'   — автоначисление от платежа реферала (есть payment_id, payer_id > 0)
        - 'manual' — корректировка/выплата админом (payment_id == 'MANUAL_ADJUST')
        - 'other'  — всё остальное (на всякий случай)
        """
        pid = (row.get('payment_id') or '').strip()
        if pid == 'MANUAL_ADJUST':
            return 'manual'
        if pid and (row.get('payer_id') or 0) > 0:
            return 'auto'
        return 'other'

    # ---------- /partners — список ----------

    @admin_bp_instance.route('/partners', methods=['GET'])
    async def partners_list():
        """Список всех партнёров (у кого выдан partner_ref_code)."""
        from web_admin.async_db import async_query_db
        await _ensure_partners_indexes()
        t0 = time.perf_counter()

        try:
            page = max(1, int(request.args.get('page', 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            per_page = int(request.args.get('per_page', 30))
        except (TypeError, ValueError):
            per_page = 30
        if per_page not in (15, 30, 50, 100):
            per_page = 30

        sort = (request.args.get('sort') or 'bal').lower()
        if sort not in ('bal', 'ref', 'pay'):
            sort = 'bal'

        q = (request.args.get('q') or '').strip()

        # WHERE: только пользователи с partner_ref_code
        where_clauses = ["u.partner_ref_code IS NOT NULL", "u.partner_ref_code <> ''"]
        params: list = []

        if q:
            like = f"%{q}%"
            if q.isdigit() and len(q) <= 18:
                where_clauses.append(
                    "(CAST(u.telegram_id AS TEXT) LIKE ? "
                    " OR u.username LIKE ? "
                    " OR u.real_username LIKE ? "
                    " OR u.partner_ref_code LIKE ? "
                    " OR u.email LIKE ?)"
                )
                params.extend([like, like, like, like, like])
            else:
                where_clauses.append(
                    "(u.username LIKE ? "
                    " OR u.real_username LIKE ? "
                    " OR u.partner_ref_code LIKE ? "
                    " OR u.email LIKE ?)"
                )
                params.extend([like, like, like, like])

        where_sql = " AND ".join(where_clauses)

        order_by_sql = {
            'bal': 'balance DESC, u.telegram_id DESC',
            'ref': 'referrals_count DESC, u.telegram_id DESC',
            'pay': 'payments_count DESC, u.telegram_id DESC',
        }[sort]

        # Total
        total_row = await async_query_db(
            f"SELECT COUNT(*) AS c FROM users u WHERE {where_sql}",
            tuple(params), one=True,
        ) or {}
        total = int(total_row.get('c') or 0)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        offset = (page - 1) * per_page

        # Раньше тут были два коррелированных подзапроса (sub_ref, sub_pay) — их SQLite
        # выполнял ДЛЯ КАЖДОЙ строки результата, а при ORDER BY referrals/payments —
        # вообще для всех 600+ партнёров до пагинации. На больших базах страница
        # подвисала или таймаутила.
        #
        # Решение: один проход через CTE, который собирает агрегаты для всех партнёров
        # сразу, а потом LEFT JOIN-ом подмешивается к основному списку. Сложность —
        # O(N) вместо O(N×M).
        partners = await async_query_db(
            f"""
            WITH ref_stats AS (
                SELECT invited_by, COUNT(1) AS ref_count
                  FROM users
                 WHERE invited_by_method = 'partner'
                   AND COALESCE(is_blocked, 0) = 0
                 GROUP BY invited_by
            ),
            pay_stats AS (
                SELECT u2.invited_by, COUNT(1) AS pay_count
                  FROM users u2
                  JOIN payments p ON p.telegram_id = u2.telegram_id
                 WHERE u2.invited_by_method = 'partner'
                   AND p.status = 'succeeded'
                 GROUP BY u2.invited_by
            )
            SELECT u.telegram_id,
                   u.username,
                   u.real_username,
                   u.partner_ref_code,
                   COALESCE(u.partner_balance_rub, 0) AS balance,
                   COALESCE(u.partner_percent_rub, '') AS partner_percent_rub,
                   COALESCE(r.ref_count, 0) AS referrals_count,
                   COALESCE(p.pay_count, 0) AS payments_count
              FROM users u
              LEFT JOIN ref_stats r ON r.invited_by = u.telegram_id
              LEFT JOIN pay_stats p ON p.invited_by = u.telegram_id
             WHERE {where_sql}
             ORDER BY {order_by_sql}
             LIMIT ? OFFSET ?
            """,
            tuple(params) + (per_page, offset),
        ) or []

        # KPI: одним запросом по всей базе партнёров (без учёта поиска).
        # «Выплачено» = сумма отрицательных bonus в partner_accruals (manual + auto уменьшения).
        # «Начислено» = сумма положительных bonus за все время.
        # Один проход по users + один по partner_accruals (вместо 4 раздельных подзапросов).
        kpi = await async_query_db(
            """
            SELECT
                COALESCE(u_stats.total_partners, 0) AS total_partners,
                COALESCE(u_stats.total_balance, 0)  AS total_balance,
                COALESCE(p_stats.total_accrued, 0)  AS total_accrued,
                COALESCE(p_stats.total_payouts, 0)  AS total_payouts
              FROM (
                    SELECT COUNT(*) AS total_partners,
                           SUM(partner_balance_rub) AS total_balance
                      FROM users
                     WHERE partner_ref_code IS NOT NULL AND partner_ref_code <> ''
                   ) u_stats
              LEFT JOIN (
                    SELECT SUM(CASE WHEN bonus > 0 THEN bonus  ELSE 0 END) AS total_accrued,
                           SUM(CASE WHEN bonus < 0 THEN -bonus ELSE 0 END) AS total_payouts
                      FROM partner_accruals
                   ) p_stats ON 1=1
            """,
            (), one=True,
        ) or {}

        # Глобальный процент по умолчанию.
        global_pct_row = await async_query_db(
            "SELECT value FROM settings WHERE key = 'partner_percent_rub'",
            (), one=True,
        )
        try:
            global_pct = float((global_pct_row or {}).get('value') or 10)
        except (ValueError, TypeError):
            global_pct = 10.0

        elapsed_ms = (time.perf_counter() - t0) * 1000
        if elapsed_ms > 800:
            current_app.logger.warning(
                f"[PARTNERS] partners_list slow: {elapsed_ms:.0f}ms (total={total}, q={q!r})"
            )

        return await render_template(
            'partners_list.html',
            partners=partners,
            page=page,
            per_page=per_page,
            total=total,
            total_pages=total_pages,
            sort=sort,
            q=q,
            kpi=kpi,
            global_pct=global_pct,
        )

    # ---------- /partners/<tid> — детали ----------

    @admin_bp_instance.route('/partners/<int:telegram_id>', methods=['GET', 'POST'])
    async def partner_details(telegram_id):
        """Детальная страница партнёра с историей начислений и админских действий."""
        from web_admin.async_db import async_execute_db, async_query_db
        await _ensure_partners_indexes()

        # POST — корректировка баланса (admin action). История этих корректировок
        # пишется в partner_accruals с маркером payment_id='MANUAL_ADJUST' и используется
        # как «история действий админа с партнёром» (по факту: корректировки + выплаты).
        if request.method == 'POST':
            form = await request.form
            action = form.get('action')
            if action == 'adjust_balance':
                try:
                    amount_str = (form.get('amount') or '').strip().replace(',', '.')
                    if not amount_str:
                        await flash('Не указана сумма корректировки', 'danger')
                        return redirect(url_for('admin.partner_details', telegram_id=telegram_id))
                    try:
                        amount = float(amount_str)
                    except (ValueError, TypeError):
                        await flash(f'Неверный формат суммы «{amount_str}». Используйте число (например: 100.50 или -50)', 'danger')
                        return redirect(url_for('admin.partner_details', telegram_id=telegram_id))
                    if abs(amount) > 1_000_000:
                        await flash('Сумма корректировки слишком большая (максимум ±1 000 000 ₽)', 'danger')
                        return redirect(url_for('admin.partner_details', telegram_id=telegram_id))
                    if amount == 0:
                        await flash('Сумма равна нулю — изменения не применены', 'info')
                        return redirect(url_for('admin.partner_details', telegram_id=telegram_id))

                    comment = (form.get('comment') or '').strip()

                    await async_execute_db(
                        "UPDATE users SET partner_balance_rub = COALESCE(partner_balance_rub, 0) + ? "
                        "WHERE telegram_id = ?",
                        (amount, telegram_id),
                    )
                    try:
                        await async_execute_db(
                            """
                            INSERT INTO partner_accruals
                              (partner_id, payer_id, payment_id, amount, currency, percent, bonus, comment, created_at)
                            VALUES (?, 0, 'MANUAL_ADJUST', 0, 'RUB', 0, ?, ?, ?)
                            """,
                            (telegram_id, amount, comment, datetime.now(timezone.utc).isoformat()),
                        )
                    except Exception as e:
                        current_app.logger.error(
                            f"[PARTNERS] не удалось записать историю корректировки "
                            f"partner_id={telegram_id}: {e}"
                        )

                    sign = '+' if amount > 0 else ''
                    msg = f'Баланс изменён на {sign}{amount:.2f} ₽'
                    if comment:
                        msg += f' · {comment}'
                    await flash(msg, 'success')
                except Exception as e:
                    current_app.logger.error(
                        f"[PARTNERS] корректировка баланса partner_id={telegram_id}: {e}",
                        exc_info=True,
                    )
                    await flash(f'Ошибка корректировки: {e}', 'danger')
                return redirect(url_for('admin.partner_details', telegram_id=telegram_id))

        partner = await async_query_db(
            """
            SELECT telegram_id,
                   username,
                   real_username,
                   email,
                   partner_ref_code,
                   COALESCE(partner_balance_rub, 0) AS balance,
                   COALESCE(partner_percent_rub, '') AS partner_percent_rub
              FROM users
             WHERE telegram_id = ?
            """,
            (telegram_id,), one=True,
        )
        if not partner:
            await flash('Партнёр не найден', 'danger')
            return redirect(url_for('admin.partners_list'))

        # Приглашённые этим партнёром (через method='partner').
        try:
            ref_page = max(1, int(request.args.get('ref_page', 1)))
        except (TypeError, ValueError):
            ref_page = 1
        ref_per_page = 15
        ref_total_row = await async_query_db(
            "SELECT COUNT(*) AS c FROM users "
            "WHERE invited_by = ? AND invited_by_method = 'partner'",
            (telegram_id,), one=True,
        )
        referrals_total = int((ref_total_row or {}).get('c') or 0)
        ref_total_pages = max(1, (referrals_total + ref_per_page - 1) // ref_per_page)
        ref_page = min(ref_page, ref_total_pages)
        ref_offset = (ref_page - 1) * ref_per_page

        referrals_raw = await async_query_db(
            """
            SELECT telegram_id, username, real_username, created_at, subscription_end_date
              FROM users
             WHERE invited_by = ? AND invited_by_method = 'partner'
             ORDER BY datetime(created_at) DESC, telegram_id DESC
             LIMIT ? OFFSET ?
            """,
            (telegram_id, ref_per_page, ref_offset),
        ) or []
        now_utc = datetime.now(timezone.utc)
        referrals = []
        for r in referrals_raw:
            sub = r.get('subscription_end_date')
            sub_status = 'none'
            if sub:
                try:
                    end = datetime.fromisoformat(str(sub).replace('Z', '+00:00'))
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=timezone.utc)
                    sub_status = 'active' if end > now_utc else 'expired'
                except Exception:
                    pass
            referrals.append({
                'telegram_id': r.get('telegram_id'),
                'username': r.get('username'),
                'real_username': r.get('real_username'),
                'created_msk': _msk_format(r.get('created_at')),
                'sub_status': sub_status,
            })

        # Платежи рефералов (счётчик)
        payments_count_row = await async_query_db(
            """
            SELECT COUNT(*) AS c
              FROM payments p
              JOIN users u ON u.telegram_id = p.telegram_id
             WHERE u.invited_by = ? AND u.invited_by_method = 'partner'
               AND p.status = 'succeeded'
            """,
            (telegram_id,), one=True,
        )
        payments_count = int((payments_count_row or {}).get('c') or 0)

        # История начислений с фильтрами.
        try:
            acc_page = max(1, int(request.args.get('accruals_page', 1)))
        except (TypeError, ValueError):
            acc_page = 1
        acc_per_page = 20
        acc_offset = (acc_page - 1) * acc_per_page

        acc_type = (request.args.get('acc_type') or 'all').lower()
        if acc_type not in ('all', 'auto', 'manual'):
            acc_type = 'all'
        date_from = (request.args.get('date_from') or '').strip()
        date_to = (request.args.get('date_to') or '').strip()

        acc_where = ["partner_id = ?"]
        acc_params: list = [telegram_id]
        if acc_type == 'auto':
            acc_where.append("COALESCE(payment_id, '') <> 'MANUAL_ADJUST'")
            acc_where.append("COALESCE(payment_id, '') <> ''")
            acc_where.append("COALESCE(payer_id, 0) > 0")
        elif acc_type == 'manual':
            acc_where.append("COALESCE(payment_id, '') = 'MANUAL_ADJUST'")
        if date_from:
            acc_where.append("date(created_at) >= date(?)")
            acc_params.append(date_from)
        if date_to:
            acc_where.append("date(created_at) <= date(?)")
            acc_params.append(date_to)
        acc_where_sql = " AND ".join(acc_where)

        acc_total_row = await async_query_db(
            f"SELECT COUNT(*) AS c FROM partner_accruals WHERE {acc_where_sql}",
            tuple(acc_params), one=True,
        )
        acc_total = int((acc_total_row or {}).get('c') or 0)
        acc_total_pages = max(1, (acc_total + acc_per_page - 1) // acc_per_page)
        acc_page = min(acc_page, acc_total_pages)
        acc_offset = (acc_page - 1) * acc_per_page

        accruals_raw = await async_query_db(
            f"""
            SELECT id, created_at, payer_id, amount, currency, percent, bonus, payment_id, comment
              FROM partner_accruals
             WHERE {acc_where_sql}
             ORDER BY datetime(created_at) DESC
             LIMIT ? OFFSET ?
            """,
            tuple(acc_params) + (acc_per_page, acc_offset),
        ) or []
        accruals = []
        for a in accruals_raw:
            accruals.append({
                **dict(a),
                'created_msk': _msk_format(a.get('created_at')),
                'kind': _classify_accrual(a),
            })

        # Sparkline: суммарный bonus по дням за последние 30 дней (UTC).
        spark_rows = await async_query_db(
            """
            SELECT date(created_at) AS d, SUM(bonus) AS s
              FROM partner_accruals
             WHERE partner_id = ?
               AND date(created_at) >= date('now', '-29 days')
             GROUP BY date(created_at)
             ORDER BY d ASC
            """,
            (telegram_id,),
        ) or []
        spark_map = {r['d']: float(r['s'] or 0) for r in spark_rows}
        chart_days = []
        today = datetime.now(timezone.utc).date()
        for i in range(29, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            chart_days.append({'date': d, 'value': spark_map.get(d, 0.0)})

        # Агрегаты по деньгам для карточки партнёра (как в partners/export.csv).
        fin_row = await async_query_db(
            """
            SELECT
                COALESCE(-SUM(CASE WHEN bonus < 0 THEN bonus END), 0) AS total_paid_out_rub,
                COALESCE(SUM(CASE
                    WHEN COALESCE(payment_id, '') = 'MANUAL_ADJUST' AND bonus > 0
                    THEN bonus END), 0) AS manual_credit_rub
              FROM partner_accruals
             WHERE partner_id = ?
            """,
            (telegram_id,),
            one=True,
        ) or {}
        partner_finance = {
            'total_paid_out_rub': float(fin_row.get('total_paid_out_rub') or 0),
            'manual_credit_rub': float(fin_row.get('manual_credit_rub') or 0),
        }

        # Ссылка партнёра.
        bot_settings = await async_query_db(
            "SELECT key, value FROM settings WHERE key IN ('bot_username', 'partner_percent_rub', 'website_url')",
            (),
        ) or []
        settings_dict = {r['key']: r['value'] for r in bot_settings}
        bot_username = (settings_dict.get('bot_username') or '').strip()
        try:
            global_pct = float(settings_dict.get('partner_percent_rub') or 10)
        except (ValueError, TypeError):
            global_pct = 10.0
        partner_link = (
            f"https://t.me/{bot_username}?start=par_{partner['partner_ref_code']}"
            if bot_username and partner.get('partner_ref_code') else None
        )
        # Как в «Продвижении»: SITE_URL (.env) > settings.website_url; формат как website/run.py
        site_base = (os.getenv('SITE_URL') or '').strip().rstrip('/')
        if not site_base:
            site_base = (settings_dict.get('website_url') or '').strip().rstrip('/')
        ref_code = (partner.get('partner_ref_code') or '').strip()
        partner_site_link = (
            f"{site_base}/?ref=par_{ref_code}" if site_base and ref_code else None
        )

        return await render_template(
            'partner_details.html',
            partner=partner,
            partner_finance=partner_finance,
            partner_link=partner_link,
            partner_site_link=partner_site_link,
            global_pct=global_pct,
            referrals=referrals,
            referrals_page=ref_page,
            referrals_total=referrals_total,
            referrals_total_pages=ref_total_pages,
            payments_count=payments_count,
            accruals=accruals,
            accruals_page=acc_page,
            accruals_total=acc_total,
            accruals_total_pages=acc_total_pages,
            acc_type=acc_type,
            date_from=date_from,
            date_to=date_to,
            chart_days=chart_days,
        )

    # ---------- CSV-экспорт ----------

    @admin_bp_instance.route('/partners/export.csv')
    async def partners_export_csv():
        """CSV-дамп всех партнёров с агрегатами. Только для админа."""
        from web_admin.async_db import async_query_db
        from web_admin.run import current_user
        if not current_user.is_admin:
            return '', 403
        await _ensure_partners_indexes()

        # CSV-экспорт раньше делал по 4 коррелированных подзапроса на каждого партнёра.
        # На 600+ строках это давало десятки тысяч обращений к users/payments/partner_accruals.
        # Через CTE — один проход по каждой таблице.
        rows = await async_query_db(
            """
            WITH ref_stats AS (
                SELECT invited_by, COUNT(1) AS ref_count
                  FROM users
                 WHERE invited_by_method = 'partner'
                   AND COALESCE(is_blocked, 0) = 0
                 GROUP BY invited_by
            ),
            pay_stats AS (
                SELECT u2.invited_by, COUNT(1) AS pay_count
                  FROM users u2
                  JOIN payments p ON p.telegram_id = u2.telegram_id
                 WHERE u2.invited_by_method = 'partner'
                   AND p.status = 'succeeded'
                 GROUP BY u2.invited_by
            ),
            acc_stats AS (
                SELECT partner_id,
                       SUM(CASE WHEN bonus > 0 THEN bonus  ELSE 0 END) AS total_accrued,
                       SUM(CASE WHEN bonus < 0 THEN -bonus ELSE 0 END) AS total_payouts
                  FROM partner_accruals
                 GROUP BY partner_id
            )
            SELECT u.telegram_id,
                   u.username,
                   u.real_username,
                   u.email,
                   u.partner_ref_code,
                   COALESCE(u.partner_balance_rub, 0) AS balance,
                   COALESCE(u.partner_percent_rub, '') AS partner_percent_rub,
                   COALESCE(r.ref_count, 0)      AS referrals_count,
                   COALESCE(p.pay_count, 0)      AS payments_count,
                   COALESCE(a.total_accrued, 0)  AS total_accrued,
                   COALESCE(a.total_payouts, 0)  AS total_payouts
              FROM users u
              LEFT JOIN ref_stats r ON r.invited_by = u.telegram_id
              LEFT JOIN pay_stats p ON p.invited_by = u.telegram_id
              LEFT JOIN acc_stats a ON a.partner_id = u.telegram_id
             WHERE u.partner_ref_code IS NOT NULL AND u.partner_ref_code <> ''
             ORDER BY balance DESC, u.telegram_id DESC
            """
        ) or []

        buf = io.StringIO()
        buf.write('\ufeff')
        w = csv.writer(buf, delimiter=';', lineterminator='\n')
        w.writerow([
            'telegram_id', 'username', 'real_username', 'email', 'partner_ref_code',
            'balance_rub', 'partner_percent', 'referrals_count', 'payments_count',
            'total_accrued', 'total_payouts',
        ])
        for r in rows:
            w.writerow([
                r.get('telegram_id') or '',
                r.get('username') or '',
                r.get('real_username') or '',
                r.get('email') or '',
                r.get('partner_ref_code') or '',
                f"{float(r.get('balance') or 0):.2f}",
                r.get('partner_percent_rub') or '',
                r.get('referrals_count') or 0,
                r.get('payments_count') or 0,
                f"{float(r.get('total_accrued') or 0):.2f}",
                f"{float(r.get('total_payouts') or 0):.2f}",
            ])
        ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')
        return Response(
            buf.getvalue().encode('utf-8'),
            mimetype='text/csv; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="partners_{ts}.csv"'},
        )

    @admin_bp_instance.route('/partners/<int:telegram_id>/export.csv')
    async def partner_accruals_export_csv(telegram_id: int):
        """CSV-дамп истории начислений конкретного партнёра. Только для админа."""
        from web_admin.async_db import async_query_db
        from web_admin.run import current_user
        if not current_user.is_admin:
            return '', 403
        rows = await async_query_db(
            """
            SELECT id, created_at, payer_id, amount, currency, percent, bonus, payment_id, comment
              FROM partner_accruals
             WHERE partner_id = ?
             ORDER BY datetime(created_at) DESC
            """,
            (telegram_id,),
        ) or []

        buf = io.StringIO()
        buf.write('\ufeff')
        w = csv.writer(buf, delimiter=';', lineterminator='\n')
        w.writerow([
            'id', 'created_at_utc', 'kind', 'payer_id', 'payment_id',
            'amount', 'currency', 'percent', 'bonus_rub', 'comment',
        ])
        for r in rows:
            w.writerow([
                r.get('id') or '',
                r.get('created_at') or '',
                _classify_accrual(r),
                r.get('payer_id') or 0,
                r.get('payment_id') or '',
                f"{float(r.get('amount') or 0):.2f}",
                r.get('currency') or '',
                r.get('percent') or 0,
                f"{float(r.get('bonus') or 0):.2f}",
                r.get('comment') or '',
            ])
        ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')
        return Response(
            buf.getvalue().encode('utf-8'),
            mimetype='text/csv; charset=utf-8',
            headers={
                'Content-Disposition': (
                    f'attachment; filename="partner_{telegram_id}_accruals_{ts}.csv"'
                )
            },
        )
