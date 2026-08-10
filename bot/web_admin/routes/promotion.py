"""Раздел «Продвижение» — рекламные кампании.

Идея простая: рекламная кампания — это пара (telegram_id, name).
- telegram_id уже существующего клиента (создаётся вручную в /clients).
- name — человекочитаемое название кампании ("TikTok Январь" и т.п.).
- Все, кто пришёл по реф-ссылке этого клиента (`/start <tid>` или `/?ref=<tid>`),
  получают `invited_by = <tid>` и `invited_by_method = 'referral'` (это уже
  работает в bot/handle_start и website/run.py — ничего нового не вводим).

Хранилище: один ключ в settings — `ad_campaigns` с JSON-массивом записей
[{telegram_id, name, created_at}, ...]. Без миграций.

Аналитика везде смотрит на `users.invited_by = ?` с фильтром
`COALESCE(invited_by_method,'') <> 'partner'`, чтобы не пересекаться
с разделом /partners.
"""
from datetime import datetime, timedelta, timezone
import json
import os
import time

from quart import current_app, flash, redirect, render_template, request, url_for


_AD_CAMPAIGNS_KEY = 'ad_campaigns'


def attach_promotion_routes(admin_bp_instance, query_db_func, execute_db_func):
    # ---------- helpers ----------

    async def _load_campaigns() -> list[dict]:
        """Возвращает список кампаний из settings.ad_campaigns. Безопасно к мусору."""
        from web_admin.async_db import async_query_db
        row = await async_query_db(
            "SELECT value FROM settings WHERE key = ?", (_AD_CAMPAIGNS_KEY,), one=True,
        )
        if not row or not row.get('value'):
            return []
        try:
            data = json.loads(row['value'])
            if not isinstance(data, list):
                return []
            out = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    tid = int(item.get('telegram_id') or 0)
                except (TypeError, ValueError):
                    continue
                if tid <= 0:
                    continue
                out.append({
                    'telegram_id': tid,
                    'name': str(item.get('name') or '').strip()[:120],
                    'created_at': str(item.get('created_at') or '').strip(),
                })
            return out
        except Exception as e:
            current_app.logger.warning(f"[PROMOTION] не удалось распарсить ad_campaigns: {e}")
            return []

    async def _save_campaigns(campaigns: list[dict]) -> None:
        from web_admin.async_db import async_execute_db
        payload = json.dumps(campaigns, ensure_ascii=False)
        await async_execute_db(
            """
            INSERT INTO settings (key, value, description)
            VALUES (?, ?, 'Рекламные кампании (JSON)')
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_AD_CAMPAIGNS_KEY, payload),
        )

    async def _site_base_url() -> str:
        """Базовый URL сайта: env SITE_URL > settings.website_url > ''. Без trailing /."""
        from web_admin.async_db import async_query_db
        env_v = (os.getenv('SITE_URL') or '').strip().rstrip('/')
        if env_v:
            return env_v
        row = await async_query_db(
            "SELECT value FROM settings WHERE key = 'website_url'", (), one=True,
        )
        return ((row or {}).get('value') or '').strip().rstrip('/')

    async def _bot_username() -> str:
        from web_admin.async_db import async_query_db
        row = await async_query_db(
            "SELECT value FROM settings WHERE key = 'bot_username'", (), one=True,
        )
        return ((row or {}).get('value') or '').strip().lstrip('@')

    def _build_links(bot_username: str, site_base: str, telegram_id: int) -> dict:
        bot = f"https://t.me/{bot_username}?start={telegram_id}" if bot_username else ''
        site = f"{site_base}/?ref={telegram_id}" if site_base else ''
        return {'bot': bot, 'site': site}

    async def _resolve_user(tid: int) -> dict | None:
        from web_admin.async_db import async_query_db
        return await async_query_db(
            """
            SELECT telegram_id, username, real_username,
                   COALESCE(is_blocked, 0) AS is_blocked
              FROM users
             WHERE telegram_id = ?
            """,
            (tid,), one=True,
        )

    # «Не партнёр» — общий фильтр для всех аналитик
    not_partner = "COALESCE(invited_by_method, '') <> 'partner'"

    async def _aggregate_for_ids(tids: list[int]) -> dict[int, dict]:
        """Считает агрегаты по списку telegram_id-кампаний за один-два запроса.
        Возвращает {tid: {invited, paid_users, total_payments, revenue}}.
        """
        if not tids:
            return {}
        from web_admin.async_db import async_query_db

        placeholders = ",".join(["?"] * len(tids))
        out: dict[int, dict] = {
            int(t): {'invited': 0, 'paid_users': 0, 'total_payments': 0, 'revenue': 0.0}
            for t in tids
        }

        # invited (не блокированные, не партнёрские)
        rows = await async_query_db(
            f"""
            SELECT invited_by AS pid, COUNT(*) AS c
              FROM users
             WHERE invited_by IN ({placeholders})
               AND {not_partner}
               AND COALESCE(is_blocked, 0) = 0
             GROUP BY invited_by
            """,
            tuple(tids),
        ) or []
        for r in rows:
            pid = int(r['pid'])
            if pid in out:
                out[pid]['invited'] = int(r['c'] or 0)

        # платежи: paid_users + total_payments + revenue
        rows = await async_query_db(
            f"""
            SELECT u.invited_by AS pid,
                   COUNT(DISTINCT u.telegram_id) AS paid_users,
                   COUNT(p.payment_id) AS total_payments,
                   COALESCE(SUM(p.amount), 0) AS revenue
              FROM payments p
              JOIN users u ON u.telegram_id = p.telegram_id
             WHERE u.invited_by IN ({placeholders})
               AND COALESCE(u.invited_by_method, '') <> 'partner'
               AND p.status = 'succeeded'
             GROUP BY u.invited_by
            """,
            tuple(tids),
        ) or []
        for r in rows:
            pid = int(r['pid'])
            if pid in out:
                out[pid]['paid_users'] = int(r['paid_users'] or 0)
                out[pid]['total_payments'] = int(r['total_payments'] or 0)
                out[pid]['revenue'] = float(r['revenue'] or 0)

        return out

    # ---------- /promotion (список) ----------

    @admin_bp_instance.route('/promotion', methods=['GET'])
    async def promotion_list():
        """Список рекламных кампаний с агрегатами."""
        t0 = time.perf_counter()
        campaigns = await _load_campaigns()
        tids = [c['telegram_id'] for c in campaigns]

        # Подгружаем username для отображения
        from web_admin.async_db import async_query_db
        users_map: dict[int, dict] = {}
        if tids:
            placeholders = ",".join(["?"] * len(tids))
            rows = await async_query_db(
                f"""
                SELECT telegram_id, username, real_username,
                       COALESCE(is_blocked, 0) AS is_blocked
                  FROM users
                 WHERE telegram_id IN ({placeholders})
                """,
                tuple(tids),
            ) or []
            users_map = {int(r['telegram_id']): dict(r) for r in rows}

        # Агрегаты
        agg = await _aggregate_for_ids(tids)

        bot_username = await _bot_username()
        site_base = await _site_base_url()

        enriched = []
        for c in campaigns:
            tid = c['telegram_id']
            u = users_map.get(tid)
            a = agg.get(tid, {})
            invited = int(a.get('invited') or 0)
            paid_users = int(a.get('paid_users') or 0)
            conv = (paid_users / invited * 100.0) if invited else 0.0
            enriched.append({
                **c,
                'username': (u or {}).get('real_username') or (u or {}).get('username'),
                'is_blocked': int((u or {}).get('is_blocked') or 0),
                'user_exists': u is not None,
                'invited': invited,
                'paid_users': paid_users,
                'total_payments': int(a.get('total_payments') or 0),
                'revenue': float(a.get('revenue') or 0),
                'conv': conv,
                'links': _build_links(bot_username, site_base, tid),
            })

        # Сортировка по revenue desc по дефолту
        enriched.sort(key=lambda x: (-(x.get('revenue') or 0), -(x.get('invited') or 0)))

        # KPI глобальные
        kpi = {
            'campaigns': len(campaigns),
            'invited': sum(c['invited'] for c in enriched),
            'paid_users': sum(c['paid_users'] for c in enriched),
            'revenue': sum(c['revenue'] for c in enriched),
        }

        elapsed_ms = (time.perf_counter() - t0) * 1000
        if elapsed_ms > 800:
            current_app.logger.warning(
                f"[PROMOTION] promotion_list slow: {elapsed_ms:.0f}ms "
                f"(campaigns={len(campaigns)})"
            )

        return await render_template(
            'promotion_list.html',
            campaigns=enriched,
            kpi=kpi,
            bot_username=bot_username,
            site_base=site_base,
        )

    # ---------- POST: добавить ----------

    @admin_bp_instance.route('/promotion/add', methods=['POST'])
    async def promotion_add():
        form = await request.form
        tid_raw = (form.get('telegram_id') or '').strip()
        name = (form.get('name') or '').strip()[:120]

        try:
            tid = int(tid_raw)
            if tid <= 0:
                raise ValueError
        except (TypeError, ValueError):
            await flash('Некорректный Telegram ID', 'danger')
            return redirect(url_for('admin.promotion_list'))

        if not name:
            await flash('Укажите название кампании', 'danger')
            return redirect(url_for('admin.promotion_list'))

        u = await _resolve_user(tid)
        if not u:
            await flash(
                f'Клиент с ID {tid} не найден в базе. Сначала создайте его '
                f'вручную в разделе «Клиенты», потом добавьте кампанию.',
                'danger',
            )
            return redirect(url_for('admin.promotion_list'))

        campaigns = await _load_campaigns()
        if any(c['telegram_id'] == tid for c in campaigns):
            await flash(
                f'Этот клиент (ID {tid}) уже привязан к существующей кампании. '
                f'Переименуйте её вместо добавления.',
                'warning',
            )
            return redirect(url_for('admin.promotion_list'))

        campaigns.append({
            'telegram_id': tid,
            'name': name,
            'created_at': datetime.now(timezone.utc).isoformat(),
        })
        await _save_campaigns(campaigns)
        await flash(f'Кампания «{name}» создана', 'success')
        return redirect(url_for('admin.promotion_list'))

    # ---------- POST: переименовать ----------

    @admin_bp_instance.route('/promotion/<int:telegram_id>/rename', methods=['POST'])
    async def promotion_rename(telegram_id: int):
        form = await request.form
        name = (form.get('name') or '').strip()[:120]
        if not name:
            await flash('Название не может быть пустым', 'danger')
            return redirect(url_for('admin.promotion_details', telegram_id=telegram_id))

        campaigns = await _load_campaigns()
        found = False
        for c in campaigns:
            if c['telegram_id'] == telegram_id:
                c['name'] = name
                found = True
                break
        if not found:
            await flash('Кампания не найдена', 'danger')
            return redirect(url_for('admin.promotion_list'))

        await _save_campaigns(campaigns)
        await flash('Название обновлено', 'success')
        return redirect(url_for('admin.promotion_details', telegram_id=telegram_id))

    # ---------- POST: удалить ----------

    @admin_bp_instance.route('/promotion/<int:telegram_id>/delete', methods=['POST'])
    async def promotion_delete(telegram_id: int):
        campaigns = await _load_campaigns()
        new = [c for c in campaigns if c['telegram_id'] != telegram_id]
        if len(new) == len(campaigns):
            await flash('Кампания не найдена', 'danger')
            return redirect(url_for('admin.promotion_list'))
        await _save_campaigns(new)
        await flash('Кампания удалена. Реф-связи приглашённых сохранены.', 'success')
        return redirect(url_for('admin.promotion_list'))

    # ---------- /promotion/<tid> (детали) ----------

    @admin_bp_instance.route('/promotion/<int:telegram_id>', methods=['GET'])
    async def promotion_details(telegram_id: int):
        from web_admin.async_db import async_query_db

        campaigns = await _load_campaigns()
        campaign = next((c for c in campaigns if c['telegram_id'] == telegram_id), None)
        if not campaign:
            await flash('Кампания не найдена', 'danger')
            return redirect(url_for('admin.promotion_list'))

        owner = await _resolve_user(telegram_id)

        bot_username = await _bot_username()
        site_base = await _site_base_url()
        links = _build_links(bot_username, site_base, telegram_id)

        # KPI
        invited_total = await async_query_db(
            f"""
            SELECT COUNT(*) AS c FROM users
             WHERE invited_by = ?
               AND {not_partner}
               AND COALESCE(is_blocked, 0) = 0
            """,
            (telegram_id,), one=True,
        )
        invited = int((invited_total or {}).get('c') or 0)

        pay_row = await async_query_db(
            f"""
            SELECT COUNT(DISTINCT u.telegram_id) AS paid_users,
                   COUNT(p.payment_id) AS total_payments,
                   COALESCE(SUM(p.amount), 0) AS revenue
              FROM payments p
              JOIN users u ON u.telegram_id = p.telegram_id
             WHERE u.invited_by = ?
               AND COALESCE(u.invited_by_method, '') <> 'partner'
               AND p.status = 'succeeded'
            """,
            (telegram_id,), one=True,
        )
        paid_users = int((pay_row or {}).get('paid_users') or 0)
        total_payments = int((pay_row or {}).get('total_payments') or 0)
        revenue = float((pay_row or {}).get('revenue') or 0)

        active_subs = await async_query_db(
            f"""
            SELECT COUNT(*) AS c FROM users
             WHERE invited_by = ? AND {not_partner}
               AND COALESCE(is_blocked, 0) = 0
               AND subscription_end_date IS NOT NULL
               AND datetime(subscription_end_date) > datetime('now')
            """,
            (telegram_id,), one=True,
        )
        active_subs = int((active_subs or {}).get('c') or 0)

        expired_subs = await async_query_db(
            f"""
            SELECT COUNT(*) AS c FROM users
             WHERE invited_by = ? AND {not_partner}
               AND COALESCE(is_blocked, 0) = 0
               AND subscription_end_date IS NOT NULL
               AND datetime(subscription_end_date) <= datetime('now')
            """,
            (telegram_id,), one=True,
        )
        expired_subs = int((expired_subs or {}).get('c') or 0)

        no_sub = await async_query_db(
            f"""
            SELECT COUNT(*) AS c FROM users
             WHERE invited_by = ? AND {not_partner}
               AND COALESCE(is_blocked, 0) = 0
               AND subscription_end_date IS NULL
            """,
            (telegram_id,), one=True,
        )
        no_sub = int((no_sub or {}).get('c') or 0)

        trial_only = await async_query_db(
            f"""
            SELECT COUNT(*) AS c FROM users u
             WHERE u.invited_by = ?
               AND COALESCE(u.invited_by_method, '') <> 'partner'
               AND COALESCE(u.is_blocked, 0) = 0
               AND COALESCE(u.is_trial_used, 0) = 1
               AND NOT EXISTS (
                 SELECT 1 FROM payments p
                  WHERE p.telegram_id = u.telegram_id AND p.status = 'succeeded'
               )
            """,
            (telegram_id,), one=True,
        )
        trial_only = int((trial_only or {}).get('c') or 0)

        conv = (paid_users / invited * 100.0) if invited else 0.0

        kpi = {
            'invited': invited,
            'paid_users': paid_users,
            'total_payments': total_payments,
            'revenue': revenue,
            'active_subs': active_subs,
            'expired_subs': expired_subs,
            'no_sub': no_sub,
            'trial_only': trial_only,
            'conv': conv,
        }

        # Sparkline новых рефералов за 30 дней
        spark_rows = await async_query_db(
            f"""
            SELECT date(created_at) AS d, COUNT(*) AS c
              FROM users
             WHERE invited_by = ? AND {not_partner}
               AND date(created_at) >= date('now', '-29 days')
             GROUP BY date(created_at)
             ORDER BY d ASC
            """,
            (telegram_id,),
        ) or []
        spark_map = {r['d']: int(r['c'] or 0) for r in spark_rows}
        chart_days = []
        today = datetime.now(timezone.utc).date()
        for i in range(29, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            chart_days.append({'date': d, 'value': spark_map.get(d, 0)})

        # Список рефералов (пагинация)
        try:
            page = max(1, int(request.args.get('page', 1)))
        except (TypeError, ValueError):
            page = 1
        per_page = 20
        total_pages = max(1, (invited + per_page - 1) // per_page)
        page = min(page, total_pages)
        offset = (page - 1) * per_page

        refs = await async_query_db(
            f"""
            SELECT u.telegram_id,
                   u.username,
                   u.real_username,
                   u.created_at,
                   u.subscription_end_date,
                   COALESCE(u.is_blocked, 0) AS is_blocked,
                   (SELECT COUNT(*) FROM payments p
                     WHERE p.telegram_id = u.telegram_id AND p.status = 'succeeded') AS pays_count,
                   (SELECT COALESCE(SUM(p.amount), 0) FROM payments p
                     WHERE p.telegram_id = u.telegram_id AND p.status = 'succeeded') AS pays_sum
              FROM users u
             WHERE u.invited_by = ? AND {not_partner}
               AND COALESCE(u.is_blocked, 0) = 0
             ORDER BY datetime(u.created_at) DESC, u.telegram_id DESC
             LIMIT ? OFFSET ?
            """,
            (telegram_id, per_page, offset),
        ) or []

        now_utc = datetime.now(timezone.utc)
        referrals = []
        for r in refs:
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
                'created_at': r.get('created_at'),
                'sub_status': sub_status,
                'pays_count': int(r.get('pays_count') or 0),
                'pays_sum': float(r.get('pays_sum') or 0),
            })

        return await render_template(
            'promotion_details.html',
            campaign=campaign,
            owner=owner,
            kpi=kpi,
            chart_days=chart_days,
            referrals=referrals,
            page=page,
            total_pages=total_pages,
            invited_total=invited,
            links=links,
            bot_username=bot_username,
            site_base=site_base,
        )
