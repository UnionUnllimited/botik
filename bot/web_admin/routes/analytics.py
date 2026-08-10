"""
/analytics — комплексный раздел аналитики для админки.

Содержит:
  • KPI-плашки (юзеры, активные подписки, выручка, средний чек, конверсия, churn)
  • Графики регистраций и выручки по дням (Chart.js)
  • Распределения: методы оплаты, статусы подписок, источники регистрации
  • Топ тарифов / топ клиентов
  • Воронка: registered → first paid → repeat
  • Реферальные метрики

Один SSR-роут /analytics + один JSON-эндпоинт /analytics/data?period=7d|30d|90d|all|custom.
Все цифры — только успешные платежи в RUB (как в /payments).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from quart import render_template, request, jsonify, session, g

import db_helpers


def _can_view_analytics() -> bool:
    """Аналитику видит админ всегда; модератор — если ему выдан раздел 'analytics'."""
    if session.get('admin_role') == 'admin':
        return True
    secs = getattr(g, 'moderator_visible_sections', None)
    return bool(secs) and 'analytics' in secs


# ── Период — единая утилита ───────────────────────────────────────────────────
_PERIODS = {
    '7d':   ("datetime('now', '-7 days')",   7,   'day'),
    '30d':  ("datetime('now', '-30 days')",  30,  'day'),
    '90d':  ("datetime('now', '-90 days')",  90,  'day'),
    '365d': ("datetime('now', '-365 days')", 365, 'month'),
    'all':  (None,                            None, 'month'),
}


def _resolve_period(period: str) -> tuple[str | None, int | None, str]:
    """Возвращает (sql_lower_bound, days_count, group_by_kind)."""
    return _PERIODS.get(period, _PERIODS['30d'])


def _date_filter_clause(col: str, since_sql: str | None, until_sql: str | None = None) -> str:
    """Безопасный сборщик WHERE-фрагмента по диапазону `col`."""
    parts: list[str] = []
    if since_sql:
        parts.append(f"{col} >= {since_sql}")
    if until_sql:
        parts.append(f"{col} < {until_sql}")
    return " AND ".join(parts) if parts else "1=1"


def _payment_method_from_id(pid: str) -> str | None:
    if not pid:
        return None
    pid_l = pid.lower()
    if pid.startswith('WATA_'):                      return 'Wata'
    if pid.startswith('PLATEGA_'):                 return 'Platega'
    if pid.startswith('YOOMONEY_'):                return 'YooMoney'
    if pid.startswith('YK_') or 'yookassa' in pid_l: return 'YooKassa'
    if pid.startswith('CRYPTOBOT_') or pid.startswith('CRYPTO_'): return 'CryptoBot'
    if pid.startswith('TGSTAR_'):                  return 'TG Stars'
    if pid.startswith('USDT_'):                    return 'USDT'
    return None


def _normalize_method_name(name: str) -> str:
    name_l = (name or '').lower().strip()
    if not name_l: return 'Неизвестно'
    if 'wata' in name_l: return 'Wata'
    if 'platega' in name_l or 'sbp' in name_l: return 'Platega'
    if 'yookassa' in name_l or name_l == 'yk':  return 'YooKassa'
    if 'yoomoney' in name_l:                    return 'YooMoney'
    if 'cryptobot' in name_l or 'crypto' in name_l: return 'CryptoBot'
    if 'tgstar' in name_l or 'tg star' in name_l or 'stars' in name_l: return 'TG Stars'
    if 'usdt' in name_l: return 'USDT'
    return name


def attach_analytics_routes(admin_bp):
    from web_admin.async_db import async_query_db

    # ── Главная страница ──────────────────────────────────────────────────────
    async def analytics_view():
        if not _can_view_analytics():
            return await render_template('analytics.html', forbidden=True)
        return await render_template('analytics.html', forbidden=False)

    # ── Данные для страницы (JSON) ───────────────────────────────────────────
    async def analytics_data():
        if not _can_view_analytics():
            return jsonify({'ok': False, 'error': 'forbidden'}), 403

        period = request.args.get('period', '30d')
        since_sql, days, group_kind = _resolve_period(period)

        # Где мы сейчас по таблицам
        try:
            user_columns = [c['name'] for c in (await async_query_db("PRAGMA table_info(users)", ()))]
        except Exception:
            user_columns = []
        has_blocked  = 'is_blocked'    in user_columns
        has_reg_type = 'registration_type' in user_columns
        has_free_ren = 'free_renewal_used' in user_columns
        has_trial    = 'is_trial_used' in user_columns

        blocked_filter = "AND COALESCE(is_blocked, 0) = 0" if has_blocked else ""

        # Where для периода (по конкретной колонке)
        users_period_w    = _date_filter_clause("created_at",  since_sql)
        payments_period_w = _date_filter_clause("created_at",  since_sql)

        # ── 1. Сводные KPI ───────────────────────────────────────────────────
        # Всего пользователей (без заблокированных)
        row = await async_query_db(
            f"SELECT COUNT(*) AS cnt FROM users WHERE 1=1 {blocked_filter}", (), one=True
        )
        users_total = (row['cnt'] if row else 0) or 0

        # Новые за период
        row = await async_query_db(
            f"SELECT COUNT(*) AS cnt FROM users WHERE {users_period_w} {blocked_filter}", (), one=True
        )
        users_new_period = (row['cnt'] if row else 0) or 0

        # Статусы подписок — единая Python-логика UTC (как на главной и в «Клиентах»)
        sub_stats = await db_helpers.aggregate_subscription_stats(exclude_blocked=bool(blocked_filter))
        subs_active = sub_stats['subs_active']
        subs_expired = sub_stats['subs_expired']
        subs_none = sub_stats['subs_none']
        subs_expiring_7d = sub_stats['subs_expiring_7d']

        # Заблокированные (если поле есть)
        blocked_count = 0
        if has_blocked:
            row = await async_query_db("SELECT COUNT(*) AS cnt FROM users WHERE is_blocked = 1", (), one=True)
            blocked_count = (row['cnt'] if row else 0) or 0

        # Триал использовали
        trial_used = 0
        trial_paid = 0
        if has_trial:
            row = await async_query_db(
                f"SELECT COUNT(*) AS cnt FROM users WHERE is_trial_used = 1 {blocked_filter}",
                (), one=True
            )
            trial_used = (row['cnt'] if row else 0) or 0

            # Конверсия триала: использовали триал И сделали хотя бы один успешный RUB-платёж
            row = await async_query_db(
                f"""SELECT COUNT(DISTINCT u.telegram_id) AS cnt
                    FROM users u
                    JOIN payments p ON p.telegram_id = u.telegram_id
                    WHERE u.is_trial_used = 1
                      AND p.status='succeeded' AND p.currency='RUB'
                      {('AND COALESCE(u.is_blocked, 0) = 0' if has_blocked else '')}""",
                (), one=True
            )
            trial_paid = (row['cnt'] if row else 0) or 0

        # Бесплатное продление использовали
        free_ren_used = 0
        if has_free_ren:
            row = await async_query_db(
                f"SELECT COUNT(*) AS cnt FROM users WHERE free_renewal_used = 1 {blocked_filter}",
                (), one=True
            )
            free_ren_used = (row['cnt'] if row else 0) or 0

        # Доход за период (только RUB succeeded)
        row = await async_query_db(
            f"""SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS cnt
                FROM payments
                WHERE status='succeeded' AND currency='RUB' AND {payments_period_w}""",
            (), one=True
        )
        revenue_period = float((row['total'] if row else 0) or 0)
        payments_succ_period = (row['cnt'] if row else 0) or 0

        # Доход всего время (RUB)
        row = await async_query_db(
            "SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS cnt FROM payments "
            "WHERE status='succeeded' AND currency='RUB'", (), one=True
        )
        revenue_total = float((row['total'] if row else 0) or 0)
        payments_succ_total = (row['cnt'] if row else 0) or 0

        # Уникальные плательщики за период
        row = await async_query_db(
            f"""SELECT COUNT(DISTINCT telegram_id) AS cnt FROM payments
                WHERE status='succeeded' AND currency='RUB' AND {payments_period_w}""",
            (), one=True
        )
        unique_payers_period = (row['cnt'] if row else 0) or 0

        # Уникальные плательщики всего
        row = await async_query_db(
            "SELECT COUNT(DISTINCT telegram_id) AS cnt FROM payments WHERE status='succeeded' AND currency='RUB'",
            (), one=True
        )
        unique_payers_total = (row['cnt'] if row else 0) or 0

        # Средний чек = revenue_period / payments_succ_period
        avg_check_period = (revenue_period / payments_succ_period) if payments_succ_period else 0
        # ARPU за период = revenue_period / unique_payers_period
        arpu_period = (revenue_period / unique_payers_period) if unique_payers_period else 0

        # ── Когортная конверсия ─────────────────────────────────────────────
        # Раньше формула была unique_payers_period / users_new_period — это
        # давало >100% (числитель — все плательщики периода вне зависимости от
        # даты регистрации; знаменатель — только новые юзеры). Теперь считаем
        # «сколько из НОВЫХ юзеров периода уже сделали платёж» — стабильно 0..100%.
        # На периоде 'all' since_sql=None → users_period_w = '1=1', т.е. это
        # глобальная конверсия (все юзеры → все, кто платил хотя бы раз).
        users_period_w_u = users_period_w.replace('created_at', 'u.created_at')
        u_blocked_join  = "AND COALESCE(u.is_blocked, 0) = 0" if has_blocked else ""
        row = await async_query_db(
            f"""SELECT COUNT(DISTINCT u.telegram_id) AS cnt
                FROM users u
                JOIN payments p ON p.telegram_id = u.telegram_id
                WHERE {users_period_w_u} {u_blocked_join}
                  AND p.status = 'succeeded' AND p.currency = 'RUB'""",
            (), one=True
        )
        cohort_paid_count = (row['cnt'] if row else 0) or 0
        conversion_period = (cohort_paid_count / users_new_period * 100) if users_new_period else 0

        # ── 2. Регистрации по дням (TG vs Сайт) ─────────────────────────────
        if group_kind == 'month':
            group_expr = "strftime('%Y-%m', created_at)"
        else:
            group_expr = "date(created_at)"

        if has_reg_type:
            rows_tg = await async_query_db(
                f"""SELECT {group_expr} AS p, COUNT(*) AS cnt FROM users
                    WHERE {users_period_w} {blocked_filter}
                      AND (registration_type = 'telegram' OR registration_type IS NULL OR registration_type = '')
                    GROUP BY {group_expr} ORDER BY p ASC""", ()
            )
            rows_site = await async_query_db(
                f"""SELECT {group_expr} AS p, COUNT(*) AS cnt FROM users
                    WHERE {users_period_w} {blocked_filter}
                      AND registration_type = 'site'
                    GROUP BY {group_expr} ORDER BY p ASC""", ()
            )
        else:
            rows_tg = await async_query_db(
                f"""SELECT {group_expr} AS p, COUNT(*) AS cnt FROM users
                    WHERE {users_period_w} {blocked_filter}
                    GROUP BY {group_expr} ORDER BY p ASC""", ()
            )
            rows_site = []

        tg_map   = {dict(r)['p']: dict(r)['cnt'] for r in rows_tg}
        site_map = {dict(r)['p']: dict(r)['cnt'] for r in rows_site}
        all_periods = sorted(set(list(tg_map.keys()) + list(site_map.keys())))

        registrations_chart = {
            'labels': all_periods,
            'tg':     [tg_map.get(p, 0)   for p in all_periods],
            'site':   [site_map.get(p, 0) for p in all_periods],
        }

        # ── 3. Платежи по дням (revenue + count) ────────────────────────────
        rows = await async_query_db(
            f"""SELECT {group_expr} AS p,
                       COALESCE(SUM(amount), 0) AS total,
                       COUNT(*) AS cnt
                FROM payments
                WHERE status='succeeded' AND currency='RUB' AND {payments_period_w}
                GROUP BY {group_expr} ORDER BY p ASC""",
            ()
        )
        pay_map_total = {dict(r)['p']: float(dict(r)['total'] or 0) for r in rows}
        pay_map_cnt   = {dict(r)['p']: int(dict(r)['cnt'] or 0)     for r in rows}
        pay_periods = sorted(pay_map_total.keys())

        revenue_chart = {
            'labels':  pay_periods,
            'revenue': [pay_map_total.get(p, 0) for p in pay_periods],
            'count':   [pay_map_cnt.get(p, 0)   for p in pay_periods],
        }

        # ── 4. Распределения ─────────────────────────────────────────────────
        # 4a. Источники регистрации
        sources = []
        if has_reg_type:
            rows = await async_query_db(
                f"""SELECT
                        CASE
                            WHEN registration_type = 'site' THEN 'Сайт'
                            ELSE 'Telegram'
                        END AS src,
                        COUNT(*) AS cnt
                    FROM users
                    WHERE {users_period_w} {blocked_filter}
                    GROUP BY src ORDER BY cnt DESC""", ()
            )
            sources = [{'label': dict(r)['src'], 'count': dict(r)['cnt']} for r in rows]

        # 4b. Статусы подписок (среди всех незаблокированных)
        sub_status = [
            {'label': 'Без подписки', 'count': subs_none},
            {'label': 'Активные', 'count': subs_active},
            {'label': 'Истёкшие', 'count': subs_expired},
        ]

        # 4d. Распределение пользователей по лимиту устройств (limit_ip)
        # Только клиенты с активной подпиской.
        limits_dist = []
        for lim in sorted(sub_stats['limits_by_active']):
            label = '∞ безлимит' if lim == 0 else f'Лимит {lim}'
            limits_dist.append({
                'label': label,
                'limit': lim,
                'count': sub_stats['limits_by_active'][lim],
            })

        # ── 5. Топ тарифов и методов оплаты — парсим metadata_json ─────────────
        # Тащим все succeeded RUB-платежи за период за один проход
        succ_rows = await async_query_db(
            f"""SELECT payment_id, telegram_id, amount, metadata_json
                FROM payments
                WHERE status='succeeded' AND currency='RUB' AND {payments_period_w}""",
            ()
        )

        method_agg: dict[str, dict] = {}    # name -> {count, total}
        type_agg = {                         # тип покупки
            'subscription':   {'count': 0, 'total': 0.0},
            'traffic_topup':  {'count': 0, 'total': 0.0},
            'device_upgrade': {'count': 0, 'total': 0.0},
        }

        for p in succ_rows:
            p = dict(p)
            pid    = p.get('payment_id') or ''
            amount = float(p.get('amount') or 0)
            md_j   = p.get('metadata_json')

            # Метод оплаты — приоритет prefix → metadata.payment_method
            method = _payment_method_from_id(pid)
            ptype  = None

            if md_j:
                try:
                    md = json.loads(md_j)
                except Exception:
                    md = None
                if isinstance(md, dict):
                    if not method:
                        method = _normalize_method_name(md.get('payment_method') or md.get('cms_name') or '')
                    ptype = md.get('payment_type')

            method = method or 'Неизвестно'

            # Агрегируем
            mref = method_agg.setdefault(method, {'count': 0, 'total': 0.0})
            mref['count'] += 1
            mref['total'] += amount

            # Тип покупки
            if ptype == 'traffic_renewal':
                tt = type_agg['traffic_topup']
            elif ptype == 'device_limit_upgrade':
                tt = type_agg['device_upgrade']
            else:
                tt = type_agg['subscription']
            tt['count'] += 1
            tt['total'] += amount

        # Методы оплаты
        methods_list = sorted(
            ({'label': k, 'count': v['count'], 'total': v['total']} for k, v in method_agg.items()),
            key=lambda x: x['total'], reverse=True
        )

        # Типы покупок
        types_list = [
            {'key': 'subscription',   'label': 'Подписки',          **type_agg['subscription']},
            {'key': 'traffic_topup',  'label': 'Докупка трафика',   **type_agg['traffic_topup']},
            {'key': 'device_upgrade', 'label': 'Лимит устройств',   **type_agg['device_upgrade']},
        ]

        # Топ-10 клиентов по сумме успешных RUB-платежей за период
        top_rows = await async_query_db(
            f"""SELECT telegram_id,
                       COUNT(payment_id) AS cnt,
                       SUM(amount) AS total
                FROM payments
                WHERE status = 'succeeded'
                  AND currency = 'RUB'
                  AND telegram_id IS NOT NULL
                  AND {payments_period_w}
                GROUP BY telegram_id
                ORDER BY total DESC
                LIMIT 10""",
            (),
        )
        top_customers_raw = [
            {
                'telegram_id': int(dict(r)['telegram_id']),
                'count': int(dict(r)['cnt'] or 0),
                'total': float(dict(r)['total'] or 0),
            }
            for r in (top_rows or [])
        ]
        customer_ids = [c['telegram_id'] for c in top_customers_raw]
        customer_names: dict[int, str] = {}
        if customer_ids:
            ph = ','.join(['?'] * len(customer_ids))
            for u in (await async_query_db(
                f"SELECT telegram_id, username, real_username FROM users WHERE telegram_id IN ({ph})",
                customer_ids
            )):
                u = dict(u)
                name = u.get('username') or (u.get('real_username') or '').lstrip('@') or str(u['telegram_id'])
                customer_names[int(u['telegram_id'])] = name
        for c in top_customers_raw:
            c['name'] = customer_names.get(c['telegram_id'], str(c['telegram_id']))
        top_customers = top_customers_raw

        # ── 6. Воронка ────────────────────────────────────────────────────────
        # Все клиенты, зарегистрированные за период (или за всё время для period='all')
        since_user_clause = f"u.created_at >= {since_sql}" if since_sql else "1=1"
        blocked_u = "AND COALESCE(u.is_blocked, 0) = 0" if has_blocked else ""

        funnel_registered = users_new_period

        # Шаг 2: из них хоть раз создали платёж (любого статуса)
        row = await async_query_db(
            f"""SELECT COUNT(DISTINCT u.telegram_id) AS cnt
                FROM users u
                JOIN payments p ON p.telegram_id = u.telegram_id
                WHERE {since_user_clause} {blocked_u}""",
            (), one=True
        )
        funnel_attempted = (row['cnt'] if row else 0) or 0

        # Шаг 3: оплатили хоть раз (succeeded RUB)
        row = await async_query_db(
            f"""SELECT COUNT(DISTINCT u.telegram_id) AS cnt
                FROM users u
                JOIN payments p ON p.telegram_id = u.telegram_id
                WHERE {since_user_clause} {blocked_u}
                  AND p.status='succeeded' AND p.currency='RUB'""",
            (), one=True
        )
        funnel_paid = (row['cnt'] if row else 0) or 0

        # Шаг 4: сделали ≥ 2 успешных платежа
        row = await async_query_db(
            f"""SELECT COUNT(*) AS cnt FROM (
                    SELECT u.telegram_id
                    FROM users u
                    JOIN payments p ON p.telegram_id = u.telegram_id
                    WHERE {since_user_clause} {blocked_u}
                      AND p.status='succeeded' AND p.currency='RUB'
                    GROUP BY u.telegram_id
                    HAVING COUNT(p.payment_id) >= 2
                )""",
            (), one=True
        )
        funnel_repeat = (row['cnt'] if row else 0) or 0

        funnel = {
            'registered': funnel_registered,
            'attempted':  funnel_attempted,
            'paid':       funnel_paid,
            'repeat':     funnel_repeat,
        }

        return jsonify({
            'ok': True,
            'period': period,
            'period_days': days,
            'kpi': {
                'users_total':         users_total,
                'users_new_period':    users_new_period,
                'subs_active':         subs_active,
                'subs_expired':        subs_expired,
                'subs_none':           subs_none,
                'subs_expiring_7d':    subs_expiring_7d,
                'blocked':             blocked_count,
                'trial_used':          trial_used,
                'trial_paid':          trial_paid,
                'free_renewal_used':   free_ren_used,
                'revenue_period':      round(revenue_period, 2),
                'revenue_total':       round(revenue_total, 2),
                'payments_succ_period': payments_succ_period,
                'payments_succ_total':  payments_succ_total,
                'unique_payers_period': unique_payers_period,
                'unique_payers_total':  unique_payers_total,
                'avg_check_period':    round(avg_check_period, 2),
                'arpu_period':         round(arpu_period, 2),
                # cohort_paid_count — числитель когортной конверсии:
                # сколько из users_new_period сделали платёж (когда-либо).
                'cohort_paid_count':   cohort_paid_count,
                'conversion_period':   round(conversion_period, 2),
            },
            'registrations_chart': registrations_chart,
            'revenue_chart':       revenue_chart,
            'distributions': {
                'sources':    sources,
                'sub_status': sub_status,
                'methods':    methods_list,
                'types':      types_list,
                'limits':     limits_dist,
            },
            'top_customers': top_customers,
            'funnel':        funnel,
        })

    admin_bp.add_url_rule('/analytics',     view_func=analytics_view, endpoint='analytics')
    admin_bp.add_url_rule('/analytics/data', view_func=analytics_data, endpoint='analytics_data')
