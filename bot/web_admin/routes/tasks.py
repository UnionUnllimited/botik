from quart import render_template, url_for, redirect, flash, request, jsonify
import json
import math
from datetime import datetime, timedelta


ERR_DETAILS_PER_PAGE = 25
FAILED_USERS_PER_PAGE = 25


async def _ensure_news_tasks_news_text_column(execute_db_async):
    """SQLite: добавить колонку текста рассылки для старых БД."""
    try:
        await execute_db_async("ALTER TABLE news_tasks ADD COLUMN news_text TEXT", ())
    except Exception:
        pass


def _safe_positive_int(val, default=1):
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return default


def attach_tasks_routes(admin_bp_instance, query_db_func, execute_db_func):
    @admin_bp_instance.route('/tasks')
    async def tasks_list():
        """Показывает список задач отправки новостей."""
        await _ensure_news_tasks_news_text_column(execute_db_func)

        news_tasks = await query_db_func(
            """
            SELECT * FROM news_tasks 
            ORDER BY created_at DESC 
            LIMIT 20
            """
        )
        return await render_template('tasks.html', news_tasks=news_tasks)

    @admin_bp_instance.route('/tasks/<task_id>')
    async def task_details(task_id):
        """Показывает детали задачи рассылки новостей."""
        if task_id.startswith('cleanup_'):
            await flash('Задача очистки 3X-UI устарела и больше не используется.', 'info')
            return redirect(url_for('admin.tasks_list'))

        await _ensure_news_tasks_news_text_column(execute_db_func)

        if task_id.startswith('news_'):
            task = await query_db_func("SELECT * FROM news_tasks WHERE task_id = ?", (task_id,), one=True)
            task_type = 'news'
        else:
            await flash('Неизвестный тип задачи', 'danger')
            return redirect(url_for('admin.tasks_list'))

        if not task:
            await flash('Задача не найдена', 'danger')
            return redirect(url_for('admin.tasks_list'))

        error_details = []
        failed_users = []

        def _normalize_errors(seq):
            if seq is None:
                return []
            if isinstance(seq, str):
                return [seq]
            if isinstance(seq, list):
                return [str(x) for x in seq]
            return [str(seq)]

        def _normalize_failed_users(rows):
            out = []
            if not isinstance(rows, list):
                return out
            for r in rows:
                if isinstance(r, dict):
                    out.append({
                        'telegram_id': r.get('telegram_id', '—'),
                        'username': r.get('username'),
                    })
            return out

        if task['error_details']:
            try:
                error_data = json.loads(task['error_details'])
                if isinstance(error_data, dict):
                    error_details = _normalize_errors(error_data.get('error_details', []))
                    failed_users = error_data.get('failed_users') or []
                    if not isinstance(failed_users, list):
                        failed_users = []
                    failed_user_ids = error_data.get('failed_user_ids', [])
                    if failed_user_ids:
                        placeholders = ','.join(['?' for _ in failed_user_ids])
                        users_info = await query_db_func(
                            f"SELECT telegram_id, username FROM users WHERE telegram_id IN ({placeholders})",
                            failed_user_ids
                        )
                        failed_users = [
                            {'telegram_id': dict(user)['telegram_id'], 'username': dict(user)['username']}
                            for user in users_info
                        ]
                else:
                    error_details = _normalize_errors(error_data)
            except Exception:
                error_details = _normalize_errors(task['error_details'])

        failed_users = _normalize_failed_users(failed_users)

        err_page = _safe_positive_int(request.args.get('err_page'))
        usr_page = _safe_positive_int(request.args.get('usr_page'))

        errors_total = len(error_details)
        users_total = len(failed_users)

        errors_total_pages = max(1, math.ceil(errors_total / ERR_DETAILS_PER_PAGE)) if errors_total else 1
        users_total_pages = max(1, math.ceil(users_total / FAILED_USERS_PER_PAGE)) if users_total else 1

        err_page = min(err_page, errors_total_pages)
        usr_page = min(usr_page, users_total_pages)

        e0 = (err_page - 1) * ERR_DETAILS_PER_PAGE
        error_details = error_details[e0:e0 + ERR_DETAILS_PER_PAGE]

        u0 = (usr_page - 1) * FAILED_USERS_PER_PAGE
        failed_users = failed_users[u0:u0 + FAILED_USERS_PER_PAGE]

        return await render_template(
            'task_details.html',
            task=task,
            task_type=task_type,
            error_details=error_details,
            failed_users=failed_users,
            errors_page=err_page,
            errors_total_pages=errors_total_pages,
            errors_total=errors_total,
            errors_per_page=ERR_DETAILS_PER_PAGE,
            errors_row_offset=e0,
            users_page=usr_page,
            users_total_pages=users_total_pages,
            users_total=users_total,
            users_per_page=FAILED_USERS_PER_PAGE,
            users_row_offset=u0,
        )

    @admin_bp_instance.route('/tasks/cleanup', methods=['POST'])
    async def cleanup_old_tasks():
        """Очищает старые завершённые задачи рассылок (старше 7 дней)."""
        from web_admin.async_db import async_execute_db
        
        try:
            cutoff_date = (datetime.now() - timedelta(days=7)).isoformat()
            
            from web_admin.async_db import async_query_db
            
            news_count = await async_query_db(
                "SELECT COUNT(*) as cnt FROM news_tasks WHERE status = 'completed' AND completed_at < ?",
                (cutoff_date,),
                one=True
            )
            deleted_news = news_count['cnt'] if news_count else 0
            
            await async_execute_db(
                "DELETE FROM news_tasks WHERE status = 'completed' AND completed_at < ?",
                (cutoff_date,)
            )
            # Legacy: старые записи очистки 3X-UI — удаляем из БД без отображения в UI
            try:
                await async_execute_db(
                    "DELETE FROM cleanup_tasks WHERE status = 'completed' AND completed_at < ?",
                    (cutoff_date,)
                )
            except Exception:
                pass
            
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({
                    'ok': True,
                    'message': f'Удалено задач рассылок: {deleted_news}',
                    'deleted_counts': {'news': deleted_news},
                    'total': deleted_news
                })
            
            await flash(
                f'Удалено задач рассылок: {deleted_news}',
                'success' if deleted_news > 0 else 'info'
            )
            return redirect(url_for('admin.tasks_list'))
            
        except Exception as e:
            error_msg = f'Ошибка при очистке задач: {e}'
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'ok': False, 'error': error_msg}), 500
            await flash(error_msg, 'danger')
            return redirect(url_for('admin.tasks_list'))


