import re
import secrets
import string
from datetime import datetime, timezone

from quart import render_template, request, redirect, url_for, flash, jsonify
from web_admin.async_db import async_execute_db, async_query_db


# Допустимые символы кода: A-Z, 0-9, -, _ (3..64)
_CODE_RE = re.compile(r'[A-Z0-9_\-]{3,64}')

# Алфавит для генерации случайных кодов: убраны легко путающиеся символы (0/O, 1/I/L).
_RANDOM_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


def _generate_unique_code(length: int = 8) -> str:
    return ''.join(secrets.choice(_RANDOM_ALPHABET) for _ in range(length))


def attach_promo_routes(admin_bp_instance, query_db_func, execute_db_func):

    # ─────────────────────────────────────────────────────────────
    #  Список промокодов + поиск/фильтр + агрегаты
    # ─────────────────────────────────────────────────────────────
    @admin_bp_instance.route('/promo')
    async def promo_list():
        page = request.args.get('page', 1, type=int)
        per_page = 24
        if page < 1:
            page = 1
        offset = (page - 1) * per_page

        q = (request.args.get('q') or '').strip().upper()
        status = (request.args.get('status') or 'all').strip().lower()
        if status not in ('all', 'active', 'inactive', 'exhausted', 'used'):
            status = 'all'

        # ── условие WHERE ──
        where_parts: list[str] = []
        params: list = []
        if q:
            where_parts.append("UPPER(code) LIKE ?")
            params.append(f'%{q}%')
        if status == 'active':
            where_parts.append("is_active = 1 AND (max_uses = 0 OR used_count < max_uses)")
        elif status == 'inactive':
            where_parts.append("is_active = 0")
        elif status == 'exhausted':
            where_parts.append("max_uses > 0 AND used_count >= max_uses")
        elif status == 'used':
            where_parts.append("used_count > 0")

        where_sql = ('WHERE ' + ' AND '.join(where_parts)) if where_parts else ''

        # ── общее количество для пагинации ──
        total_row = await async_query_db(
            f"SELECT COUNT(*) AS c FROM promo_codes {where_sql}",
            tuple(params), one=True
        )
        total_codes = (total_row['c'] if total_row else 0) or 0
        total_pages = max(1, (total_codes + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages
            offset = (page - 1) * per_page

        # ── страница ──
        promo_codes = await async_query_db(
            f"""
            SELECT code, is_active, activated_by_telegram_id, activated_at,
                   created_at, days, max_uses, used_count
            FROM promo_codes
            {where_sql}
            ORDER BY
                CASE WHEN is_active = 1 AND (max_uses = 0 OR used_count < max_uses)
                     THEN 0 ELSE 1 END,
                COALESCE(created_at, '') DESC,
                code ASC
            LIMIT ? OFFSET ?
            """,
            tuple(params) + (per_page, offset)
        )

        # ── активации по кодам страницы ──
        codes_on_page = [row['code'] for row in promo_codes] if promo_codes else []
        redemptions_by_code: dict[str, list] = {}
        if codes_on_page:
            placeholders = ",".join(["?"] * len(codes_on_page))
            rows = await async_query_db(
                f"""
                SELECT r.code, r.telegram_id, r.used_at, u.username
                FROM promo_redemptions r
                LEFT JOIN users u ON u.telegram_id = r.telegram_id
                WHERE r.code IN ({placeholders})
                ORDER BY r.used_at DESC
                """,
                tuple(codes_on_page)
            )
            for r in rows:
                redemptions_by_code.setdefault(r['code'], []).append({
                    'telegram_id': r['telegram_id'],
                    'username': r['username'],
                    'used_at': r['used_at'],
                })

        # ── глобальные агрегаты (без фильтра — для виджетов «всего/активных»)
        agg = await async_query_db(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN is_active = 1 AND (max_uses = 0 OR used_count < max_uses)
                         THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN max_uses > 0 AND used_count >= max_uses
                         THEN 1 ELSE 0 END) AS exhausted,
                SUM(used_count) AS redemptions
            FROM promo_codes
            """,
            (), one=True
        )
        stats = {
            'total':       int((agg['total']       if agg else 0) or 0),
            'active':      int((agg['active']      if agg else 0) or 0),
            'exhausted':   int((agg['exhausted']   if agg else 0) or 0),
            'redemptions': int((agg['redemptions'] if agg else 0) or 0),
        }

        return await render_template(
            'promo_list.html',
            promo_codes=promo_codes,
            page=page,
            total_pages=total_pages,
            total_codes=total_codes,
            redemptions_by_code=redemptions_by_code,
            stats=stats,
            q=q,
            status=status,
        )

    # ─────────────────────────────────────────────────────────────
    #  Создание (одиночное / случайное)
    # ─────────────────────────────────────────────────────────────
    @admin_bp_instance.route('/promo/create', methods=['POST'])
    async def promo_create():
        form = await request.form
        manual_code = ((form.get('manual_code') or '')).strip().upper()
        random_mode = (form.get('random') or '').strip() in ('1', 'true', 'on')

        try:
            days = int(form.get('days') or 30)
            if days < 1:
                days = 1
        except Exception:
            days = 30
        try:
            max_uses = int(form.get('max_uses') or 1)
            if max_uses < 0:
                max_uses = 0
        except Exception:
            max_uses = 1

        # ── случайный код (если ручной не задан и нажата «Сгенерировать») ──
        if not manual_code and random_mode:
            for _ in range(8):
                candidate = _generate_unique_code(8)
                exists = await async_query_db(
                    "SELECT 1 FROM promo_codes WHERE code = ?",
                    (candidate,), one=True
                )
                if not exists:
                    manual_code = candidate
                    break

        if not manual_code:
            await flash('Укажите код или нажмите «Сгенерировать».', 'warning')
            return redirect(url_for('admin.promo_list'))

        if not _CODE_RE.fullmatch(manual_code):
            await flash('Неверный формат кода. Разрешены A-Z, 0-9, "-", "_" (3-64 символа).', 'danger')
            return redirect(url_for('admin.promo_list'))

        try:
            now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
            await async_execute_db(
                """
                INSERT INTO promo_codes (code, is_active, days, max_uses, used_count, created_at)
                VALUES (?, 1, ?, ?, 0, ?)
                """,
                (manual_code, days, max_uses, now_iso)
            )
            await flash(
                f'Промокод {manual_code} создан на {days} дн. '
                f'(лимит {"∞" if max_uses == 0 else max_uses}).',
                'success'
            )
        except Exception:
            await flash(
                f'Не удалось создать промокод {manual_code}: возможно, такой код уже существует.',
                'danger'
            )
        return redirect(url_for('admin.promo_list'))

    # ─────────────────────────────────────────────────────────────
    #  Включить/отключить (toggle, AJAX)
    # ─────────────────────────────────────────────────────────────
    @admin_bp_instance.route('/promo/toggle/<code>', methods=['POST'])
    async def promo_toggle(code):
        try:
            row = await async_query_db(
                "SELECT is_active FROM promo_codes WHERE code = ?",
                (code,), one=True
            )
            if not row:
                return jsonify({'ok': False, 'error': 'not_found'}), 404
            new_state = 0 if row['is_active'] else 1
            await async_execute_db(
                "UPDATE promo_codes SET is_active = ? WHERE code = ?",
                (new_state, code)
            )
            return jsonify({'ok': True, 'is_active': new_state})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

    # ─────────────────────────────────────────────────────────────
    #  Экспорт / удаление
    # ─────────────────────────────────────────────────────────────
    @admin_bp_instance.route('/promo/export')
    async def promo_export():
        promo_codes = await async_query_db(
            "SELECT code, is_active, days, used_count, max_uses, created_at FROM promo_codes "
            "ORDER BY created_at DESC, code ASC"
        )
        lines = ['code\tstatus\tdays\tused\tlimit\tcreated_at']
        for row in promo_codes:
            keys = row.keys()
            status = 'active' if row['is_active'] else 'inactive'
            days = row['days'] if 'days' in keys else 30
            used = row['used_count'] if 'used_count' in keys else 0
            limit = row['max_uses'] if 'max_uses' in keys else 1
            created = (row['created_at'] if 'created_at' in keys else '') or ''
            lines.append(f"{row['code']}\t{status}\t{days}\t{used}\t{limit}\t{created}")
        body = '\n'.join(lines)
        return body, 200, {
            'Content-Type': 'text/plain; charset=utf-8',
            'Content-Disposition': 'attachment; filename=promo_codes.tsv',
        }

    @admin_bp_instance.route('/promo/delete_all', methods=['POST'])
    async def promo_delete_all():
        try:
            await async_execute_db("DELETE FROM promo_redemptions")
            await async_execute_db("DELETE FROM promo_codes")
            await flash('Все промокоды удалены.', 'success')
        except Exception as e:
            await flash(f'Ошибка удаления промокодов: {e}', 'danger')
        return redirect(url_for('admin.promo_list'))

    @admin_bp_instance.route('/promo/delete/<code>', methods=['POST'])
    async def promo_delete_code(code):
        try:
            await async_execute_db("DELETE FROM promo_redemptions WHERE code = ?", (code,))
            await async_execute_db("DELETE FROM promo_codes WHERE code = ?", (code,))
            await flash(f'Промокод {code} удалён.', 'success')
        except Exception as e:
            await flash(f'Не удалось удалить промокод {code}: {e}', 'danger')
        return redirect(url_for('admin.promo_list'))
