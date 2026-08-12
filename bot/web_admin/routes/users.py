from quart import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
import html
import re
from pytz import timezone as _tz
import pytz
import json
import math
from datetime import datetime, timezone, timedelta
import asyncio
import os
from urllib.parse import quote
from loguru import logger
import httpx

# Импорты из проекта
from web_admin.async_db import async_execute_db, async_query_db, get_table_columns_cached
from app_config import app_conf
import db_helpers
from tg_sender import send_telegram_message
from keyboards import get_back_to_main_keyboard, get_success_with_referral_keyboard
from subscription_manager import grant_subscription
from aiogram.utils.markdown import hcode

# Легаси: локальный словарь для инвалидации после POST.
# Эндпоинт /users/<id>/traffic кеш не использует — ответ всегда свежий.
_TRAFFIC_CACHE: dict = {}

_RW_ONLINE_WINDOW_MIN = 10


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return few
    return many


def _format_last_online(online_at):
    """Статус онлайна из users.online_at: до 10 мин — «Онлайн», иначе «был в сети … назад»."""
    if not online_at:
        return {
            'online_label': '—',
            'online_is_live': False,
            'online_has_data': False,
            'online_count': 0,
        }
    try:
        dt = datetime.fromisoformat(str(online_at).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        total_seconds = max(0, (datetime.now(timezone.utc) - dt).total_seconds())
        if total_seconds < _RW_ONLINE_WINDOW_MIN * 60:
            return {
                'online_label': 'Онлайн',
                'online_is_live': True,
                'online_has_data': True,
                'online_count': 1,
            }
        if total_seconds < 3600:
            m = max(1, int(total_seconds / 60))
            label = f"был в сети {m} {_ru_plural(m, 'минуту', 'минуты', 'минут')} назад"
        elif total_seconds < 86400:
            h = max(1, int(total_seconds / 3600))
            label = f"был в сети {h} {_ru_plural(h, 'час', 'часа', 'часов')} назад"
        else:
            d = max(1, int(total_seconds / 86400))
            label = f"был в сети {d} {_ru_plural(d, 'день', 'дня', 'дней')} назад"
        return {
            'online_label': label,
            'online_is_live': False,
            'online_has_data': True,
            'online_count': 0,
        }
    except Exception:
        return {
            'online_label': '—',
            'online_is_live': False,
            'online_has_data': False,
            'online_count': 0,
        }


def _users_rw_traffic_cols_plain(column_names, alias: str = '') -> str:
    prefix = f'{alias}.' if alias else ''
    parts = []
    if 'total_bytes' in column_names:
        parts.append(f'COALESCE({prefix}total_bytes, 0) AS total_bytes')
    if 'total_total_bytes' in column_names:
        parts.append(f'COALESCE({prefix}total_total_bytes, 0) AS total_total_bytes')
    if 'online_at' in column_names:
        parts.append(f'{prefix}online_at')
    return (', ' + ', '.join(parts)) if parts else ''


def _enrich_user_traffic_online(user: dict) -> None:
    total_bytes = int(user.get('total_total_bytes') or 0)
    daily_bytes = int(user.get('total_bytes') or 0)
    user['total_traffic'] = total_bytes
    user['total_traffic_mb'] = total_bytes / (1024 * 1024)
    user['daily_consumption'] = daily_bytes
    user['daily_consumption_mb'] = daily_bytes / (1024 * 1024)
    status = _format_last_online(user.get('online_at'))
    user['online_label'] = status['online_label']
    user['online_is_live'] = status['online_is_live']
    user['online_count'] = status['online_count']
    user['online_has_data'] = status['online_has_data']
    user['online_servers'] = []
    user['online_exceeds_limit'] = False


def _enrich_users_list_traffic_online(users_list: list) -> None:
    for user in users_list:
        _enrich_user_traffic_online(user)


def _users_rw_traffic_select(column_names) -> str:
    parts = []
    if 'total_bytes' in column_names:
        parts.append('COALESCE(u.total_bytes, 0) AS total_bytes')
    if 'total_total_bytes' in column_names:
        parts.append('COALESCE(u.total_total_bytes, 0) AS total_total_bytes')
    if 'online_at' in column_names:
        parts.append('u.online_at')
    return (', ' + ', '.join(parts)) if parts else ''


def _users_rw_traffic_groupby(column_names) -> str:
    parts = []
    if 'total_bytes' in column_names:
        parts.append('u.total_bytes')
    if 'total_total_bytes' in column_names:
        parts.append('u.total_total_bytes')
    if 'online_at' in column_names:
        parts.append('u.online_at')
    return (', ' + ', '.join(parts)) if parts else ''


def _users_list_apply_limit_email_filters(
    where_parts: list,
    params: list,
    *,
    and_prefix: bool,
    filter_limit_ip,
    no_lk_email: bool,
    has_email_col: bool,
) -> None:
    pfx = 'AND ' if and_prefix else ''
    if filter_limit_ip is not None:
        where_parts.append(f'{pfx}COALESCE(u.limit_ip, 0) = ?')
        params.append(filter_limit_ip)
    if no_lk_email and has_email_col:
        where_parts.append(f"{pfx}(u.email IS NULL OR TRIM(u.email) = '')")


_TRAFFIC_CACHE_TTL_SECONDS = 60




def attach_user_routes(admin_bp_instance, query_db_func, execute_db_func):
    @admin_bp_instance.route('/users', methods=['GET', 'POST'])
    async def users_list():
        # Импортируем асинхронные обертки
        # Список пользователей: фильтры/пагинация
        # Универсальный поиск: search_query (новый) или search_id (legacy)
        _raw_query = (request.args.get('search_query') or '').strip()
        if not _raw_query:
            _raw_query = request.args.get('search_id', '')
        search_query = _raw_query  # строка для шаблона
        # Определяем тип поиска
        search_id = None
        search_email = None
        search_username = None
        # SQLite INTEGER ограничен 8 байтами (signed) — ±2^63-1.
        # Telegram ID гарантированно влезает в этот диапазон, поэтому всё, что больше,
        # реальным юзером быть не может: считаем такой ввод текстовым поиском.
        _SQLITE_INT64_MAX = (1 << 63) - 1
        if _raw_query:
            if _raw_query.lstrip('-').isdigit():
                try:
                    _candidate = int(_raw_query)
                    if abs(_candidate) <= _SQLITE_INT64_MAX:
                        search_id = _candidate
                    else:
                        # Слишком большое число — не Telegram ID. Падать в SQLite нельзя,
                        # поэтому ищем как строку (вряд ли что-то найдёт, но интерфейс жив).
                        search_username = _raw_query
                except Exception:
                    search_username = _raw_query
            elif _raw_query.startswith('@'):
                search_username = _raw_query[1:]  # убираем @
            elif '@' in _raw_query:
                search_email = _raw_query.lower()
            else:
                search_username = _raw_query  # по username/real_username
        page = request.args.get('page', 1, type=int)
        per_page = 15
        offset = (page - 1) * per_page
        users_list_local = []
        total_users = 0
        # Фильтр по серверу
        filter_server_id = request.args.get('server_id', type=int)
        # Доп. фильтры
        expiring_3d = request.args.get('expiring_3d') in ('1', 'true', 'yes')
        empty_uuid_only = request.args.get('empty_uuid_only') in ('1', 'true', 'yes')
        min_referrals = request.args.get('min_referrals', type=int)
        paid_only = request.args.get('paid_only') in ('1', 'true', 'yes')
        new_24h_only = request.args.get('new_24h_only') in ('1', 'true', 'yes')
        filter_tag = request.args.get('filter_tag', '').strip()
        top_traffic = request.args.get('top_traffic') in ('1', 'true', 'yes')
        online_only = request.args.get('online_only') in ('1', 'true', 'yes')
        top_daily_consumption = request.args.get('top_daily_consumption') in ('1', 'true', 'yes')
        client_telegram = request.args.get('client_telegram') in ('1', 'true', 'yes')
        client_site = request.args.get('client_site') in ('1', 'true', 'yes')
        no_lk_email = request.args.get('no_lk_email') in ('1', 'true', 'yes')
        filter_limit_ip_raw = (request.args.get('filter_limit_ip') or '').strip()
        filter_limit_ip = None
        if filter_limit_ip_raw != '':
            try:
                filter_limit_ip = int(filter_limit_ip_raw)
                if filter_limit_ip < 0:
                    filter_limit_ip = None
            except ValueError:
                filter_limit_ip = None
        # Отладочный вывод для проверки filter_tag
        import logging
        import unicodedata
        logger = logging.getLogger(__name__)
        
        # Нормализуем filter_tag для сравнения (убираем zero-width joiner и другие невидимые символы)
        if filter_tag:
            # Нормализуем Unicode (NFKC - совместимая композиция)
            filter_tag_normalized = unicodedata.normalize('NFKC', filter_tag)
            # Убираем zero-width joiner и другие невидимые символы
            filter_tag_normalized = filter_tag_normalized.replace('\u200d', '').replace('\u200c', '').replace('\ufeff', '').strip()
            filter_tag = filter_tag_normalized
        
        # Вычисляем UTC время один раз для всех фильтров
        now_utc = datetime.now(timezone.utc)
        now_utc_str = now_utc.isoformat()
        in_3_days_utc_str = (now_utc + timedelta(days=3)).isoformat()
        # Время 24 часа назад в UTC для фильтра новых пользователей
        day_ago_utc = now_utc - timedelta(hours=24)
        day_ago_utc_str = day_ago_utc.isoformat()
        servers_row_for_filter = await async_query_db("SELECT value FROM settings WHERE key = 'xui_servers'", (), one=True)
        servers_for_filter = json.loads(dict(servers_row_for_filter)['value']) if servers_row_for_filter else []
        
        # Получаем список существующих тегов из БД
        tags_rows = await async_query_db("SELECT DISTINCT user_tag FROM users WHERE user_tag IS NOT NULL AND user_tag != '' ORDER BY user_tag", ())
        existing_tags = [row['user_tag'] for row in tags_rows] if tags_rows else []
        try:
            limit_rows = await async_query_db(
                "SELECT DISTINCT COALESCE(limit_ip, 0) AS lim FROM users ORDER BY lim ASC",
                (),
            )
            existing_limit_ips = [int(row['lim']) for row in (limit_rows or [])]
        except Exception:
            existing_limit_ips = []
        users_column_names = await get_table_columns_cached('users')
        has_email_col = 'email' in users_column_names

        # Нормализуем filter_tag для сравнения с тегами из БД
        # Проблема: эмодзи могут содержать zero-width joiner (\u200d), который может храниться по-разному
        if filter_tag and filter_tag != '__no_tag__':
            # Ищем точное совпадение в existing_tags
            filter_tag_matched = None
            for db_tag in existing_tags:
                if db_tag == filter_tag:
                    # Точное совпадение
                    filter_tag_matched = db_tag
                    break
                # Нормализуем оба тега для сравнения
                filter_tag_norm = unicodedata.normalize('NFKC', filter_tag).replace('\u200d', '').replace('\u200c', '').replace('\ufeff', '').strip()
                db_tag_norm = unicodedata.normalize('NFKC', db_tag).replace('\u200d', '').replace('\u200c', '').replace('\ufeff', '').strip()
                if db_tag_norm == filter_tag_norm:
                    # Совпадение после нормализации
                    filter_tag_matched = db_tag
                    break
            
            if filter_tag_matched:
                filter_tag = filter_tag_matched
            else:
                pass  # используем filter_tag как есть

        # Создание пользователя (админ) - используем grant_subscription как в боте
        if request.method == 'POST':
            form = await request.form
            if form.get('action') == 'create_user_manual':
                try:
                    new_id = int(form.get('telegram_id') or 0)
                except Exception:
                    new_id = 0
                username_in = (form.get('username') or '').strip() or None

                # Telegram ID должен влезать в SQLite INTEGER (8 байт signed).
                # Если админ опечатался и ввёл 20+ цифр, без проверки aiosqlite упадёт
                # с OverflowError ещё до запроса в БД.
                if not new_id or new_id <= 0 or abs(new_id) > (1 << 63) - 1:
                    await flash('Укажите корректный Telegram ID (целое число до 19 цифр).', 'danger')
                    return redirect(url_for('admin.users_list'))

                email_in = (form.get('email') or '').strip().lower() or None

                existing_user = await async_query_db(
                    "SELECT telegram_id, username, real_username FROM users WHERE telegram_id = ?",
                    (new_id,),
                    one=True,
                )
                if existing_user:
                    name = existing_user.get('real_username') or existing_user.get('username') or ''
                    label = f'{name} (ID {new_id})' if name else f'ID {new_id}'
                    await flash(f'Клиент с таким Telegram ID уже существует: {label}', 'danger')
                    return redirect(url_for('admin.users_list'))

                if email_in:
                    if not has_email_col:
                        await flash('Поле email недоступно в текущей схеме базы.', 'danger')
                        return redirect(url_for('admin.users_list'))
                    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email_in):
                        await flash('Неверный формат email.', 'danger')
                        return redirect(url_for('admin.users_list'))
                    existing_email = await async_query_db(
                        "SELECT telegram_id, username, real_username FROM users "
                        "WHERE LOWER(TRIM(email)) = ? LIMIT 1",
                        (email_in,),
                        one=True,
                    )
                    if existing_email:
                        other_id = existing_email.get('telegram_id')
                        other_name = (
                            existing_email.get('real_username')
                            or existing_email.get('username')
                            or f'ID {other_id}'
                        )
                        await flash(f'Email уже занят: {other_name} (ID {other_id})', 'danger')
                        return redirect(url_for('admin.users_list'))

                await app_conf.load_settings()
                remnawave_ready = (
                    app_conf.get('remnawave_enabled', False)
                    and app_conf.get('remnawave_base_url')
                    and app_conf.get('remnawave_api_token')
                )
                if not remnawave_ready:
                    await flash(
                        'Remnawave не настроен. Укажите URL и API-токен в настройках панели.',
                        'warning',
                    )
                    return redirect(url_for('admin.users_list'))

                try:
                    if not username_in:
                        username_in = f"user{new_id}"
                    if has_email_col and email_in:
                        await async_execute_db(
                            "INSERT INTO users (telegram_id, username, email, created_at) VALUES (?, ?, ?, datetime('now'))",
                            (new_id, username_in, email_in),
                        )
                    else:
                        await async_execute_db(
                            "INSERT INTO users (telegram_id, username, created_at) VALUES (?, ?, datetime('now'))",
                            (new_id, username_in),
                        )
                    
                    # Получаем параметры из формы
                    try:
                        limit_ip_val = int(form.get('limit_ip') or 0)
                    except Exception:
                        limit_ip_val = 0
                    
                    # Дни подписки или дата окончания
                    days_to_add = 30  # По умолчанию
                    expiry_date_str = form.get('expiry_date', '').strip()
                    if expiry_date_str:
                        # Если указана дата окончания, рассчитываем дни
                        try:
                            expiry_date = datetime.fromisoformat(expiry_date_str).replace(tzinfo=timezone.utc)
                            now_utc = datetime.now(timezone.utc)
                            delta = expiry_date - now_utc
                            if delta.total_seconds() > 0:
                                days_to_add = int(delta.days) + 1  # +1 чтобы включить день окончания
                            else:
                                await flash('Дата окончания должна быть в будущем.', 'warning')
                                return redirect(url_for('admin.users_list'))
                        except Exception as e:
                            await flash(f'Ошибка парсинга даты окончания: {e}', 'warning')
                            return redirect(url_for('admin.users_list'))
                    else:
                        # Используем дни из формы
                        try:
                            days_from_form = int(form.get('days_to_add') or 0)
                            if days_from_form > 0:
                                days_to_add = days_from_form
                        except Exception:
                            pass
                    
                    subscription_data = await grant_subscription(
                        user_id=new_id,
                        days_to_add=days_to_add,
                        is_trial=False,
                        limit_ip=limit_ip_val,
                        reset_traffic_on_renewal=True,
                        traffic_gb_to_add=0  # Трафик устанавливается только из настроек
                    )
                    
                    if subscription_data:
                        expiry_date = subscription_data.get('expiry_date')

                        expiry_str = ''
                        if expiry_date:
                            moscow = pytz.timezone('Europe/Moscow')
                            local_expiry = expiry_date.astimezone(moscow)
                            expiry_str = local_expiry.strftime('%d.%m.%Y %H:%M')

                        toast_msg = f'✅ Пользователь {new_id} создан (Remnawave)'
                        if email_in:
                            toast_msg += f' | {email_in}'
                        if expiry_str:
                            toast_msg += f' | до {expiry_str}'
                        await flash(toast_msg, 'toast')
                    else:
                        await flash(f'Не удалось создать подписку для {new_id}. Проверьте логи.', 'danger')
                        return redirect(url_for('admin.users_list'))

                    return redirect(url_for('admin.users_list'))
                except Exception as e:
                    import traceback
                    logger.error(f"Ошибка создания пользователя {new_id}: {e}\n{traceback.format_exc()}")
                    await flash(f'Не удалось создать пользователя: {e}', 'danger')
                    return redirect(url_for('admin.users_list'))

        # Поиск по Telegram ID
        if search_id:
            column_names = await get_table_columns_cached('users')
            has_is_blocked = 'is_blocked' in column_names
            has_is_active = 'is_active' in column_names
            has_created_at = 'created_at' in column_names
            rw_plain = _users_rw_traffic_cols_plain(column_names)

            if has_is_blocked and has_is_active:
                user = await async_query_db(f"SELECT telegram_id, username, subscription_end_date, is_trial_used, current_server_id, xui_client_email, xui_client_uuid, COALESCE(limit_ip,0) as limit_ip, COALESCE(is_blocked, 0) as is_blocked, COALESCE(is_active, 0) as is_active, user_tag, created_at, COALESCE(registration_type,'') as registration_type{rw_plain} FROM users WHERE telegram_id = ? AND COALESCE(is_blocked, 0) = 0", (search_id,), one=True)
            elif has_is_blocked and not has_is_active:
                # Используем UTC время для корректного сравнения
                now_utc_str = datetime.now(timezone.utc).isoformat()
                user = await async_query_db(f"SELECT telegram_id, username, subscription_end_date, is_trial_used, current_server_id, xui_client_email, xui_client_uuid, COALESCE(limit_ip,0) as limit_ip, COALESCE(is_blocked, 0) as is_blocked, CASE WHEN subscription_end_date IS NOT NULL AND subscription_end_date > ? AND COALESCE(is_blocked,0)=0 THEN 1 ELSE 0 END as is_active, user_tag, created_at, COALESCE(registration_type,'') as registration_type{rw_plain} FROM users WHERE telegram_id = ? AND COALESCE(is_blocked, 0) = 0", (now_utc_str, search_id), one=True)
            else:
                # Используем UTC время для корректного сравнения
                if 'now_utc_str' not in locals():
                    now_utc_str = datetime.now(timezone.utc).isoformat()
                user = await async_query_db(f"SELECT telegram_id, username, subscription_end_date, is_trial_used, current_server_id, xui_client_email, xui_client_uuid, COALESCE(limit_ip,0) as limit_ip, 0 as is_blocked, CASE WHEN subscription_end_date IS NOT NULL AND subscription_end_date > ? THEN 1 ELSE 0 END as is_active, user_tag, created_at, COALESCE(registration_type,'') as registration_type{rw_plain} FROM users WHERE telegram_id = ?", (now_utc_str, search_id), one=True)

            if user:
                user = dict(user)
                if user['subscription_end_date']:
                    try:
                        dt = datetime.fromisoformat(user['subscription_end_date'])
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        else:
                            dt = dt.astimezone(timezone.utc)
                        user['subscription_end_date'] = dt
                    except Exception:
                        user['subscription_end_date'] = None
                # Вычисляем количество дней с момента регистрации для найденного пользователя
                created_at_val = user.get('created_at')
                if created_at_val and str(created_at_val).strip():
                    try:
                        created_dt = datetime.fromisoformat(str(created_at_val).replace('Z', '+00:00'))
                        if created_dt.tzinfo is None:
                            created_dt = created_dt.replace(tzinfo=timezone.utc)
                        else:
                            created_dt = created_dt.astimezone(timezone.utc)
                        days_with_us = (datetime.now(timezone.utc) - created_dt).days
                        user['days_with_us'] = max(0, days_with_us)  # Не показываем отрицательные значения
                    except Exception as e:
                        user['days_with_us'] = None
                else:
                    user['days_with_us'] = None
                
                users_list_local = [user]
                total_users = 1
            else:
                users_list_local = []
                total_users = 0
            total_pages = 1

        elif search_email or search_username:
            # Поиск по email, username или real_username — с пагинацией
            column_names = await get_table_columns_cached('users')
            rw_plain = _users_rw_traffic_cols_plain(column_names)
            now_utc_str = datetime.now(timezone.utc).isoformat()
            if search_email:
                where_clause = "WHERE LOWER(email) = ?"
                where_params = (search_email.lower(),)
                count_params = (search_email.lower(),)
                order_clause = ""
            else:
                like = f"%{search_username}%"
                where_clause = "WHERE LOWER(real_username) LIKE LOWER(?) OR LOWER(username) LIKE LOWER(?)"
                where_params = (like, like)
                count_params = (like, like)
                order_clause = "ORDER BY created_at DESC"

            count_row = await async_query_db(
                f"SELECT COUNT(*) as cnt FROM users {where_clause}",
                count_params, one=True
            )
            total_users = count_row['cnt'] if count_row else 0
            total_pages = max(1, (total_users + per_page - 1) // per_page)
            page = max(1, min(page, total_pages))
            offset = (page - 1) * per_page

            sql = f"""
                SELECT telegram_id, username, real_username, email, subscription_end_date, is_trial_used,
                       current_server_id, xui_client_email, xui_client_uuid,
                       COALESCE(limit_ip,0) as limit_ip,
                       COALESCE(is_blocked,0) as is_blocked,
                       CASE WHEN subscription_end_date IS NOT NULL AND subscription_end_date > ? THEN 1 ELSE 0 END as is_active,
                       user_tag, created_at, registration_type{rw_plain}
                FROM users
                {where_clause}
                {order_clause}
                LIMIT ? OFFSET ?
            """
            found = await async_query_db(sql, (now_utc_str, *where_params, per_page, offset))

            users_list_local = []
            for u in (found or []):
                u = dict(u)
                if u.get('subscription_end_date'):
                    try:
                        dt = datetime.fromisoformat(u['subscription_end_date'])
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        u['subscription_end_date'] = dt
                    except Exception:
                        u['subscription_end_date'] = None
                if u.get('created_at'):
                    try:
                        cd = datetime.fromisoformat(u['created_at'])
                        if cd.tzinfo is None:
                            cd = cd.replace(tzinfo=timezone.utc)
                        u['days_with_us'] = (datetime.now(timezone.utc) - cd).days
                    except Exception:
                        u['days_with_us'] = None
                else:
                    u['days_with_us'] = None
                users_list_local.append(u)

        else:
            # Общий список с фильтрами
            column_names = await get_table_columns_cached('users')
            has_is_blocked = 'is_blocked' in column_names
            has_is_active = 'is_active' in column_names
            has_created_at = 'created_at' in column_names
            has_email_col = 'email' in column_names
            rw_traffic_sel = _users_rw_traffic_select(column_names)
            rw_traffic_grp = _users_rw_traffic_groupby(column_names)

            # Если включен фильтр "Топ по трафику", "Онлайн клиенты" или "Топ за сутки", получаем всех пользователей без пагинации
            # для последующей сортировки/фильтрации
            use_traffic_sort = top_traffic
            use_online_filter = online_only
            use_daily_sort = top_daily_consumption
            
            # Формируем ORDER BY: сначала новые за 24ч (если есть created_at), затем по сроку
            # Если сортировка по трафику, ORDER BY будет применен после получения трафика
            if has_created_at:
                # Используем UTC время для корректного сравнения
                order_by_sql = (
                    f"CASE WHEN u.created_at IS NOT NULL AND datetime(u.created_at, 'utc') >= datetime(?, 'utc') THEN 0 ELSE 1 END, "
                    "CASE WHEN u.subscription_end_date IS NULL THEN 1 ELSE 0 END, "
                    "u.subscription_end_date DESC, "
                    "u.telegram_id DESC"
                )
                order_by_sql = "".join(order_by_sql)
            else:
                order_by_sql = (
                    "CASE WHEN u.subscription_end_date IS NULL THEN 1 ELSE 0 END, "
                    "u.subscription_end_date DESC, "
                    "u.telegram_id DESC"
                )
                order_by_sql = "".join(order_by_sql)

            if has_is_blocked and has_is_active:
                # Отбор по учётке в панели: обычный список показывает всех, отдельный фильтр — только пустые UUID
                if empty_uuid_only:
                    uuid_filter = ""  # Показываем только пустые UUID через where_parts
                else:
                    # Клиента, у которого ещё нет учётки в панели, не прячем: у роутеров
                    # человек появляется с первого /start и задолго до подписки,
                    # а оператору его искать — чтобы привязать роутер к заказу.
                    # Отбор «только пустые UUID» остался отдельным фильтром.
                    uuid_filter = ""
                
                base_sql = (
                    f"""
                    SELECT u.telegram_id, u.username, u.subscription_end_date, u.is_trial_used, u.current_server_id,
                           u.xui_client_email as xui_client_email, COALESCE(u.limit_ip,0) as limit_ip,
                           COALESCE(u.is_blocked, 0) as is_blocked, COALESCE(u.is_active, 0) as is_active, COUNT(r.telegram_id) as referrals_count,
                           u.user_tag, u.created_at, COALESCE(u.remnawave_username, '') as remnawave_username,
                           COALESCE(u.subscription_provider, 'x-ui') as subscription_provider,
                           COALESCE(u.registration_type, '') as registration_type{rw_traffic_sel}
                    FROM users u
                    LEFT JOIN users r ON u.telegram_id = r.invited_by
                    WHERE COALESCE(u.is_blocked, 0) = 0 {{server_filter}} {{uuid_filter}}
                    GROUP BY u.telegram_id, u.username, u.subscription_end_date, u.is_trial_used, u.current_server_id, u.xui_client_email, u.limit_ip, u.is_blocked, u.is_active, u.user_tag, u.created_at, u.remnawave_username, u.subscription_provider, u.registration_type{rw_traffic_grp}
                    {{having_clause}}
                    ORDER BY {{order_by_sql}}
                    {{limit_clause}}
                    """
                )
                where_parts = []
                params: list = []
                if filter_server_id:
                    where_parts.append("AND u.current_server_id = ?")
                    params.append(filter_server_id)
                if expiring_3d:
                    # Используем UTC время для корректного сравнения с ISO датами
                    where_parts.append("AND u.subscription_end_date IS NOT NULL AND u.subscription_end_date <= ? AND u.subscription_end_date > ?")
                    params.extend([in_3_days_utc_str, now_utc_str])
                if empty_uuid_only:
                    where_parts.append("AND (u.xui_client_uuid IS NULL OR u.xui_client_uuid = '')")
                if paid_only:
                    where_parts.append("AND EXISTS (SELECT 1 FROM payments p WHERE p.telegram_id = u.telegram_id AND p.status = 'succeeded')")
                if new_24h_only:
                    # Используем UTC время для корректного сравнения
                    where_parts.append("AND u.created_at IS NOT NULL AND datetime(u.created_at, 'utc') >= datetime(?, 'utc')")
                    params.append(day_ago_utc_str)
                if filter_tag:
                    if filter_tag == '__no_tag__':
                        where_parts.append("AND (u.user_tag IS NULL OR u.user_tag = '')")
                    else:
                        where_parts.append("AND u.user_tag = ?")
                        params.append(filter_tag)
                if client_telegram:
                    where_parts.append("AND (u.registration_type = 'telegram' OR u.registration_type IS NULL OR u.registration_type = '')")
                if client_site:
                    where_parts.append("AND u.registration_type = 'site'")
                _users_list_apply_limit_email_filters(
                    where_parts, params, and_prefix=True,
                    filter_limit_ip=filter_limit_ip, no_lk_email=no_lk_email, has_email_col=has_email_col,
                )
                having_sql = " HAVING COUNT(r.telegram_id) >= ?" if (min_referrals is not None and min_referrals > 0) else ""
                if min_referrals is not None and min_referrals > 0:
                    params.append(min_referrals)
                
                # Формируем limit_clause в зависимости от фильтров top_traffic, online_only или top_daily_consumption
                if use_traffic_sort or use_online_filter or use_daily_sort:
                    limit_clause = ""  # Без пагинации - получим всех пользователей для сортировки/фильтрации
                    limit_params = []
                else:
                    limit_clause = "LIMIT ? OFFSET ?"
                    limit_params = [per_page, offset]
                
                # Добавляем параметры для CASE выражений в SELECT и ORDER BY
                # В этой ветке (has_is_blocked and has_is_active) в SELECT НЕТ CASE с параметром, только в ORDER BY
                # Проверяем, есть ли CASE с параметром в SELECT части base_sql (до ORDER BY)
                select_part = base_sql.split('ORDER BY')[0] if 'ORDER BY' in base_sql else base_sql
                has_case_in_select = '?' in select_part and 'CASE' in select_part
                
                # Формируем final_params в правильном порядке
                # В этой ветке (has_is_blocked and has_is_active) в SELECT НЕТ CASE с параметром
                # Порядок параметров в SQL: WHERE (params), ORDER BY (day_ago_utc_str), LIMIT (per_page), OFFSET (offset)
                # В SQL запросе: WHERE ... u.user_tag = ? (первый ?), ORDER BY ... datetime(?, 'utc') (второй ?), LIMIT ? (третий ?), OFFSET ? (четвертый ?)
                if has_created_at and 'datetime(?, \'utc\')' in order_by_sql:
                    # Есть ORDER BY с параметром для 24 часов
                    # Порядок: params (WHERE), day_ago_utc_str (ORDER BY), limit_params (LIMIT/OFFSET или пусто)
                    final_params = params + [day_ago_utc_str] + limit_params
                else:
                    # Нет ORDER BY с параметром
                    final_params = params + limit_params
                
                server_filter_str = " ".join(where_parts) if where_parts else ""
                # Формируем финальный SQL для отладки
                final_sql = base_sql.format(server_filter=server_filter_str, uuid_filter=uuid_filter, having_clause=having_sql, order_by_sql=order_by_sql, limit_clause=limit_clause)
                users = await async_query_db(final_sql, tuple(final_params))
            elif has_is_blocked and not has_is_active:
                # Отбор по учётке в панели: обычный список показывает всех, отдельный фильтр — только пустые UUID
                if empty_uuid_only:
                    uuid_filter = ""  # Показываем только пустые UUID через where_parts
                else:
                    # Клиента, у которого ещё нет учётки в панели, не прячем: у роутеров
                    # человек появляется с первого /start и задолго до подписки,
                    # а оператору его искать — чтобы привязать роутер к заказу.
                    # Отбор «только пустые UUID» остался отдельным фильтром.
                    uuid_filter = ""
                
                base_sql = (
                    f"""
                    SELECT u.telegram_id, u.username, u.subscription_end_date, u.is_trial_used, u.current_server_id,
                           u.xui_client_email as xui_client_email,
                           COALESCE(u.is_blocked, 0) as is_blocked,
                           CASE WHEN u.subscription_end_date IS NOT NULL AND u.subscription_end_date > ? AND COALESCE(u.is_blocked,0)=0 THEN 1 ELSE 0 END as is_active,
                           COUNT(r.telegram_id) as referrals_count,
                           COALESCE(u.limit_ip,0) as limit_ip,
                           u.user_tag, u.created_at, COALESCE(u.remnawave_username, '') as remnawave_username,
                           COALESCE(u.subscription_provider, 'x-ui') as subscription_provider,
                           COALESCE(u.registration_type, '') as registration_type{rw_traffic_sel}
                    FROM users u
                    LEFT JOIN users r ON u.telegram_id = r.invited_by
                    WHERE COALESCE(u.is_blocked, 0) = 0 {{server_filter}} {{uuid_filter}}
                    GROUP BY u.telegram_id, u.username, u.subscription_end_date, u.is_trial_used, u.current_server_id, u.xui_client_email, u.limit_ip, u.is_blocked, u.user_tag, u.created_at, u.remnawave_username, u.subscription_provider, u.registration_type{rw_traffic_grp}
                    {{having_clause}}
                    ORDER BY {{order_by_sql}}
                    {{limit_clause}}
                    """
                )
                where_parts = []
                params = []
                if filter_server_id:
                    where_parts.append("AND u.current_server_id = ?")
                    params.append(filter_server_id)
                if expiring_3d:
                    # Используем UTC время для корректного сравнения с ISO датами
                    where_parts.append("AND u.subscription_end_date IS NOT NULL AND u.subscription_end_date <= ? AND u.subscription_end_date > ?")
                    params.extend([in_3_days_utc_str, now_utc_str])
                if empty_uuid_only:
                    where_parts.append("AND (u.xui_client_uuid IS NULL OR u.xui_client_uuid = '')")
                if paid_only:
                    where_parts.append("AND EXISTS (SELECT 1 FROM payments p WHERE p.telegram_id = u.telegram_id AND p.status = 'succeeded')")
                if new_24h_only:
                    # Используем UTC время для корректного сравнения
                    where_parts.append("AND u.created_at IS NOT NULL AND datetime(u.created_at, 'utc') >= datetime(?, 'utc')")
                    params.append(day_ago_utc_str)
                if filter_tag:
                    if filter_tag == '__no_tag__':
                        where_parts.append("AND (u.user_tag IS NULL OR u.user_tag = '')")
                    else:
                        where_parts.append("AND u.user_tag = ?")
                        params.append(filter_tag)
                if client_telegram:
                    where_parts.append("AND (u.registration_type = 'telegram' OR u.registration_type IS NULL OR u.registration_type = '')")
                if client_site:
                    where_parts.append("AND u.registration_type = 'site'")
                _users_list_apply_limit_email_filters(
                    where_parts, params, and_prefix=True,
                    filter_limit_ip=filter_limit_ip, no_lk_email=no_lk_email, has_email_col=has_email_col,
                )
                having_sql = " HAVING COUNT(r.telegram_id) >= ?" if (min_referrals is not None and min_referrals > 0) else ""
                if min_referrals is not None and min_referrals > 0:
                    params.append(min_referrals)
                
                # Формируем limit_clause в зависимости от фильтров top_traffic, online_only или top_daily_consumption
                if use_traffic_sort or use_online_filter or use_daily_sort:
                    limit_clause = ""  # Без пагинации - получим всех пользователей для сортировки/фильтрации
                    limit_params = []
                else:
                    limit_clause = "LIMIT ? OFFSET ?"
                    limit_params = [per_page, offset]
                
                # Добавляем параметры для CASE выражений в SELECT и ORDER BY
                # Порядок: now_utc_str (для SELECT CASE), day_ago_utc_str (для ORDER BY CASE), остальные params
                if has_created_at and 'datetime(?, \'utc\')' in order_by_sql:
                    # Есть ORDER BY с параметром для 24 часов
                    if '?' in base_sql and 'CASE' in base_sql:
                        # Есть CASE в SELECT (now_utc_str) и ORDER BY (day_ago_utc_str)
                        final_params = [now_utc_str, day_ago_utc_str] + params + limit_params
                    else:
                        # Только ORDER BY с параметром
                        final_params = [day_ago_utc_str] + params + limit_params
                elif '?' in base_sql and 'CASE' in base_sql:
                    # Только CASE в SELECT
                    final_params = [now_utc_str] + params + limit_params
                else:
                    final_params = params + limit_params
                # Проверяем, нужен ли параметр для ORDER BY
                if has_created_at and 'datetime(?, \'utc\')' in order_by_sql and day_ago_utc_str not in final_params:
                    # Добавляем day_ago_utc_str в начало, если его еще нет
                    final_params = [day_ago_utc_str] + final_params
                users = await async_query_db(base_sql.format(server_filter=" ".join(where_parts), uuid_filter=uuid_filter, having_clause=having_sql, order_by_sql=order_by_sql, limit_clause=limit_clause), tuple(final_params))
            else:
                # Отбор по учётке в панели: обычный список показывает всех, отдельный фильтр — только пустые UUID
                if empty_uuid_only:
                    uuid_filter = ""  # Показываем только пустые UUID через where_parts
                else:
                    uuid_filter = ""  # Для where_clause добавляем в where_parts
                
                base_sql = (
                    f"""
                    SELECT u.telegram_id, u.username, u.subscription_end_date, u.is_trial_used, u.current_server_id,
                           u.xui_client_email as xui_client_email,
                           0 as is_blocked,
                           CASE WHEN u.subscription_end_date IS NOT NULL AND u.subscription_end_date > ? THEN 1 ELSE 0 END as is_active,
                           COUNT(r.telegram_id) as referrals_count,
                           COALESCE(u.limit_ip,0) as limit_ip,
                           u.user_tag, u.created_at, COALESCE(u.remnawave_username, '') as remnawave_username,
                           COALESCE(u.subscription_provider, 'x-ui') as subscription_provider{rw_traffic_sel}
                    FROM users u
                    LEFT JOIN users r ON u.telegram_id = r.invited_by
                    {{where_clause}}
                    GROUP BY u.telegram_id, u.username, u.subscription_end_date, u.is_trial_used, u.current_server_id, u.xui_client_email, u.limit_ip, u.user_tag, u.created_at, u.remnawave_username, u.subscription_provider{rw_traffic_grp}
                    {{having_clause}}
                    ORDER BY {{order_by_sql}}
                    {{limit_clause}}
                    """
                )
                where_parts = []
                params = []
                # Клиента без учётки в панели не прячем: у роутеров человек
                # появляется с первого /start и задолго до подписки, а искать его
                # оператору — чтобы привязать роутер к заказу перед отгрузкой.
                # Отбор «только пустые UUID» остался отдельным фильтром ниже.
                if filter_server_id:
                    where_parts.append("u.current_server_id = ?")
                    params.append(filter_server_id)
                if expiring_3d:
                    # Используем UTC время для корректного сравнения с ISO датами
                    where_parts.append("u.subscription_end_date IS NOT NULL AND u.subscription_end_date <= ? AND u.subscription_end_date > ?")
                    params.extend([in_3_days_utc_str, now_utc_str])
                if empty_uuid_only:
                    where_parts.append("(u.xui_client_uuid IS NULL OR u.xui_client_uuid = '')")
                if paid_only:
                    where_parts.append("EXISTS (SELECT 1 FROM payments p WHERE p.telegram_id = u.telegram_id AND p.status = 'succeeded')")
                if new_24h_only:
                    # Используем UTC время для корректного сравнения
                    where_parts.append("u.created_at IS NOT NULL AND datetime(u.created_at, 'utc') >= datetime(?, 'utc')")
                    params.append(day_ago_utc_str)
                if filter_tag:
                    if filter_tag == '__no_tag__':
                        where_parts.append("(u.user_tag IS NULL OR u.user_tag = '')")
                    else:
                        where_parts.append("u.user_tag = ?")
                        params.append(filter_tag)
                if client_telegram:
                    where_parts.append("(u.registration_type = 'telegram' OR u.registration_type IS NULL OR u.registration_type = '')")
                if client_site:
                    where_parts.append("u.registration_type = 'site'")
                _users_list_apply_limit_email_filters(
                    where_parts, params, and_prefix=False,
                    filter_limit_ip=filter_limit_ip, no_lk_email=no_lk_email, has_email_col=has_email_col,
                )
                where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
                having_sql = " HAVING COUNT(r.telegram_id) >= ?" if (min_referrals is not None and min_referrals > 0) else ""
                if min_referrals is not None and min_referrals > 0:
                    params.append(min_referrals)
                
                # Формируем limit_clause в зависимости от фильтров top_traffic, online_only или top_daily_consumption
                if use_traffic_sort or use_online_filter or use_daily_sort:
                    limit_clause = ""  # Без пагинации - получим всех пользователей для сортировки/фильтрации
                    limit_params = []
                else:
                    limit_clause = "LIMIT ? OFFSET ?"
                    limit_params = [per_page, offset]
                
                # Добавляем параметры для CASE выражений в SELECT и ORDER BY
                # Порядок: now_utc_str (для SELECT CASE), day_ago_utc_str (для ORDER BY CASE), остальные params
                if has_created_at and 'datetime(?, \'utc\')' in order_by_sql:
                    # Есть ORDER BY с параметром для 24 часов
                    if '?' in base_sql and 'CASE' in base_sql:
                        # Есть CASE в SELECT (now_utc_str) и ORDER BY (day_ago_utc_str)
                        final_params = [now_utc_str, day_ago_utc_str] + params + limit_params
                    else:
                        # Только ORDER BY с параметром
                        final_params = [day_ago_utc_str] + params + limit_params
                elif '?' in base_sql and 'CASE' in base_sql:
                    # Только CASE в SELECT
                    final_params = [now_utc_str] + params + limit_params
                else:
                    final_params = params + limit_params
                # Проверяем, нужен ли параметр для ORDER BY
                if has_created_at and 'datetime(?, \'utc\')' in order_by_sql and day_ago_utc_str not in final_params:
                    # Добавляем day_ago_utc_str в начало, если его еще нет
                    final_params = [day_ago_utc_str] + final_params
                users = await async_query_db(base_sql.format(where_clause=where_sql, having_clause=having_sql, order_by_sql=order_by_sql, limit_clause=limit_clause), tuple(final_params))


            for user in users:
                user = dict(user)
                if user['subscription_end_date']:
                    try:
                        dt = datetime.fromisoformat(user['subscription_end_date'])
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        else:
                            dt = dt.astimezone(timezone.utc)
                        user['subscription_end_date'] = dt
                    except Exception:
                        user['subscription_end_date'] = None
                # Вычисляем количество дней с момента регистрации
                if user.get('created_at'):
                    try:
                        created_dt = datetime.fromisoformat(user['created_at'])
                        if created_dt.tzinfo is None:
                            created_dt = created_dt.replace(tzinfo=timezone.utc)
                        else:
                            created_dt = created_dt.astimezone(timezone.utc)
                        days_with_us = (datetime.now(timezone.utc) - created_dt).days
                        user['days_with_us'] = days_with_us
                    except Exception:
                        user['days_with_us'] = None
                else:
                    user['days_with_us'] = None
                users_list_local.append(user)

            # Подсчёт total с теми же условиями через подзапрос.
            # Если включены top_traffic / online_only / top_daily_consumption — пагинация
            # делается в Python после загрузки analytics, и total_users всё равно
            # перетирается (`total_users = len(users_list_local)`). Запрос COUNT(*)
            # в этом случае бессмысленен и стоит дорого — пропускаем его.
            async def count_with(sql_no_limit: str, params_no_limit: tuple) -> int:
                row = await async_query_db(f"SELECT COUNT(*) as c FROM ({sql_no_limit}) t", params_no_limit, one=True)
                return row['c'] if row and 'c' in row.keys() else 0

            skip_count = use_traffic_sort or use_online_filter or use_daily_sort
            if skip_count:
                total_users = len(users_list_local)
            elif has_is_blocked and has_is_active:
                # То же для счётчика: список и его число должны считаться по одному правилу
                if empty_uuid_only:
                    uuid_filter_count = ""  # Показываем только пустые UUID через where_parts
                else:
                    # Клиента, у которого ещё нет учётки в панели, не прячем: у роутеров
                    # человек появляется с первого /start и задолго до подписки,
                    # а оператору его искать — чтобы привязать роутер к заказу.
                    # Отбор «только пустые UUID» остался отдельным фильтром.
                    uuid_filter_count = ""
                
                count_sql = (
                    """
                    SELECT u.telegram_id
                    FROM users u
                    LEFT JOIN users r ON u.telegram_id = r.invited_by
                    WHERE COALESCE(u.is_blocked, 0) = 0 {server_filter} {uuid_filter}
                    GROUP BY u.telegram_id, u.username, u.subscription_end_date, u.is_trial_used, u.current_server_id, u.is_blocked, u.is_active
                    {having_clause}
                    """
                )
                where_parts = []
                params = []
                if filter_server_id:
                    where_parts.append("AND u.current_server_id = ?")
                    params.append(filter_server_id)
                if expiring_3d:
                    # Используем UTC время для корректного сравнения с ISO датами
                    where_parts.append("AND u.subscription_end_date IS NOT NULL AND u.subscription_end_date <= ? AND u.subscription_end_date > ?")
                    params.extend([in_3_days_utc_str, now_utc_str])
                if empty_uuid_only:
                    where_parts.append("AND (u.xui_client_uuid IS NULL OR u.xui_client_uuid = '')")
                if paid_only:
                    where_parts.append("AND EXISTS (SELECT 1 FROM payments p WHERE p.telegram_id = u.telegram_id AND p.status = 'succeeded')")
                if new_24h_only:
                    # Используем UTC время для корректного сравнения
                    where_parts.append("AND u.created_at IS NOT NULL AND datetime(u.created_at, 'utc') >= datetime(?, 'utc')")
                    params.append(day_ago_utc_str)
                if filter_tag:
                    if filter_tag == '__no_tag__':
                        where_parts.append("AND (u.user_tag IS NULL OR u.user_tag = '')")
                    else:
                        where_parts.append("AND u.user_tag = ?")
                        params.append(filter_tag)
                if client_telegram:
                    where_parts.append("AND (u.registration_type = 'telegram' OR u.registration_type IS NULL OR u.registration_type = '')")
                if client_site:
                    where_parts.append("AND u.registration_type = 'site'")
                _users_list_apply_limit_email_filters(
                    where_parts, params, and_prefix=True,
                    filter_limit_ip=filter_limit_ip, no_lk_email=no_lk_email, has_email_col=has_email_col,
                )
                having_sql = " HAVING COUNT(r.telegram_id) >= ?" if (min_referrals is not None and min_referrals > 0) else ""
                if min_referrals is not None and min_referrals > 0:
                    params.append(min_referrals)
                total_users = await count_with(count_sql.format(server_filter=" ".join(where_parts), uuid_filter=uuid_filter_count, having_clause=having_sql), tuple(params))
            elif has_is_blocked and not has_is_active:
                # То же для счётчика: список и его число должны считаться по одному правилу
                if empty_uuid_only:
                    uuid_filter_count = ""  # Показываем только пустые UUID через where_parts
                else:
                    # Клиента, у которого ещё нет учётки в панели, не прячем: у роутеров
                    # человек появляется с первого /start и задолго до подписки,
                    # а оператору его искать — чтобы привязать роутер к заказу.
                    # Отбор «только пустые UUID» остался отдельным фильтром.
                    uuid_filter_count = ""
                
                count_sql = (
                    """
                    SELECT u.telegram_id
                    FROM users u
                    LEFT JOIN users r ON u.telegram_id = r.invited_by
                    WHERE COALESCE(u.is_blocked, 0) = 0 {server_filter} {uuid_filter}
                    GROUP BY u.telegram_id, u.username, u.subscription_end_date, u.is_trial_used, u.current_server_id, u.is_blocked
                    {having_clause}
                    """
                )
                where_parts = []
                params = []
                if filter_server_id:
                    where_parts.append("AND u.current_server_id = ?")
                    params.append(filter_server_id)
                if expiring_3d:
                    # Используем UTC время для корректного сравнения с ISO датами
                    where_parts.append("AND u.subscription_end_date IS NOT NULL AND u.subscription_end_date <= ? AND u.subscription_end_date > ?")
                    params.extend([in_3_days_utc_str, now_utc_str])
                if empty_uuid_only:
                    where_parts.append("AND (u.xui_client_uuid IS NULL OR u.xui_client_uuid = '')")
                if paid_only:
                    where_parts.append("AND EXISTS (SELECT 1 FROM payments p WHERE p.telegram_id = u.telegram_id AND p.status = 'succeeded')")
                if new_24h_only:
                    # Используем UTC время для корректного сравнения
                    where_parts.append("AND u.created_at IS NOT NULL AND datetime(u.created_at, 'utc') >= datetime(?, 'utc')")
                    params.append(day_ago_utc_str)
                if filter_tag:
                    if filter_tag == '__no_tag__':
                        where_parts.append("AND (u.user_tag IS NULL OR u.user_tag = '')")
                    else:
                        where_parts.append("AND u.user_tag = ?")
                        params.append(filter_tag)
                if client_telegram:
                    where_parts.append("AND (u.registration_type = 'telegram' OR u.registration_type IS NULL OR u.registration_type = '')")
                if client_site:
                    where_parts.append("AND u.registration_type = 'site'")
                _users_list_apply_limit_email_filters(
                    where_parts, params, and_prefix=True,
                    filter_limit_ip=filter_limit_ip, no_lk_email=no_lk_email, has_email_col=has_email_col,
                )
                having_sql = " HAVING COUNT(r.telegram_id) >= ?" if (min_referrals is not None and min_referrals > 0) else ""
                if min_referrals is not None and min_referrals > 0:
                    params.append(min_referrals)
                total_users = await count_with(count_sql.format(server_filter=" ".join(where_parts), uuid_filter=uuid_filter_count, having_clause=having_sql), tuple(params))
            else:
                count_sql = (
                    """
                    SELECT u.telegram_id
                    FROM users u
                    LEFT JOIN users r ON u.telegram_id = r.invited_by
                    {where_clause}
                    GROUP BY u.telegram_id, u.username, u.subscription_end_date, u.is_trial_used, u.current_server_id
                    {having_clause}
                    """
                )
                where_parts = []
                params = []
                # Клиента без учётки в панели не прячем: у роутеров человек
                # появляется с первого /start и задолго до подписки, а искать его
                # оператору — чтобы привязать роутер к заказу перед отгрузкой.
                # Отбор «только пустые UUID» остался отдельным фильтром ниже.
                if filter_server_id:
                    where_parts.append("u.current_server_id = ?")
                    params.append(filter_server_id)
                if expiring_3d:
                    # Используем UTC время для корректного сравнения с ISO датами
                    where_parts.append("u.subscription_end_date IS NOT NULL AND u.subscription_end_date <= ? AND u.subscription_end_date > ?")
                    params.extend([in_3_days_utc_str, now_utc_str])
                if empty_uuid_only:
                    where_parts.append("(u.xui_client_uuid IS NULL OR u.xui_client_uuid = '')")
                if paid_only:
                    where_parts.append("EXISTS (SELECT 1 FROM payments p WHERE p.telegram_id = u.telegram_id AND p.status = 'succeeded')")
                if new_24h_only:
                    # Используем UTC время для корректного сравнения
                    where_parts.append("u.created_at IS NOT NULL AND datetime(u.created_at, 'utc') >= datetime(?, 'utc')")
                    params.append(day_ago_utc_str)
                if filter_tag:
                    if filter_tag == '__no_tag__':
                        where_parts.append("(u.user_tag IS NULL OR u.user_tag = '')")
                    else:
                        where_parts.append("u.user_tag = ?")
                        params.append(filter_tag)
                if client_telegram:
                    where_parts.append("(u.registration_type = 'telegram' OR u.registration_type IS NULL OR u.registration_type = '')")
                if client_site:
                    where_parts.append("u.registration_type = 'site'")
                _users_list_apply_limit_email_filters(
                    where_parts, params, and_prefix=False,
                    filter_limit_ip=filter_limit_ip, no_lk_email=no_lk_email, has_email_col=has_email_col,
                )
                where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
                having_sql = " HAVING COUNT(r.telegram_id) >= ?" if (min_referrals is not None and min_referrals > 0) else ""
                if min_referrals is not None and min_referrals > 0:
                    params.append(min_referrals)
                total_users = await count_with(count_sql.format(where_clause=where_sql, having_clause=having_sql), tuple(params))
            total_pages = (total_users + per_page - 1) // per_page

        now = datetime.now(timezone.utc)
        # Шаблоны новостей вместе с полями кастомной кнопки и медиа
        try:
            news_templates = await async_query_db(
                "SELECT id, title, body, "
                "COALESCE(custom_btn_text,'') as custom_btn_text, "
                "COALESCE(custom_btn_url,'') as custom_btn_url, "
                "COALESCE(media_kind,'') as media_kind, "
                "COALESCE(media_file_id,'') as media_file_id, "
                "COALESCE(media_local_path,'') as media_local_path "
                "FROM news_templates ORDER BY id DESC"
            )
        except Exception:
            try:
                news_templates = await async_query_db(
                    "SELECT id, title, body, "
                    "COALESCE(custom_btn_text,'') as custom_btn_text, "
                    "COALESCE(custom_btn_url,'') as custom_btn_url "
                    "FROM news_templates ORDER BY id DESC"
                )
            except Exception:
                news_templates = await async_query_db("SELECT id, title, body FROM news_templates ORDER BY id DESC")
        # === Трафик и онлайн из таблицы users (синхронизация Remnawave) ===
        traffic_source = 'remnawave'
        try:
            _enrich_users_list_traffic_online(users_list_local)

            if online_only:
                before = len(users_list_local)
                users_list_local = [
                    u for u in users_list_local
                    if u.get('online_count', 0) > 0
                ]
                logger.info(f"[ONLINE] online_only ({traffic_source}): {len(users_list_local)}/{before}")

            if top_traffic:
                users_list_local.sort(key=lambda u: u.get('total_traffic', 0), reverse=True)
            if top_daily_consumption:
                users_list_local.sort(key=lambda u: u.get('daily_consumption', 0), reverse=True)

            if top_traffic or online_only or top_daily_consumption:
                total_users = len(users_list_local)
                total_pages = (total_users + per_page - 1) // per_page if total_users > 0 else 1
                users_list_local = users_list_local[offset:offset + per_page]
                logger.info(
                    f"[FILTERS] post-filter slice: {len(users_list_local)}/{total_users} (page {page})"
                )
        except Exception as e:
            logger.error(f"[ANALYTICS] fatal error: {e}", exc_info=True)
            for user in users_list_local:
                user.setdefault('total_traffic', 0)
                user.setdefault('total_traffic_mb', 0)
                user.setdefault('daily_consumption', 0)
                user.setdefault('daily_consumption_mb', 0)
                user.setdefault('online_count', 0)
                user.setdefault('online_label', '—')
                user.setdefault('online_is_live', False)
                user.setdefault('online_servers', [])
                user.setdefault('online_has_data', False)
                user.setdefault('online_exceeds_limit', False)
        
        # Старая переменная для совместимости (не используется в шаблоне)
        traffic_totals_mb: dict[int, float] = {}

        # Загружаем названия кнопок из настроек для компактных кнопок в форме новостей
        btn_renew_sub = '🔁 Продлить'
        btn_free_renew = '🆓 Продлить подписку бесплатно'
        btn_referral = '👥 Реферальная программа'
        btn_video_instruction = '📹 Видео инструкция'
        show_website_button = False
        show_device_upgrade_button = False
        btn_device_upgrade = '📱 Расширить лимит устройств'
        try:
            row = await async_query_db("SELECT value FROM settings WHERE key = 'btn_renew_sub'", (), one=True)
            if row and row.get('value'):
                btn_renew_sub = row['value']
        except Exception:
            pass
        try:
            row = await async_query_db("SELECT value FROM settings WHERE key = 'btn_free_renew'", (), one=True)
            if row and row.get('value'):
                btn_free_renew = row['value']
        except Exception:
            pass
        try:
            row = await async_query_db("SELECT value FROM settings WHERE key = 'btn_referral'", (), one=True)
            if row and row.get('value'):
                btn_referral = row['value']
        except Exception:
            pass
        try:
            row = await async_query_db("SELECT value FROM settings WHERE key = 'show_website_button'", (), one=True)
            if row and str(row.get('value', '0')) == '1':
                show_website_button = True
        except Exception:
            pass
        try:
            row = await async_query_db("SELECT value FROM settings WHERE key = 'device_upgrade_enabled'", (), one=True)
            if row and str(row.get('value', '0')) == '1':
                show_device_upgrade_button = True
        except Exception:
            pass
        try:
            row = await async_query_db("SELECT value FROM settings WHERE key = 'btn_device_upgrade'", (), one=True)
            if row and row.get('value'):
                btn_device_upgrade = row['value']
        except Exception:
            pass

        # Роутеры показанных клиентов — одной картой на страницу. Молчание
        # основного приложения оставляет колонку пустой, но список не ломает.
        from src import shop_api
        client_routers_map, client_subs_map, _ = await shop_api.routers_of_clients(
            [u['telegram_id'] for u in users_list_local if u.get('telegram_id')]
        )

        # Рендер страницы
        return await render_template(
            'users.html',
            users=users_list_local,
            client_routers_map=client_routers_map,
            client_subs_map=client_subs_map,
            page=page,
            total_pages=total_pages,
            now=now,
            news_templates=news_templates,
            servers=servers_for_filter,
            filter_server_id=filter_server_id,
            expiring_3d=expiring_3d,
            empty_uuid_only=empty_uuid_only,
            min_referrals=min_referrals,
            paid_only=paid_only,
            new_24h_only=new_24h_only,
            traffic_totals_mb=traffic_totals_mb,
            filter_tag=filter_tag,
            existing_tags=existing_tags,
            top_traffic=top_traffic,
            online_only=online_only,
            top_daily_consumption=top_daily_consumption,
            client_telegram=client_telegram,
            client_site=client_site,
            no_lk_email=no_lk_email,
            filter_limit_ip=filter_limit_ip,
            existing_limit_ips=existing_limit_ips,
            has_email_col=has_email_col,
            btn_renew_sub=btn_renew_sub,
            btn_free_renew=btn_free_renew,
            btn_referral=btn_referral,
            btn_video_instruction=btn_video_instruction,
            show_website_button=show_website_button,
            show_device_upgrade_button=show_device_upgrade_button,
            btn_device_upgrade=btn_device_upgrade,
            search_query=search_query,
            traffic_source=traffic_source,
        )

    # Профиль пользователя
    @admin_bp_instance.route('/users/<int:telegram_id>', methods=['GET', 'POST'])
    async def user_details(telegram_id):
        
        if request.method == 'POST':
            form = await request.form
            notified_expiring = 1 if 'notified_expiring' in form else 0
            notified_expired = 1 if 'notified_expired' in form else 0
            await async_execute_db(
                "UPDATE users SET notified_expiring = ?, notified_expired = ? WHERE telegram_id = ?",
                (notified_expiring, notified_expired, telegram_id)
            )
            await flash('Статусы уведомлений обновлены.', 'success')
            return redirect(url_for('admin.user_details', telegram_id=telegram_id))

        column_names = await get_table_columns_cached('users')
        has_is_blocked = 'is_blocked' in column_names

        if has_is_blocked:
            user = await async_query_db("SELECT *, COALESCE(subscription_mode, 'multi') as subscription_mode, COALESCE(is_blocked, 0) as is_blocked FROM users WHERE telegram_id = ?", (telegram_id,), one=True)
        else:
            user = await async_query_db("SELECT *, COALESCE(subscription_mode, 'multi') as subscription_mode, 0 as is_blocked FROM users WHERE telegram_id = ?", (telegram_id,), one=True)
        if not user:
            await flash(f'Пользователь с ID {telegram_id} не найден.', 'danger')
            return redirect(url_for('admin.users_list'))

        payments = await async_query_db("SELECT * FROM payments WHERE telegram_id = ? ORDER BY created_at DESC", (telegram_id,))
        # Показываем активированные промокоды из новой таблицы redemptions (совместимо со старой схемой)
        promo = await async_query_db("SELECT code, used_at FROM promo_redemptions WHERE telegram_id = ? ORDER BY used_at DESC", (telegram_id,))
        try:
            device_limit_history = await async_query_db(
                "SELECT old_limit, new_limit, source, payment_id, reason, changed_at "
                "FROM device_limit_changes WHERE telegram_id = ? ORDER BY changed_at DESC",
                (telegram_id,),
            )
        except Exception:
            device_limit_history = []
        user = dict(user)

        # Конвертируем дату окончания подписки в объект datetime
        if user.get('subscription_end_date'):
            try:
                current_app.logger.info(f"DEBUG: Конвертируем дату {user['subscription_end_date']} для пользователя {telegram_id}")
                dt = datetime.fromisoformat(user['subscription_end_date'])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
                user['subscription_end_date'] = dt
                current_app.logger.info(f"DEBUG: Дата успешно сконвертирована: {dt}")
            except Exception as e:
                current_app.logger.error(f"DEBUG: Ошибка конвертации даты для пользователя {telegram_id}: {e}")
                user['subscription_end_date'] = None
        else:
            current_app.logger.info(f"DEBUG: У пользователя {telegram_id} нет даты окончания подписки")
        
        # Трафик грузим лениво на клиенте через отдельный эндпоинт
        traffic_stats = None
        multi_traffic_stats = None

        # Текущее время для отладочной информации
        now = datetime.now(timezone.utc)

        # Флаг: заполнен ли токен 2ip — для обогащения IP во фронте
        _2ip_row = await async_query_db(
            "SELECT value FROM settings WHERE key = 'web_2ip_token'", (), one=True,
        )
        has_2ip_token = bool(_2ip_row and (_2ip_row.get('value') or '').strip())

        # Расширенное (опасное) редактирование: сырые значения строки users + метаданные колонок.
        # user выше содержит вычисленные/сконвертированные поля (дата → datetime), поэтому для
        # редактора берём отдельный «сырой» снимок строки.
        _raw_row = await async_query_db("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,), one=True)
        user_raw = dict(_raw_row) if _raw_row else {}
        _col_info = await async_query_db("PRAGMA table_info(users)", ())
        user_columns_meta = [
            {'name': c['name'], 'type': (c['type'] or '').upper(), 'pk': bool(c['pk'])}
            for c in (_col_info or [])
        ]

        # Роутеры клиента живут в основном приложении — там же, где парк и заказы.
        # Ошибку не поднимаем: карточка клиента не должна пропадать из-за того,
        # что соседний сервис молчит, — блок просто скажет, что не дозвонился.
        from src import shop_api
        client_routers_data, client_routers_error = await shop_api.client_routers(telegram_id)

        return await render_template(
            'user_details.html',
            user=user,
            client_routers=client_routers_data.get('routers', []),
            client_free_routers=client_routers_data.get('free', []),
            client_routers_error=client_routers_error,
            payments=payments,
            promo=promo,
            traffic_stats=traffic_stats,
            multi_traffic_stats=multi_traffic_stats,
            now=now,
            device_limit_history=device_limit_history,
            has_2ip_token=has_2ip_token,
            user_raw=user_raw,
            user_columns_meta=user_columns_meta,
        )

    @admin_bp_instance.route('/users/<int:telegram_id>/edit-raw', methods=['POST'])
    async def user_edit_raw(telegram_id):
        """Опасное расширенное редактирование строки users. Только админ.

        Имена колонок валидируются по фактической схеме (whitelist из PRAGMA) —
        SQL-инъекция через имя колонки невозможна; значения идут параметрами.
        PK (telegram_id) не редактируется. Пустое значение трактуется как NULL.
        """
        from web_admin.run import current_user
        if not current_user.is_admin:
            await flash('Недостаточно прав для этого действия.', 'danger')
            return redirect(url_for('admin.user_details', telegram_id=telegram_id))

        exists = await async_query_db("SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,), one=True)
        if not exists:
            await flash(f'Пользователь {telegram_id} не найден.', 'danger')
            return redirect(url_for('admin.users_list'))

        _col_info = await async_query_db("PRAGMA table_info(users)", ())
        col_types = {c['name']: (c['type'] or '').upper() for c in (_col_info or [])}
        pk_cols = {c['name'] for c in (_col_info or []) if c['pk']}

        form = await request.form
        updates = {}
        for key in list(form.keys()):
            if not key.startswith('col__'):
                continue
            col = key[len('col__'):]
            if col not in col_types or col in pk_cols:
                continue
            raw_val = form.get(key)
            if raw_val is None or str(raw_val).strip() == '':
                updates[col] = None
                continue
            t = col_types[col]
            try:
                if 'INT' in t:
                    updates[col] = int(str(raw_val).strip())
                elif any(x in t for x in ('REAL', 'FLOA', 'DOUB', 'NUMER', 'DEC')):
                    updates[col] = float(str(raw_val).strip())
                else:
                    updates[col] = raw_val
            except (ValueError, TypeError):
                await flash(f'Некорректное значение для «{col}» (ожидался тип {t}).', 'danger')
                return redirect(url_for('admin.user_details', telegram_id=telegram_id))

        if not updates:
            await flash('Не выбрано ни одного корректного поля для изменения.', 'info')
            return redirect(url_for('admin.user_details', telegram_id=telegram_id))

        set_clause = ', '.join(f'"{c}" = ?' for c in updates)
        params = list(updates.values()) + [telegram_id]
        await async_execute_db(f'UPDATE users SET {set_clause} WHERE telegram_id = ?', params)

        admin_id = getattr(current_user, 'user_id', '?')
        logger.warning(
            f"[ADMIN][DANGER] admin={admin_id} raw-edit users(telegram_id={telegram_id}) "
            f"поля={list(updates.keys())}"
        )
        await flash(f'Данные обновлены. Изменено полей: {len(updates)}.', 'success')
        return redirect(url_for('admin.user_details', telegram_id=telegram_id))

    # JSON: трафик/онлайн
    @admin_bp_instance.route('/users/<int:telegram_id>/traffic', methods=['GET'])
    async def user_traffic(telegram_id):
        try:
            user_row = await async_query_db("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,), one=True)
            if not user_row:
                return jsonify({"ok": False, "error": "user_not_found"}), 404
            if not user_row.get('xui_client_uuid'):
                return jsonify({"ok": True, "ips": [], "ips_map": {}, "message": "no_uuid"})
            return jsonify({"ok": True, "ips": [], "ips_map": {}, "traffic": [], "source": "remnawave"})
        except Exception as e:
            current_app.logger.error(f"traffic_json error for user {telegram_id}: {e}")
            return jsonify({"ok": False, "error": "internal_error"}), 500


    @admin_bp_instance.route('/users/<int:telegram_id>/traffic/stream', methods=['GET'])
    async def user_traffic_stream(telegram_id):
        from quart import Response
        async def gen_lines():
            yield '{"event": "done"}\n'
        return Response(gen_lines(), mimetype='application/x-ndjson')


    @admin_bp_instance.route('/users/<int:telegram_id>/subscription_requests', methods=['GET'])
    async def user_subscription_requests(telegram_id):
        """AJAX: запросы подписки (не браузер и не Telegram), включая записи без hwid; пагинация по 30."""
        try:
            user_row = await async_query_db(
                "SELECT xui_client_uuid FROM users WHERE telegram_id = ?",
                (telegram_id,), one=True
            )
            if not user_row:
                return jsonify({'ok': False, 'error': 'user_not_found'}), 404

            uuid = (dict(user_row).get('xui_client_uuid') or '').strip()
            if not uuid:
                return jsonify({'ok': True, 'total': 0, 'items': [], 'pages': 1, 'page': 1})

            try:
                page = max(1, int(request.args.get('page', 1)))
            except (ValueError, TypeError):
                page = 1

            try:
                from devices.database import get_subscription_requests_paginated
                result = await get_subscription_requests_paginated(uuid, page=page, per_page=30)
                return jsonify({'ok': True, **result})
            except Exception as db_err:
                current_app.logger.warning(f"[sub_requests] devices DB недоступна: {db_err}")
                return jsonify({'ok': False, 'error': 'devices_db_unavailable'}), 503

        except Exception as e:
            current_app.logger.error(f"[sub_requests] fatal for {telegram_id}: {e}")
            return jsonify({'ok': False, 'error': 'internal_error'}), 500

    @admin_bp_instance.route('/users/<int:telegram_id>/subscription_requests/delete', methods=['POST'])
    async def delete_user_subscription_request(telegram_id):
        """AJAX: удаляет запросы подписки клиента по user_agent (в пределах его uuid)."""
        try:
            payload = await request.get_json(silent=True) or {}
            user_agent = (payload.get('user_agent') or '').strip()
            if not user_agent:
                return jsonify({'ok': False, 'error': 'no_user_agent'}), 400

            user_row = await async_query_db(
                "SELECT xui_client_uuid FROM users WHERE telegram_id = ?",
                (telegram_id,), one=True
            )
            if not user_row:
                return jsonify({'ok': False, 'error': 'user_not_found'}), 404

            uuid = (dict(user_row).get('xui_client_uuid') or '').strip()
            if not uuid:
                return jsonify({'ok': False, 'error': 'no_uuid'}), 400

            try:
                from devices.database import delete_subscription_request
                deleted = await delete_subscription_request(uuid, user_agent)
            except Exception as db_err:
                current_app.logger.warning(f"[sub_requests] devices DB недоступна: {db_err}")
                return jsonify({'ok': False, 'error': 'devices_db_unavailable'}), 503

            if not deleted:
                return jsonify({'ok': False, 'error': 'not_found'}), 404
            return jsonify({'ok': True, 'deleted': deleted})

        except Exception as e:
            current_app.logger.error(f"[sub_requests] delete fatal for {telegram_id}: {e}")
            return jsonify({'ok': False, 'error': 'internal_error'}), 500

    # Удаление пользователя
    @admin_bp_instance.route('/users/<int:telegram_id>/delete', methods=['POST'])
    
    async def delete_user(telegram_id):
        
        user = await async_query_db("SELECT *, COALESCE(subscription_mode, 'multi') as subscription_mode FROM users WHERE telegram_id = ?", (telegram_id,), one=True)
        if not user:
            # Для AJAX возвращаем JSON
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'ok': False, 'error': 'not_found'}), 404
            await flash('Пользователь не найден.', 'danger')
            return redirect(url_for('admin.users_list'))

        async def delete_user_from_systems():
            try:
                await app_conf.load_settings()
                from remnawave_manager import remnawave_manager_instance
                if user.get('xui_client_uuid'):
                    try:
                        await remnawave_manager_instance.delete_user(user['xui_client_uuid'])
                        current_app.logger.info(f"[ADMIN] Пользователь {telegram_id} удален из Remnawave")
                        return True, None
                    except Exception as e:
                        current_app.logger.warning(f"[ADMIN] Ошибка удаления из Remnawave {telegram_id}: {e}")
                        return False, str(e)
                return True, None
            except Exception as e:
                return False, str(e)

        failed_servers = []

        if user.get('xui_client_uuid'):
            try:
                _, delete_err = await delete_user_from_systems()
                if delete_err:
                    failed_servers.append(delete_err)
            except Exception as e:
                failed_servers.append(f"Ошибка выполнения: {e}")

        # Удаляем/обнуляем связанные записи из всех таблиц с внешними ключами
        try:
            await async_execute_db("UPDATE payments SET telegram_id = NULL WHERE telegram_id = ?", (telegram_id,))
        except Exception as e:
            current_app.logger.warning(f"Не удалось отвязать платежи пользователя {telegram_id}: {e}")
        try:
            await async_execute_db("UPDATE promo_codes SET activated_by_telegram_id = NULL WHERE activated_by_telegram_id = ?", (telegram_id,))
        except Exception as e:
            current_app.logger.warning(f"Не удалось отвязать промокоды пользователя {telegram_id}: {e}")
        try:
            await async_execute_db("DELETE FROM client_recreation_errors WHERE telegram_id = ?", (telegram_id,))
        except Exception as e:
            current_app.logger.warning(f"Не удалось удалить ошибки восстановления пользователя {telegram_id}: {e}")
        try:
            await async_execute_db("DELETE FROM promo_redemptions WHERE telegram_id = ?", (telegram_id,))
        except Exception as e:
            current_app.logger.warning(f"Не удалось удалить активации промокодов пользователя {telegram_id}: {e}")
        try:
            await async_execute_db("DELETE FROM user_enabled_servers WHERE telegram_id = ?", (telegram_id,))
        except Exception as e:
            current_app.logger.warning(f"Не удалось удалить настройки серверов пользователя {telegram_id}: {e}")
        # Удаляем партнерские начисления (где пользователь был партнером или плательщиком)
        try:
            await async_execute_db("DELETE FROM partner_accruals WHERE partner_id = ? OR payer_id = ?", (telegram_id, telegram_id))
        except Exception as e:
            current_app.logger.warning(f"Не удалось удалить партнерские начисления пользователя {telegram_id}: {e}")
        # Удаляем реферальные бонусы (join / payment), где пользователь — пригласивший или приглашённый
        try:
            await db_helpers.delete_referral_bonuses_for_user(telegram_id)
        except Exception as e:
            current_app.logger.warning(f"Не удалось удалить реферальные бонусы пользователя {telegram_id}: {e}")
        # Обнуляем invited_by в других пользователях, которые были приглашены этим пользователем
        try:
            await async_execute_db("UPDATE users SET invited_by = NULL WHERE invited_by = ?", (telegram_id,))
        except Exception as e:
            current_app.logger.warning(f"Не удалось обнулить приглашения пользователя {telegram_id}: {e}")
        # Удаляем пользователя
        await async_execute_db("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))

        message = (
            f'Пользователь {telegram_id} удалён из базы данных. '
            'Удалён со всех возможных серверов в зависимости от их доступности и провайдера.'
        )
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'ok': True, 'message': message, 'failed_servers': failed_servers})
        await flash(message, 'toast')
        return redirect(url_for('admin.users_list'))

    # ─── Pre-flight для revoke (быстрая проверка инфраструктуры) ─────
    @admin_bp_instance.route('/api/users/<int:telegram_id>/revoke-preflight', methods=['GET', 'POST'])
    async def revoke_preflight(telegram_id):
        from web_admin.core.revoke import preflight_check
        try:
            pre = await preflight_check(telegram_id)
            return jsonify({'ok': True, 'preflight': pre.to_dict()})
        except Exception as e:
            logger.exception(f"[REVOKE PREFLIGHT] {telegram_id}: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

    # ─── Отзыв подписки (стримит SSE) ────────────────────────────────
    @admin_bp_instance.route('/api/users/<int:telegram_id>/revoke-subscription', methods=['POST'])
    async def revoke_subscription(telegram_id):
        from quart import Response
        from web_admin.core.revoke import revoke_subscription as core_revoke

        async def generate_events():
            queue: asyncio.Queue = asyncio.Queue()

            async def on_event(step, message=None, error=None, complete=False, extra=None):
                payload = {'step': step}
                if message:
                    payload['message'] = message
                if error:
                    payload['error'] = error
                if complete:
                    payload['complete'] = True
                if extra:
                    payload['extra'] = extra
                await queue.put(payload)

            async def runner():
                try:
                    await core_revoke(telegram_id, on_event=on_event)
                except Exception as e:
                    logger.exception(f"[REVOKE] критическая ошибка для {telegram_id}: {e}")
                    await queue.put({'step': 0, 'error': f'Критическая ошибка: {e}'})
                finally:
                    await queue.put(None)  # сигнал окончания

            task = asyncio.create_task(runner())
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(0.005)
            finally:
                if not task.done():
                    task.cancel()

        return Response(
            generate_events(),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
            },
        )


    # Блокировка пользователя
    @admin_bp_instance.route('/users/<int:telegram_id>/block', methods=['POST'])
    
    async def block_user(telegram_id):
        
        user = await async_query_db("SELECT *, COALESCE(subscription_mode, 'multi') as subscription_mode FROM users WHERE telegram_id = ?", (telegram_id,), one=True)
        if not user:
            await flash('Пользователь не найден.', 'danger')
            return redirect(url_for('admin.users_list'))

        user = dict(user) if user else None

        column_names = await get_table_columns_cached('users')
        has_is_blocked = 'is_blocked' in column_names

        if has_is_blocked and user.get('is_blocked', 0):
            await flash('Пользователь уже заблокирован.', 'warning')
            return redirect(url_for('admin.user_details', telegram_id=telegram_id))

        async def block_user_remnawave():
            try:
                await app_conf.load_settings()
                from remnawave_manager import remnawave_manager_instance
                if user.get('xui_client_uuid'):
                    await remnawave_manager_instance._ensure_initialized()
                    if remnawave_manager_instance._sdk:
                        await remnawave_manager_instance._sdk.users.disable_user(user['xui_client_uuid'])
                return True, None
            except Exception as e:
                return False, str(e)

        failed_servers = []
        if user.get('xui_client_uuid'):
            ok_rw, err = await block_user_remnawave()
            if not ok_rw and err:
                failed_servers.append(err)

        if has_is_blocked:
            await async_execute_db("UPDATE users SET is_blocked = 1 WHERE telegram_id = ?", (telegram_id,))
        else:
            await async_execute_db("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0", ())
            await async_execute_db("UPDATE users SET is_blocked = 1 WHERE telegram_id = ?", (telegram_id,))

        # Дополнительно: делаем подписку неактивной сразу — смещаем дату окончания на предыдущий день (UTC)
        try:
            from datetime import datetime, timezone, timedelta
            yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            # Сбрасываем также флаги уведомлений об окончании, чтобы избежать рассинхронизации
            await async_execute_db(
                "UPDATE users SET subscription_end_date = ?, notified_expiring = 0, notified_expired = 0 WHERE telegram_id = ?",
                (yesterday_str, telegram_id)
            )
            # Если есть колонка is_active — принудительно обнулим
            if 'is_active' in column_names:
                await async_execute_db("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
        except Exception:
            # Не критично для блокировки; детали в логах Flask
            pass

        message = (
            f'Пользователь {telegram_id} заблокирован. '
            'Удалён со всех возможных серверов в зависимости от их доступности и провайдера.'
        )
        await flash(message, 'toast')
        return redirect(url_for('admin.user_details', telegram_id=telegram_id))

    # Разблокировка пользователя
    @admin_bp_instance.route('/users/<int:telegram_id>/unblock', methods=['POST'])
    
    async def unblock_user(telegram_id):
        
        form = await request.form
        next_dest = (form.get('next') or '').strip()

        user = await async_query_db("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,), one=True)
        if not user:
            await flash('Пользователь не найден.', 'danger')
            return redirect(url_for('admin.users_list'))

        user = dict(user) if user else None

        column_names = await get_table_columns_cached('users')
        has_is_blocked = 'is_blocked' in column_names

        if not has_is_blocked or not user.get('is_blocked', 0):
            await flash('Пользователь не заблокирован.', 'warning')
            return redirect(url_for('admin.user_details', telegram_id=telegram_id))

        if has_is_blocked:
            await async_execute_db("UPDATE users SET is_blocked = 0 WHERE telegram_id = ?", (telegram_id,))
        else:
            await async_execute_db("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0", ())
            await async_execute_db("UPDATE users SET is_blocked = 0 WHERE telegram_id = ?", (telegram_id,))

        await flash(f'Пользователь {telegram_id} разблокирован. Для восстановления доступа ему нужно будет продлить подписку.', 'success')
        if next_dest == 'blocked':
            return redirect(url_for('admin.blocked_users_list'))
        return redirect(url_for('admin.user_details', telegram_id=telegram_id))

    # Продление подписки
    @admin_bp_instance.route('/users/<int:telegram_id>/renew', methods=['POST'])
    
    async def renew_subscription(telegram_id):
        
        try:
            form = await request.form
            days_to_add = int(form.get('days', 0))
            admin_message = (form.get('admin_message', '') or '').strip()
            notify_user = form.get('notify_user') is not None
            if days_to_add <= 0:
                await flash('Количество дней должно быть положительным числом.', 'danger')
                return redirect(url_for('admin.user_details', telegram_id=telegram_id))
        except (ValueError, TypeError):
            await flash('Неверное количество дней.', 'danger')
            return redirect(url_for('admin.user_details', telegram_id=telegram_id))

        # Получаем limit_ip до асинхронной функции
        user_row = await async_query_db("SELECT limit_ip FROM users WHERE telegram_id = ?", (telegram_id,), one=True)
        limit_ip_local = user_row['limit_ip'] if user_row and user_row['limit_ip'] is not None else 0

        async def do_renew():
            await app_conf.load_settings()
            user = await db_helpers.get_last_subscription(telegram_id)
            # Используем limit_ip_local из внешнего контекста
            result = await grant_subscription(telegram_id, days_to_add, limit_ip=limit_ip_local)
            if result and result.get('expiry_date'):
                return True, result['expiry_date'], result.get('remnawave_traffic_info'), result.get('sub_link', 'N/A')
            return False, None, None, None

        try:
            success, new_expiry_date, traffic_info, sub_link = await do_renew()
            if success:
                # После успешного продления помечаем пользователя активным (если колонка есть)
                try:
                    column_names = await get_table_columns_cached('users')
                    if 'is_active' in column_names:
                        await async_execute_db("UPDATE users SET is_active = 1 WHERE telegram_id = ?", (telegram_id,))
                except Exception:
                    pass
                
                # Формируем сообщение о продлении
                flash_message = f'Подписка для пользователя {telegram_id} успешно продлена до {new_expiry_date.strftime("%d.%m.%Y %H:%M")}.'
                if traffic_info:
                    flash_message += f' Трафик: {traffic_info["old_limit_gb"]}GB → {traffic_info["new_limit_gb"]}GB (+{traffic_info["added_gb"]}GB)'
                await flash(flash_message, 'success')
                
                # Отправляем сообщение пользователю только если отмечено notify_user
                if notify_user:
                    try:
                        moscow = pytz.timezone('Europe/Moscow')
                        local_expiry_date = new_expiry_date.astimezone(moscow) if new_expiry_date.tzinfo else new_expiry_date
                        
                        # Формируем красивое сообщение о продлении подписки с эмодзи и цитированием
                        success_message = f"✅ <b>Подписка успешно продлена!</b>\n\n"
                        success_message += f"📅 <b>Действует до:</b>\n"
                        success_message += f"<blockquote>{local_expiry_date.strftime('%d.%m.%Y %H:%M %Z')}</blockquote>"
                        if admin_message:
                            success_message += f"\n\n💬 <b>От администратора:</b>\n<blockquote>{html.escape(admin_message)}</blockquote>"
                        
                        reply_markup = get_success_with_referral_keyboard()
                        await send_telegram_message(int(telegram_id), success_message, reply_markup=reply_markup)
                    except Exception as e:
                        logger.error(f"Ошибка отправки сообщения пользователю {telegram_id} при продлении подписки: {e}", exc_info=True)
            else:
                await flash(f'Произошла ошибка при продлении подписки для пользователя {telegram_id}. См. логи.', 'danger')
        except Exception as e:
            import traceback
            error_traceback = traceback.format_exc()
            error_message = f'Критическая ошибка при запуске задачи продления: {e}\n\nTraceback:\n{error_traceback}'
            await flash(error_message, 'danger')
            # Также логируем в консоль для отладки
            import sys
            print(f"ERROR in renew_subscription for user {telegram_id}: {e}", file=sys.stderr)
            print(error_traceback, file=sys.stderr)
        return redirect(url_for('admin.user_details', telegram_id=telegram_id))

    # Уменьшение срока
    @admin_bp_instance.route('/users/<int:telegram_id>/reduce', methods=['POST'])
    
    async def reduce_subscription(telegram_id):
        
        try:
            form = await request.form
            days_to_reduce = int(form.get('days', 0))
            if days_to_reduce <= 0:
                await flash('Количество дней должно быть положительным числом.', 'danger')
                return redirect(url_for('admin.user_details', telegram_id=telegram_id))
        except (ValueError, TypeError):
            await flash('Неверное количество дней.', 'danger')
            return redirect(url_for('admin.user_details', telegram_id=telegram_id))

        # Получаем limit_ip до асинхронной функции
        user_row = await async_query_db("SELECT limit_ip FROM users WHERE telegram_id = ?", (telegram_id,), one=True)
        limit_ip_local = user_row['limit_ip'] if user_row and user_row['limit_ip'] is not None else 0

        async def do_reduce():
            await app_conf.load_settings()
            user = await db_helpers.get_last_subscription(telegram_id)
            # Используем отрицательные дни для уменьшения срока
            result = await grant_subscription(telegram_id, -days_to_reduce, limit_ip=limit_ip_local)
            if result and result.get('expiry_date'):
                return True, result['expiry_date']
            return False, None

        try:
            success, new_expiry_date = await do_reduce()
            if success:
                await flash(f'Срок подписки пользователя {telegram_id} уменьшен. Новая дата: {new_expiry_date.strftime("%d.%m.%Y %H:%M")}.', 'success')
            else:
                await flash(f'Не удалось уменьшить срок подписки пользователя {telegram_id}. См. логи.', 'danger')
        except Exception as e:
            await flash(f'Критическая ошибка при запуске задачи уменьшения срока: {e}', 'danger')
        return redirect(url_for('admin.user_details', telegram_id=telegram_id))

    # Список заблокированных
    @admin_bp_instance.route('/users/blocked')
    
    async def blocked_users_list():
        
        from datetime import datetime, timezone
        args = request.args
        # Защита от ввода 20+ цифр: SQLite не примет int > 2^63-1, иначе OverflowError.
        search_id = None
        _sid_raw = args.get('search_id')
        if _sid_raw:
            try:
                _candidate = int(_sid_raw)
                if abs(_candidate) <= (1 << 63) - 1:
                    search_id = _candidate
            except Exception:
                search_id = None
        try:
            page = int(args.get('page') or 1)
        except Exception:
            page = 1
        per_page = 15
        offset = (page - 1) * per_page
        users_list_local = []
        total_users = 0
        column_names = await get_table_columns_cached('users')
        has_is_blocked = 'is_blocked' in column_names

        sel_bl = ["u.telegram_id", "u.username"]
        grp_bl = ["u.telegram_id", "u.username"]
        if 'real_username' in column_names:
            sel_bl.append("u.real_username")
            grp_bl.append("u.real_username")
        if 'email' in column_names:
            sel_bl.append("u.email")
            grp_bl.append("u.email")
        sel_bl.extend([
            "u.subscription_end_date", "u.is_trial_used", "u.current_server_id",
            "COALESCE(u.limit_ip, 0) as limit_ip", "COALESCE(u.is_blocked, 0) as is_blocked",
            "COUNT(r.telegram_id) as referrals_count", "u.created_at",
        ])
        grp_bl.extend([
            "u.subscription_end_date", "u.is_trial_used", "u.current_server_id",
            "u.limit_ip", "u.is_blocked", "u.created_at",
        ])
        blocked_list_sql = (
            f"SELECT {', '.join(sel_bl)} FROM users u "
            "LEFT JOIN users r ON u.telegram_id = r.invited_by "
            "WHERE COALESCE(u.is_blocked, 0) = 1 "
            f"GROUP BY {', '.join(grp_bl)} "
            "ORDER BY u.telegram_id DESC LIMIT ? OFFSET ?"
        )

        search_cols = ["telegram_id", "username"]
        if 'real_username' in column_names:
            search_cols.append("real_username")
        if 'email' in column_names:
            search_cols.append("email")
        search_cols.extend([
            "subscription_end_date", "is_trial_used", "current_server_id",
            "COALESCE(limit_ip,0) as limit_ip", "COALESCE(is_blocked, 0) as is_blocked",
            "created_at",
        ])
        search_one_sql = (
            f"SELECT {', '.join(search_cols)} FROM users "
            "WHERE telegram_id = ? AND COALESCE(is_blocked, 0) = 1"
        )

        if search_id:
            if has_is_blocked:
                user = await async_query_db(search_one_sql, (search_id,), one=True)
            else:
                user = None
            if user:
                user = dict(user)
                if user['subscription_end_date']:
                    try:
                        dt = datetime.fromisoformat(user['subscription_end_date'])
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        else:
                            dt = dt.astimezone(timezone.utc)
                        user['subscription_end_date'] = dt
                    except Exception:
                        user['subscription_end_date'] = None
                ref_row = await async_query_db(
                    "SELECT COUNT(*) as c FROM users WHERE invited_by = ?", (search_id,), one=True,
                )
                user['referrals_count'] = int((ref_row or {}).get('c') or 0)
                if user.get('created_at'):
                    try:
                        created_dt = datetime.fromisoformat(user['created_at'])
                        if created_dt.tzinfo is None:
                            created_dt = created_dt.replace(tzinfo=timezone.utc)
                        else:
                            created_dt = created_dt.astimezone(timezone.utc)
                        user['days_with_us'] = (datetime.now(timezone.utc) - created_dt).days
                    except Exception:
                        user['days_with_us'] = None
                else:
                    user['days_with_us'] = None
                users_list_local = [user]
                total_users = 1
            else:
                users_list_local = []
                total_users = 0
            total_pages = 1
        else:
            if has_is_blocked:
                users = await async_query_db(blocked_list_sql, (per_page, offset))
            else:
                users = []
            for user in users:
                user = dict(user)
                if user['subscription_end_date']:
                    try:
                        dt = datetime.fromisoformat(user['subscription_end_date'])
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        else:
                            dt = dt.astimezone(timezone.utc)
                        user['subscription_end_date'] = dt
                    except Exception:
                        user['subscription_end_date'] = None
                # Вычисляем количество дней с момента регистрации
                if user.get('created_at'):
                    try:
                        created_dt = datetime.fromisoformat(user['created_at'])
                        if created_dt.tzinfo is None:
                            created_dt = created_dt.replace(tzinfo=timezone.utc)
                        else:
                            created_dt = created_dt.astimezone(timezone.utc)
                        days_with_us = (datetime.now(timezone.utc) - created_dt).days
                        user['days_with_us'] = days_with_us
                    except Exception:
                        user['days_with_us'] = None
                else:
                    user['days_with_us'] = None
                users_list_local.append(user)
            if has_is_blocked:
                total_users_row = await async_query_db("SELECT COUNT(*) as cnt FROM users WHERE COALESCE(is_blocked, 0) = 1", (), one=True)
                total_users = total_users_row['cnt'] if total_users_row else 0
            else:
                total_users = 0
            total_pages = (total_users + per_page - 1) // per_page
        # Загружаем сохраненную ссылку на JSON из настроек
        json_url_setting = await async_query_db("SELECT value FROM settings WHERE key = 'blocked_users_json_url'", (), one=True)
        saved_json_url = json_url_setting['value'] if json_url_setting and json_url_setting.get('value') else ''
        
        now = datetime.now(timezone.utc)
        show_blocked_email_col = 'email' in column_names
        return await render_template(
            'blocked_users.html',
            users=users_list_local,
            page=page,
            total_pages=total_pages,
            total_users=total_users,
            now=now,
            saved_json_url=saved_json_url,
            show_blocked_email_col=show_blocked_email_col,
        )

    @admin_bp_instance.route('/users/blocked/import', methods=['POST'])
    async def import_blocked_users():
        """API endpoint для импорта заблокированных пользователей из JSON по ссылке"""
        
        try:
            data = await request.get_json()
            json_url = data.get('json_url', '').strip()
            
            if not json_url:
                return jsonify({'success': False, 'error': 'Не указана ссылка на JSON файл'}), 400
            
            # Сохраняем ссылку в настройках
            try:
                existing_setting = await async_query_db("SELECT key FROM settings WHERE key = 'blocked_users_json_url'", (), one=True)
                if existing_setting:
                    await async_execute_db("UPDATE settings SET value = ? WHERE key = 'blocked_users_json_url'", (json_url,))
                else:
                    await async_execute_db("INSERT INTO settings (key, value) VALUES ('blocked_users_json_url', ?)", (json_url,))
                logger.info(f"[IMPORT_BLOCKED] Ссылка на JSON сохранена в настройках: {json_url}")
            except Exception as e:
                logger.warning(f"[IMPORT_BLOCKED] Не удалось сохранить ссылку в настройках: {e}")
            
            # Загружаем JSON по ссылке
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(json_url)
                    response.raise_for_status()
                    users_data = response.json()
            except httpx.HTTPError as e:
                logger.error(f"[IMPORT_BLOCKED] Ошибка загрузки JSON: {e}")
                return jsonify({'success': False, 'error': f'Ошибка загрузки JSON: {str(e)}'}), 400
            except json.JSONDecodeError as e:
                logger.error(f"[IMPORT_BLOCKED] Ошибка парсинга JSON: {e}")
                return jsonify({'success': False, 'error': f'Ошибка парсинга JSON: {str(e)}'}), 400
            
            # Проверяем, что это массив
            if not isinstance(users_data, list):
                return jsonify({'success': False, 'error': 'JSON должен содержать массив объектов'}), 400
            
            # Статистика
            total = len(users_data)
            created = 0
            skipped = 0
            errors = 0
            error_details = []
            
            # Проверяем наличие колонки is_blocked
            column_names = await get_table_columns_cached('users')
            has_is_blocked = 'is_blocked' in column_names
            
            if not has_is_blocked:
                return jsonify({'success': False, 'error': 'Таблица users не поддерживает поле is_blocked'}), 500
            
            # Обрабатываем каждого пользователя
            for idx, user_item in enumerate(users_data):
                try:
                    telegram_id = user_item.get('telegram_id')
                    username = user_item.get('username')
                    
                    # Валидация
                    if not telegram_id:
                        errors += 1
                        error_details.append(f"Запись #{idx + 1}: отсутствует telegram_id")
                        continue
                    
                    try:
                        telegram_id = int(telegram_id)
                    except (ValueError, TypeError):
                        errors += 1
                        error_details.append(f"Запись #{idx + 1}: некорректный telegram_id '{telegram_id}'")
                        continue
                    
                    if telegram_id <= 0:
                        errors += 1
                        error_details.append(f"Запись #{idx + 1}: telegram_id должен быть положительным числом")
                        continue
                    
                    # Нормализуем username
                    if username:
                        username = str(username).strip() or None
                    else:
                        username = None
                    
                    # Проверяем, существует ли пользователь
                    existing_user = await async_query_db(
                        "SELECT telegram_id FROM users WHERE telegram_id = ?",
                        (telegram_id,),
                        one=True
                    )
                    
                    if existing_user:
                        skipped += 1
                        logger.info(f"[IMPORT_BLOCKED] Пользователь {telegram_id} уже существует, пропускаем")
                        continue
                    
                    # Создаем нового заблокированного пользователя
                    await async_execute_db(
                        """INSERT INTO users (telegram_id, username, is_blocked, created_at) 
                           VALUES (?, ?, 1, datetime('now'))""",
                        (telegram_id, username)
                    )
                    created += 1
                    logger.info(f"[IMPORT_BLOCKED] Создан заблокированный пользователь {telegram_id} (username: {username or 'N/A'})")
                    
                except Exception as e:
                    errors += 1
                    error_msg = f"Запись #{idx + 1}: {str(e)}"
                    error_details.append(error_msg)
                    logger.error(f"[IMPORT_BLOCKED] {error_msg}", exc_info=True)
            
            logger.info(f"[IMPORT_BLOCKED] Импорт завершен: всего {total}, создано {created}, пропущено {skipped}, ошибок {errors}")
            
            return jsonify({
                'success': True,
                'total': total,
                'created': created,
                'skipped': skipped,
                'errors': errors,
                'error_details': error_details if errors > 0 else []
            })
            
        except Exception as e:
            logger.error(f"[IMPORT_BLOCKED] Критическая ошибка: {e}", exc_info=True)
            return jsonify({'success': False, 'error': f'Критическая ошибка: {str(e)}'}), 500

    @admin_bp_instance.route('/users/blocked/save-json-url', methods=['POST'])
    async def save_blocked_users_json_url():
        """API endpoint для сохранения ссылки на JSON файл в настройках"""
        
        try:
            data = await request.get_json()
            json_url = data.get('json_url', '').strip()
            
            # Сохраняем ссылку в настройках
            existing_setting = await async_query_db("SELECT key FROM settings WHERE key = 'blocked_users_json_url'", (), one=True)
            if existing_setting:
                await async_execute_db("UPDATE settings SET value = ? WHERE key = 'blocked_users_json_url'", (json_url,))
            else:
                await async_execute_db("INSERT INTO settings (key, value) VALUES ('blocked_users_json_url', ?)", (json_url,))
            
            logger.info(f"[SAVE_JSON_URL] Ссылка на JSON сохранена в настройках: {json_url}")
            
            return jsonify({'success': True, 'message': 'Ссылка успешно сохранена'})
            
        except Exception as e:
            logger.error(f"[SAVE_JSON_URL] Ошибка сохранения ссылки: {e}", exc_info=True)
            return jsonify({'success': False, 'error': f'Ошибка сохранения: {str(e)}'}), 500

    # Изменение лимита устройств
    @admin_bp_instance.route('/users/<int:telegram_id>/edit_limit_ip', methods=['POST'])
    
    async def edit_user_limit_ip(telegram_id):
        
        try:
            form = await request.form
            limit_ip = int(form.get('limit_ip', 0))
            if limit_ip < 0:
                await flash('Лимит устройств не может быть отрицательным.', 'danger')
                return redirect(url_for('admin.user_details', telegram_id=telegram_id))
        except (ValueError, TypeError):
            await flash('Неверное значение лимита устройств.', 'danger')
            return redirect(url_for('admin.user_details', telegram_id=telegram_id))

        user = await async_query_db("SELECT *, COALESCE(subscription_mode, 'multi') as subscription_mode FROM users WHERE telegram_id = ?", (telegram_id,), one=True)
        if not user:
            await flash('Пользователь не найден.', 'danger')
            return redirect(url_for('admin.users_list'))

        # Меняем только значение в БД. На X-UI/Remnawave новый лимит
        # применится автоматически при следующем продлении/обновлении подписки.
        await async_execute_db(
            "UPDATE users SET limit_ip = ? WHERE telegram_id = ?",
            (limit_ip, telegram_id),
        )
        await flash(
            f'Лимит устройств изменен на {limit_ip if limit_ip > 0 else "без лимита"}!',
            'success',
        )
        return redirect(url_for('admin.user_details', telegram_id=telegram_id))


    # Режим подписки убран - все пользователи всегда в multi режиме
    # Функция set_subscription_mode удалена

    @admin_bp_instance.route('/api/users/<int:telegram_id>/tag', methods=['POST'])
    async def update_user_tag(telegram_id):
        """API endpoint для обновления пометки пользователя."""
        # Импортируем асинхронные обертки
        # Проверяем авторизацию вручную, чтобы избежать циклического импорта
        from quart import session
        if not session.get('admin_user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        try:
            data = await request.get_json()
            user_tag = data.get('tag', '').strip() if data else ''
            
            # Валидация: разрешенные пометки
            allowed_tags = ['🕵️‍♂️ Подозрительный', '🎁 Доп пробный', '🙋 Друг', '☎️ Роутер', '👤 Партнёр', '🪪 Ручное добавление', '🗑️ Мусор', '📈 Таргет', '🧩VLESS ссылки', '']
            if user_tag and user_tag not in allowed_tags:
                return jsonify({'error': 'Недопустимая пометка'}), 400
            
            # Обновляем пометку в БД
            await async_execute_db(
                "UPDATE users SET user_tag = ? WHERE telegram_id = ?",
                (user_tag if user_tag else None, telegram_id)
            )
            
            return jsonify({'success': True, 'tag': user_tag})
        except Exception as e:
            current_app.logger.error(f'[ADMIN] update_user_tag error for {telegram_id}: {e}')
            return jsonify({'error': str(e)}), 500

    # Миграция в Remnawave - глобальное состояние
    _migration_state = {
        'status': 'idle',  # idle, running, completed, stopped
        'total': 0,
        'processed': 0,
        'created': 0,
        'skipped': 0,
        'failed': 0,
        'results': {
            'created': [],
            'skipped': [],
            'failed': []
        },
        'stop_requested': False
    }

    @admin_bp_instance.route('/users/migrate_to_remnawave', methods=['GET'])
    async def migrate_to_remnawave_page():
        """Страница миграции пользователей в Remnawave"""
        return await render_template('migrate_to_remnawave.html')

    @admin_bp_instance.route('/api/users/migrate_to_remnawave/start', methods=['POST'])
    async def migrate_to_remnawave_start():
        """Запуск миграции пользователей в Remnawave"""
        try:
            from remnawave_manager import remnawave_manager_instance
            from datetime import datetime, timezone, timedelta
            import re
            import asyncio
            
            current_app.logger.info('[MIGRATION] Запрос на запуск миграции получен')
            
            if _migration_state['status'] == 'running':
                current_app.logger.warning('[MIGRATION] Попытка запуска уже запущенной миграции')
                return jsonify({'error': 'Миграция уже запущена'}), 400
            
            # Сбрасываем состояние
            _migration_state.update({
                'status': 'running',
                'total': 0,
                'processed': 0,
                'created': 0,
                'skipped': 0,
                'failed': 0,
                'results': {'created': [], 'skipped': [], 'failed': []},
                'stop_requested': False
            })
            
            current_app.logger.info('[MIGRATION] Состояние миграции сброшено, запускаем задачу')
            
            # Запускаем миграцию в фоне
            async def run_migration():
                MAX_ERRORS_TO_STORE = 100  # Максимальное количество ошибок для хранения
                try:
                    current_app.logger.info('[MIGRATION] Начало миграции пользователей в Remnawave')
                    
                    # Диагностика: проверяем общее количество пользователей
                    try:
                        total_users_row = await async_query_db("SELECT COUNT(*) as cnt FROM users", (), one=True)
                        users_with_uuid_row = await async_query_db("SELECT COUNT(*) as cnt FROM users WHERE xui_client_uuid IS NOT NULL AND xui_client_uuid != '' AND trim(xui_client_uuid) != ''", (), one=True)
                        total_users_count = total_users_row['cnt'] if total_users_row else 0
                        users_with_uuid_count = users_with_uuid_row['cnt'] if users_with_uuid_row else 0
                        current_app.logger.info(f'[MIGRATION] Диагностика: всего пользователей={total_users_count}, с UUID={users_with_uuid_count}')
                    except Exception as diag_error:
                        current_app.logger.warning(f'[MIGRATION] Ошибка диагностики: {diag_error}')
                    
                    # Получаем всех пользователей с непустым UUID (независимо от статуса подписки и subscription_provider)
                    # Проверку существования в Remnawave делаем при обработке каждого пользователя
                    try:
                        users = await async_query_db("""
                            SELECT telegram_id, xui_client_uuid, xui_client_email, subscription_end_date, limit_ip, is_trial_used, subscription_provider
                            FROM users 
                            WHERE xui_client_uuid IS NOT NULL
                            AND xui_client_uuid != ''
                            AND trim(xui_client_uuid) != ''
                            ORDER BY telegram_id
                        """)
                        current_app.logger.info(f'[MIGRATION] SQL запрос выполнен, получено строк: {len(users) if users else 0}')
                        if users and len(users) > 0:
                            current_app.logger.info(f'[MIGRATION] Пример первого пользователя: ID={users[0].get("telegram_id")}, UUID={users[0].get("xui_client_uuid")[:20] if users[0].get("xui_client_uuid") else "None"}..., provider={users[0].get("subscription_provider")}')
                    except Exception as sql_error:
                        current_app.logger.error(f'[MIGRATION] Ошибка SQL запроса: {sql_error}', exc_info=True)
                        _migration_state['status'] = 'stopped'
                        _migration_state['results']['failed'].append(f"Ошибка SQL: {str(sql_error)[:200]}")
                        return
                    
                    _migration_state['total'] = len(users) if users else 0
                    
                    if not users or len(users) == 0:
                        _migration_state['status'] = 'completed'
                        current_app.logger.info('[MIGRATION] Нет пользователей для миграции')
                        return
                    
                    current_app.logger.info(f'[MIGRATION] Найдено {len(users)} пользователей для миграции')
                    
                    # Настройки Remnawave
                    await app_conf.load_settings()
                    traffic_limit_gb = app_conf.get('remnawave_default_traffic_limit_gb', 0)
                    try:
                        traffic_limit_gb = int(traffic_limit_gb) if traffic_limit_gb else 0
                    except (ValueError, TypeError):
                        traffic_limit_gb = 0
                    
                    internal_squad_uuid = app_conf.get('remnawave_default_internal_squad_uuid')
                    
                    # Обрабатываем пользователей параллельно для ускорения
                    # Используем семафор для ограничения параллелизма (20 одновременных запросов)
                    semaphore = asyncio.Semaphore(20)
                    
                    async def process_user(user):
                        """Обработка одного пользователя"""
                        async with semaphore:  # Ограничиваем параллелизм
                            if _migration_state['stop_requested']:
                                return
                            
                            telegram_id = user['telegram_id']
                            xui_client_uuid = user['xui_client_uuid']
                            xui_client_email = user['xui_client_email'] or ''
                            
                            # Пропускаем пользователей с пустым UUID
                            if not xui_client_uuid or not xui_client_uuid.strip():
                                _migration_state['skipped'] += 1
                                if len(_migration_state['results']['skipped']) < 100:
                                    _migration_state['results']['skipped'].append(f"ID: {telegram_id} (пустой UUID)")
                                _migration_state['processed'] += 1
                                return
                            
                            # Обрабатываем дату окончания подписки
                            try:
                                sub_end_str = user['subscription_end_date']
                                if isinstance(sub_end_str, str):
                                    # Пробуем разные форматы даты
                                    try:
                                        subscription_end_date = datetime.fromisoformat(sub_end_str.replace('Z', '+00:00'))
                                    except ValueError:
                                        try:
                                            subscription_end_date = datetime.strptime(sub_end_str, '%Y-%m-%d %H:%M:%S')
                                        except ValueError:
                                            subscription_end_date = datetime.strptime(sub_end_str, '%Y-%m-%d %H:%M:%S.%f')
                                else:
                                    subscription_end_date = sub_end_str
                            except Exception as e:
                                _migration_state['failed'] += 1
                                if len(_migration_state['results']['failed']) < MAX_ERRORS_TO_STORE:
                                    _migration_state['results']['failed'].append(f"ID: {telegram_id} - ошибка даты: {str(e)[:50]}")
                                _migration_state['processed'] += 1
                                return
                            
                            limit_ip = user.get('limit_ip', 0) or 0
                            is_trial = bool(user.get('is_trial_used', 0))
                            
                            try:
                                # Генерируем username для Remnawave (извлекаем из email, как при создании подписки)
                                remnawave_username = f"tg{telegram_id}"  # По умолчанию
                                if xui_client_email and '@' in xui_client_email:
                                    email_username = xui_client_email.split('@')[0]
                                    # Проверяем паттерн Remnawave (только латинские буквы, цифры, подчеркивания и дефисы)
                                    username_pattern = re.compile(r'^[a-zA-Z0-9_-]+$')
                                    if username_pattern.match(email_username):
                                        remnawave_username = email_username
                                        current_app.logger.debug(f'[MIGRATION] Username для Remnawave извлечен из email: {remnawave_username} (ID: {telegram_id})')
                                    else:
                                        current_app.logger.debug(f'[MIGRATION] Username из email "{email_username}" не соответствует паттерну Remnawave, используем "tg{telegram_id}" (ID: {telegram_id})')
                                else:
                                    # Если email пустой или нет @, используем стандартный формат
                                    remnawave_username = f"tg{telegram_id}"
                                
                                # Вычисляем дни до окончания подписки
                                now = datetime.now(timezone.utc)
                                if subscription_end_date.tzinfo is None:
                                    subscription_end_date = subscription_end_date.replace(tzinfo=timezone.utc)
                                
                                days_left = (subscription_end_date - now).days
                                if days_left <= 0:
                                    days_left = 1  # Минимум 1 день для истекших подписок
                                
                                # Вычисляем базовое время для синхронизации даты окончания с X-UI
                                # Используем текущее время минус дни до окончания, чтобы получить исходную дату начала подписки
                                # Это гарантирует, что дата окончания в Remnawave будет совпадать с датой в БД
                                base_expiry_time = subscription_end_date - timedelta(days=days_left)
                                current_app.logger.debug(f'[MIGRATION] Базовое время для синхронизации: {base_expiry_time}, дата окончания: {subscription_end_date}, дней: {days_left} (ID: {telegram_id})')
                                
                                # Проверяем, существует ли пользователь в Remnawave
                                user_exists = False
                                try:
                                    existing_user = await remnawave_manager_instance._sdk.users.get_user_by_uuid(xui_client_uuid)
                                    if existing_user:
                                        user_exists = True
                                except Exception:
                                    try:
                                        existing_user = await remnawave_manager_instance._sdk.users.get_user_by_short_uuid(xui_client_uuid)
                                        if existing_user:
                                            user_exists = True
                                    except Exception:
                                        pass
                                
                                if user_exists:
                                    _migration_state['skipped'] += 1
                                    if len(_migration_state['results']['skipped']) < 100:
                                        _migration_state['results']['skipped'].append(f"ID: {telegram_id} (уже существует)")
                                else:
                                    # ВАЖНО: Используем один UUID для всех полей (xui_client_uuid, remnawave_short_uuid, remnawave_user_uuid)
                                    # Нормализуем UUID (приводим к нижнему регистру)
                                    normalized_uuid = str(xui_client_uuid).strip().lower()
                                    
                                    # Создаем пользователя в Remnawave с заранее подготовленным UUID
                                    # Передаем base_expiry_time для синхронизации даты окончания с X-UI
                                    remnawave_user_data = await remnawave_manager_instance.create_user(
                                        telegram_id=telegram_id,
                                        username=remnawave_username,
                                        days_valid=days_left,
                                        total_gb=traffic_limit_gb,
                                        description=f"Telegram ID: {telegram_id}",
                                        internal_squad_uuid=internal_squad_uuid,
                                        short_uuid=normalized_uuid,  # Используем xui_client_uuid как short_uuid
                                        user_uuid=normalized_uuid,  # Используем тот же UUID как полный UUID пользователя
                                        base_expiry=base_expiry_time  # Передаем базовое время для синхронизации с X-UI
                                    )
                                    
                                    if remnawave_user_data and remnawave_user_data.get("uuid"):
                                        # Обновляем БД с одним UUID для всех полей
                                        # ВАЖНО: используем наш заранее подготовленный UUID для всех трех полей
                                        await db_helpers.update_user_subscription_remnawave(
                                            telegram_id=telegram_id,
                                            remnawave_user_uuid=normalized_uuid,  # Используем тот же UUID
                                            remnawave_username=remnawave_username,
                                            remnawave_short_uuid=normalized_uuid,  # Используем тот же UUID
                                            subscription_end_date=subscription_end_date,
                                            is_trial=is_trial,
                                            preserve_xui_uuid=True,  # Сохраняем xui_client_uuid (он уже равен normalized_uuid)
                                            migration_mode=True  # Режим миграции: не изменяем is_trial_used и флаги уведомлений
                                        )
                                        
                                        _migration_state['created'] += 1
                                        if len(_migration_state['results']['created']) < 100:
                                            _migration_state['results']['created'].append(f"ID: {telegram_id}")
                                    else:
                                        raise Exception("Не удалось создать пользователя в Remnawave: ответ пустой")
                                
                                _migration_state['processed'] += 1
                            
                            except Exception as e:
                                _migration_state['failed'] += 1
                                error_msg = f"ID: {telegram_id} - {str(e)[:100]}"
                                if len(_migration_state['results']['failed']) < MAX_ERRORS_TO_STORE:
                                    _migration_state['results']['failed'].append(error_msg)
                                _migration_state['processed'] += 1
                    
                    # Обрабатываем пользователей батчами по 200 для лучшей производительности
                    batch_size = 200
                    total_users = len(users) if users else 0
                    for batch_start in range(0, total_users, batch_size):
                        if _migration_state['stop_requested']:
                            _migration_state['status'] = 'stopped'
                            break
                        
                        batch = users[batch_start:batch_start + batch_size]
                        # Обрабатываем батч параллельно
                        await asyncio.gather(*[process_user(user) for user in batch], return_exceptions=True)
                        
                        # Логируем прогресс каждые 1000 пользователей
                        if (batch_start + batch_size) % 1000 == 0 or batch_start + batch_size >= total_users:
                            current_app.logger.info(f'[MIGRATION] Прогресс: обработано {_migration_state["processed"]}/{total_users}, создано: {_migration_state["created"]}, пропущено: {_migration_state["skipped"]}, ошибок: {_migration_state["failed"]}')
                    
                    _migration_state['status'] = 'completed'
                    current_app.logger.info(f'[MIGRATION] Миграция завершена: создано={_migration_state["created"]}, пропущено={_migration_state["skipped"]}, ошибок={_migration_state["failed"]}')
                    
                except Exception as e:
                    current_app.logger.error(f'[MIGRATION] Критическая ошибка миграции: {e}', exc_info=True)
                    _migration_state['status'] = 'stopped'
                    _migration_state['results']['failed'].append(f"Критическая ошибка: {str(e)[:200]}")
        
            # Запускаем миграцию в фоне
            try:
                # Получаем event loop
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                
                # Создаем задачу
                task = loop.create_task(run_migration())
                current_app.logger.info('[MIGRATION] Задача миграции создана и запущена в фоне')
                
                # Даем задаче немного времени на старт
                await asyncio.sleep(0.1)
                
                return jsonify({'success': True, 'message': 'Миграция запущена'})
            except Exception as task_error:
                error_msg = str(task_error)
                current_app.logger.error(f'[MIGRATION] Ошибка запуска задачи миграции: {task_error}', exc_info=True)
                _migration_state['status'] = 'stopped'
                return jsonify({'error': f'Ошибка запуска миграции: {error_msg}'}), 500
                
        except Exception as e:
            error_msg = str(e)
            current_app.logger.error(f'[MIGRATION] Ошибка в обработчике запуска миграции: {e}', exc_info=True)
            _migration_state['status'] = 'stopped'
            return jsonify({'error': f'Ошибка запуска миграции: {error_msg}'}), 500

    @admin_bp_instance.route('/api/users/migrate_to_remnawave/status', methods=['GET'])
    async def migrate_to_remnawave_status():
        """Получение статуса миграции"""
        
        # Если миграция еще не запускалась, показываем предварительную статистику
        # Всегда обновляем счетчик при статусе idle, чтобы показывать актуальное количество
        if _migration_state['status'] == 'idle':
            try:
                # Сначала проверяем общее количество пользователей
                total_all = await async_query_db("SELECT COUNT(*) as cnt FROM users", (), one=True)
                total_all_count = total_all['cnt'] if total_all else 0
                
                # Проверяем пользователей с UUID
                users_with_uuid = await async_query_db("""
                    SELECT COUNT(*) as cnt
                    FROM users 
                    WHERE xui_client_uuid IS NOT NULL
                """, (), one=True)
                with_uuid_count = users_with_uuid['cnt'] if users_with_uuid else 0
                
                # Проверяем пользователей с непустым UUID
                users_count = await async_query_db("""
                    SELECT COUNT(*) as cnt
                    FROM users 
                    WHERE xui_client_uuid IS NOT NULL
                    AND xui_client_uuid != ''
                    AND trim(xui_client_uuid) != ''
                """, (), one=True)
                count = users_count['cnt'] if users_count else 0
                
                _migration_state['total'] = count
                current_app.logger.info(f'[MIGRATION] Предварительная статистика: всего пользователей={total_all_count}, с UUID (включая NULL)={with_uuid_count}, с непустым UUID={count}')
                
                # Если count = 0, но есть пользователи, логируем примеры
                if count == 0 and total_all_count > 0:
                    sample_users = await async_query_db("""
                        SELECT telegram_id, xui_client_uuid, subscription_provider
                        FROM users 
                        LIMIT 5
                    """, ())
                    if sample_users:
                        current_app.logger.warning(f'[MIGRATION] Примеры пользователей из БД: {[(u.get("telegram_id"), u.get("xui_client_uuid"), u.get("subscription_provider")) for u in sample_users]}')
            except Exception as e:
                current_app.logger.error(f'[MIGRATION] Ошибка подсчета пользователей для предварительной статистики: {e}', exc_info=True)
                _migration_state['total'] = 0
        
        return jsonify(_migration_state)

    @admin_bp_instance.route('/api/users/migrate_to_remnawave/stop', methods=['POST'])
    async def migrate_to_remnawave_stop():
        """Остановка миграции"""
        _migration_state['stop_requested'] = True
        return jsonify({'success': True, 'message': 'Запрос на остановку миграции отправлен'})

