from quart import Blueprint, jsonify, request, session, current_app
import asyncio
import ipaddress
import json
import math
import os
import re
import sys
import time
import traceback
import aiofiles
import httpx
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, quote_plus

from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger
from config import DATABASE_NAME as ROOT_DB_PATH
from app_config import app_conf
from tg_sender import send_telegram_message, send_telegram_photo
from remnawave_manager import remnawave_manager_instance, normalize_remnawave_node_uuid
from web_admin.async_db import async_query_db, async_execute_db, get_table_columns_cached
from web_admin.core.user_limit import update_user_device_limit
from web_admin.core.dates import subscription_end_to_utc_iso
from subscription_manager import grant_subscription
# get_devices_by_uuid / get_latest_access_by_uuid импортируются локально в api_user_security (fallback)


def _host_is_ip(host: str) -> bool:
    if not host or ':' in host:
        host = host.split(':')[0] if host else ''
    return bool(re.match(r'^\d+\.\d+\.\d+\.\d+$', host))


api_bp = Blueprint('api', __name__)
@api_bp.before_request
async def _api_require_login():
    try:
        if _host_is_ip(request.host or ''):
            return jsonify({'error': 'forbidden', 'message': 'Access by IP is not allowed'}), 403
        # IP-whitelist (см. web_admin/core/ip_whitelist.py).
        # На уровне API возвращаем 404, чтобы не палить наличие админки.
        try:
            from web_admin.core.ip_whitelist import (
                get_whitelist_state, resolve_client_ip,
                is_ip_allowed, is_loopback,
            )
            wl_enabled, wl_networks, _ = await get_whitelist_state()
            if wl_enabled:
                client_ip = resolve_client_ip(request)
                if not is_loopback(client_ip) and not is_ip_allowed(client_ip, wl_networks):
                    return '', 404
        except Exception:
            # Любая ошибка whitelist-логики не должна валить /api/.
            pass
        if not session.get('admin_user_id'):
            return jsonify({'error': 'unauthorized'}), 401
    except Exception:
        return jsonify({'error': 'unauthorized'}), 401



def _get_database_path() -> str:
    # Абсолютный путь к БД из корня проекта
    return ROOT_DB_PATH


async def _load_servers_from_settings_async() -> list:
    try:
        row = await async_query_db("SELECT value FROM settings WHERE key = 'xui_servers'", (), one=True)
        return json.loads(row['value']) if row and row.get('value') else []
    except Exception:
        return []


# --- IP info via 2ip.ru ---
_IPINFO_CACHE: dict[str, dict] = {}
_IPINFO_TTL_SECONDS = 6 * 60 * 60  # 6 часов

# Кэш последних значений трафика по серверам для вычисления скоростей (netIO) по дельте
_NET_TRAFFIC_CACHE: dict[int, dict] = {}

async def _get_setting_value_async(key: str) -> str | None:
    row = await async_query_db("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    return row['value'] if row and row.get('value') else None

def _normalize_2ip_response(raw: dict) -> dict:
    # Нормализуем ответ к единому виду
    if not isinstance(raw, dict):
        return {}
    # По докам 2ip.io основные поля: ip, city, country, country_code, latitude, longitude, timezone, asn, organization, etc.
    asn_block = raw.get('asn') if isinstance(raw.get('asn'), dict) else {}
    lat = raw.get('latitude') or raw.get('lat')
    lon = raw.get('longitude') or raw.get('lon') or raw.get('lng')
    country_code = raw.get('country_code') or raw.get('code') or raw.get('country_code2')
    country_name = raw.get('country') or raw.get('country_name')
    tz = raw.get('timezone') or raw.get('time_zone')
    emoji = raw.get('emoji') or ''
    return {
        'ip': raw.get('ip') or raw.get('query') or raw.get('ipAddress'),
        'city': raw.get('city') or '',
        'region': raw.get('region') or raw.get('regionName') or '',
        'lat': str(lat) if lat is not None else '',
        'lon': str(lon) if lon is not None else '',
        'country': country_name or '',
        'code': (country_code or '').upper(),
        'emoji': emoji,
        'timezone': tz or '',
        'asn': {
            'id': str(asn_block.get('id') or asn_block.get('asn') or ''),
            'name': asn_block.get('name') or asn_block.get('org') or '',
            'hosting': bool(asn_block.get('hosting')) if 'hosting' in asn_block else False,
        }
    }

async def _fetch_2ip(ip: str, token: str | None) -> dict | None:
    # Согласно документации: https://api.2ip.io/{IP}?token=...&lang=ru
    # Пример: https://api.2ip.io/178.248.86.435?token=XXXX&lang=ru
    base = 'https://api.2ip.io/'
    # Без токена API возвращает сильно ограниченные данные или может не работать — попробуем всё равно
    params = {'lang': 'ru'}
    if token:
        params['token'] = token
    timeout = httpx.Timeout(6.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        url = base + ip
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    return _normalize_2ip_response(data)
            # При превышении лимита 429 — не валим, но кэшируем пустое, чтобы не спамить
            if resp.status_code == 429:
                return None
        except Exception:
            return None
    return None

@api_bp.route('/ip_lookup')
async def api_ip_lookup():
    ip = (request.args.get('ip') or '').strip()
    if not ip:
        return jsonify({'ok': False, 'error': 'missing_ip'}), 400
    try:
        ipaddress.ip_address(ip)
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid_ip'}), 400
    now_ts = int(time.time())
    cached = _IPINFO_CACHE.get(ip)
    if cached and (now_ts - cached.get('ts', 0) <= _IPINFO_TTL_SECONDS):
        return jsonify({'ok': True, 'ip': ip, 'info': cached.get('data')})
    token = await _get_setting_value_async('web_2ip_token')
    if not token:
        # Нет токена — возвращаем только IP, без ошибки, чтобы фронт просто показывал IP
        _IPINFO_CACHE[ip] = {'data': None, 'ts': now_ts}
        return jsonify({'ok': True, 'ip': ip, 'info': None, 'no_token': True})
    try:
        data = await _fetch_2ip(ip, token)
        _IPINFO_CACHE[ip] = {'data': data, 'ts': now_ts}
        return jsonify({'ok': True, 'ip': ip, 'info': data})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


async def _fetch_server_statuses(servers: list, timeout: int = 10) -> list:
    return []



@api_bp.route('/server_statuses')
async def api_server_statuses():
    servers_list = await _load_servers_from_settings_async()
    try:
        statuses = await _fetch_server_statuses(servers_list, timeout=10)
        return jsonify({'servers': statuses})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api_bp.route('/online_clients_total')
async def api_online_clients_total():
    """Возвращает общее количество онлайн клиентов по всем доступным сервисам (3X-UI и Remnawave)"""
    try:
        total_online = 0
        xui_online = 0
        remnawave_online = 0
        xui_available = False
        remnawave_available = False
        
        # 3X-UI удалён — онлайн только Remnawave.
        xui_online = 0
        xui_available = False
        
        # Получаем онлайн клиентов с Remnawave (только если включен)
        try:
            remnawave_enabled = app_conf.get('remnawave_enabled', False)
            
            if remnawave_enabled:
                system_stats = await remnawave_manager_instance.get_system_stats()
                if system_stats and 'online_stats' in system_stats:
                    remnawave_online = system_stats['online_stats'].get('online_now', 0) or 0
                    remnawave_available = True
        except Exception as e:
            current_app.logger.warning(f"Ошибка получения онлайн клиентов Remnawave: {e}")
        
        # Суммируем только если сервисы доступны
        if xui_available:
            total_online += xui_online
        if remnawave_available:
            total_online += remnawave_online
        
        return jsonify({
            'ok': True,
            'total_online': total_online,
            'xui_online': xui_online if xui_available else None,
            'remnawave_online': remnawave_online if remnawave_available else None,
            'xui_available': xui_available,
            'remnawave_available': remnawave_available
        })
    except Exception as e:
        current_app.logger.error(f"Ошибка получения общего количества онлайн клиентов: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


# --- Новый endpoint: статус одного сервера (для быстрого инкрементального обновления карточек) ---
async def _fetch_one_server_status(server_conf: dict, timeout: int = 10) -> dict:
    return {
        'id': server_conf.get('id'),
        'name': server_conf.get('name'),
        'status': False,
        'timeout': False,
        'error': '3X-UI removed',
        'ping_ms': None,
        'stats': {},
    }



@api_bp.route('/server_status/<int:server_id>')
async def api_server_status(server_id: int):
    try:
        timeout = int((request.args.get('timeout') or 10))
    except Exception:
        timeout = 10
    servers = await _load_servers_from_settings_async()
    server = next((s for s in servers if s.get('id') == server_id), None)
    if not server:
        return jsonify({'error': 'server_not_found'}), 404
    try:
        # Специальная обработка виртуального сервера-балансировщика
        if server.get('type') == 'balancer':
            try:
                member_ids = [int(x) for x in (server.get('members') or []) if isinstance(x, (int, str))]
            except Exception:
                member_ids = []
            # Также учитываем якорный сервер
            try:
                parent_id = int(server.get('parent_id') or 0)
            except Exception:
                parent_id = 0
            ids = list({*[m for m in member_ids if isinstance(m, int) and m > 0], *( [parent_id] if parent_id else [] )})
            members = [s for s in servers if s.get('id') in ids]
            # Параллельно собираем статусы участников (короткий таймаут)
            async def _gather_members(arr: list[dict]):
                tasks = [_fetch_one_server_status(cfg, timeout=min(timeout, 8)) for cfg in arr]
                return await asyncio.gather(*tasks, return_exceptions=False)
            try:
                results = await _gather_members(members) if members else []
            except Exception:
                results = []
            any_ok = any(r.get('status') for r in results)
            # Агрегированные метрики
            total_online = sum(int(r.get('stats', {}).get('online_users') or 0) for r in results)
            # Убрано: active_users - загрузка всего inbound с 40k клиентов слишком медленная
            # total_clients = sum(int(r.get('stats', {}).get('active_users') or 0) for r in results)
            # Вычисляем средний пинг среди работающих серверов
            working_pings = [r.get('ping_ms') for r in results if r.get('status') and r.get('ping_ms') is not None]
            avg_ping_ms = round(sum(working_pings) / len(working_pings), 0) if working_pings else None
            # Формируем ответ
            agg = {
                'id': server_id,
                'name': server.get('name'),
                'status': any_ok,
                'timeout': False,
                'ping_ms': avg_ping_ms,
                'error': None if any_ok else 'no members up',
                'stats': {
                    'online_users': total_online,
                    # 'active_users': total_clients,  # Убрано
                    'members': [{ 'id': r.get('id'), 'name': r.get('name'), 'status': r.get('status') } for r in results]
                }
            }
            return jsonify(agg)
        # Обычный сервер
        result = await _fetch_one_server_status(server, timeout=timeout)
        return jsonify(result)
    except Exception as e:
        return jsonify({'id': server_id, 'status': False, 'timeout': False, 'error': str(e), 'ping_ms': None, 'stats': {}}), 200


@api_bp.route('/all_user_ids')
async def api_all_user_ids():
    # Проверяем наличие колонки is_blocked
    column_names = await get_table_columns_cached('users')
    has_is_blocked = 'is_blocked' in column_names
    if has_is_blocked:
        rows = await async_query_db("SELECT telegram_id FROM users WHERE COALESCE(is_blocked, 0) = 0")
    else:
        rows = await async_query_db("SELECT telegram_id FROM users")
    return jsonify({"user_ids": [r['telegram_id'] for r in rows]})


# --- Nginx access log tail (/var/log/nginx/access.log) ---
async def _tail_lines_async(filepath: str, max_lines: int = 200, max_bytes: int = 400_000) -> list[str]:
    try:
        size = os.path.getsize(filepath)  # os.path.getsize быстрый, можно оставить
        async with aiofiles.open(filepath, 'rb') as f:
            if size > max_bytes:
                await f.seek(-max_bytes, os.SEEK_END)
            data = await f.read()
        try:
            text = data.decode('utf-8', errors='ignore')
        except Exception:
            text = data.decode('latin-1', errors='ignore')
        lines = text.strip().splitlines()
        return lines[-max_lines:]
    except Exception:
        return []
## Removed temporary API for SBP-only toggle; now handled via settings form

@api_bp.route('/expired_user_ids')
async def api_expired_user_ids():
    column_names = await get_table_columns_cached('users')
    has_is_blocked = 'is_blocked' in column_names
    # Истекшие: есть дата окончания и она <= сейчас (используем UTC время)
    now_utc_str = datetime.now(timezone.utc).isoformat()
    if has_is_blocked:
        q = (
            "SELECT telegram_id FROM users "
            "WHERE subscription_end_date IS NOT NULL "
            "AND subscription_end_date <= ? "
            "AND COALESCE(is_blocked, 0) = 0"
        )
    else:
        q = (
            "SELECT telegram_id FROM users "
            "WHERE subscription_end_date IS NOT NULL "
            "AND subscription_end_date <= ?"
        )
    rows = await async_query_db(q, (now_utc_str,))
    return jsonify({"user_ids": [r['telegram_id'] for r in rows]})

@api_bp.route('/expired_no_trial_user_ids')
async def api_expired_no_trial_user_ids():
    column_names = await get_table_columns_cached('users')
    has_is_blocked = 'is_blocked' in column_names
    # Истекшие без использованного доп. триала: subscription_end_date <= now И free_renewal_used = 0 (или NULL)
    now_utc_str = datetime.now(timezone.utc).isoformat()
    if has_is_blocked:
        q = (
            "SELECT telegram_id FROM users "
            "WHERE subscription_end_date IS NOT NULL "
            "AND subscription_end_date <= ? "
            "AND COALESCE(free_renewal_used, 0) = 0 "
            "AND COALESCE(is_blocked, 0) = 0"
        )
    else:
        q = (
            "SELECT telegram_id FROM users "
            "WHERE subscription_end_date IS NOT NULL "
            "AND subscription_end_date <= ? "
            "AND COALESCE(free_renewal_used, 0) = 0"
        )
    rows = await async_query_db(q, (now_utc_str,))
    return jsonify({"user_ids": [r['telegram_id'] for r in rows]})

@api_bp.route('/active_user_ids')
async def api_active_user_ids():
    column_names = await get_table_columns_cached('users')
    has_is_blocked = 'is_blocked' in column_names
    # Активные: подписка действует в данный момент (subscription_end_date > now_utc)
    now_utc_str = datetime.now(timezone.utc).isoformat()
    if has_is_blocked:
        q = (
            "SELECT telegram_id FROM users "
            "WHERE subscription_end_date IS NOT NULL "
            "AND subscription_end_date > ? "
            "AND COALESCE(is_blocked, 0) = 0"
        )
    else:
        q = (
            "SELECT telegram_id FROM users "
            "WHERE subscription_end_date IS NOT NULL "
            "AND subscription_end_date > ?"
        )
    rows = await async_query_db(q, (now_utc_str,))
    return jsonify({"user_ids": [r['telegram_id'] for r in rows]})

@api_bp.route('/check_active_registrations')
async def api_check_active_registrations():
    """Проверяет, есть ли пользователи без UUID, созданные в последние 2 минуты
    (т.е. потенциально находящиеся в процессе регистрации). Блокировать удаление
    нужно только если такие реально есть, а не любые регистрации за окно."""

    try:
        time_window_minutes = 2
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=time_window_minutes)).isoformat()

        column_names = await get_table_columns_cached('users')
        has_is_blocked = 'is_blocked' in column_names

        # Считаем только тех, кто без UUID и зарегистрирован недавно — именно они
        # могут "прямо сейчас" дойти до получения UUID и попасть под удаление.
        if has_is_blocked:
            q = (
                "SELECT COUNT(*) as count FROM users "
                "WHERE created_at >= ? "
                "AND (uuid IS NULL OR uuid = '') "
                "AND COALESCE(is_blocked, 0) = 0"
            )
        else:
            q = (
                "SELECT COUNT(*) as count FROM users "
                "WHERE created_at >= ? "
                "AND (uuid IS NULL OR uuid = '')"
            )

        result = await async_query_db(q, (cutoff,), one=True)
        count = result['count'] if result else 0

        return jsonify({
            "has_active_registrations": count > 0,
            "registrations_count": count,
            "time_window_minutes": time_window_minutes
        })
    except Exception as e:
        current_app.logger.error(f"Ошибка проверки активных регистраций: {e}")
        # При ошибке — оставляем строгий режим (блок), но это легко увидеть в UI.
        return jsonify({
            "has_active_registrations": True,
            "error": str(e)
        }), 500

# ⚠️ ОПАСНЫЕ ОПЕРАЦИИ
# Эндпоинт безвозвратно удаляет из users записи с пустым xui_client_uuid.
# Есть встроенные предохранители: отмена при регистрациях за последние 5 минут
# и пропуск пользователей со связанными данными (платежи, промокоды, ошибки
# восстановления, партнёрские начисления). Тем не менее операция необратима —
# при изменениях сохранять эти проверки и ограничение по роли (только админ).
@api_bp.route('/delete_empty_uuid_users', methods=['POST'])
async def api_delete_empty_uuid_users():
    """Удаляет пользователей с пустым UUID с проверкой активных регистраций и связанных данных"""
    
    try:
        # Проверяем активные регистрации
        five_minutes_ago = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        
        column_names = await get_table_columns_cached('users')
        has_is_blocked = 'is_blocked' in column_names
        
        if has_is_blocked:
            check_q = (
                "SELECT COUNT(*) as count FROM users "
                "WHERE created_at >= ? "
                "AND COALESCE(is_blocked, 0) = 0"
            )
        else:
            check_q = (
                "SELECT COUNT(*) as count FROM users "
                "WHERE created_at >= ?"
            )
        
        check_result = await async_query_db(check_q, (five_minutes_ago,), one=True)
        recent_registrations = check_result['count'] if check_result else 0
        
        if recent_registrations > 0:
            return jsonify({
                "success": False,
                "error": f"Обнаружены активные регистрации ({recent_registrations} за последние 5 минут). Операция отменена для безопасности."
            }), 400
        
        # Получаем список пользователей без UUID для удаления
        if has_is_blocked:
            users_to_delete = await async_query_db(
                "SELECT telegram_id FROM users WHERE (xui_client_uuid IS NULL OR xui_client_uuid = '') AND COALESCE(is_blocked, 0) = 0",
                ()
            )
        else:
            users_to_delete = await async_query_db(
                "SELECT telegram_id FROM users WHERE xui_client_uuid IS NULL OR xui_client_uuid = ''",
                ()
            )
        
        if not users_to_delete:
            return jsonify({
                "success": True,
                "deleted_count": 0,
                "skipped_count": 0,
                "message": "Нет пользователей для удаления"
            })
        
        deleted_count = 0
        skipped_count = 0
        skipped_reasons = []
        
        # Удаляем пользователей по одному, проверяя связанные данные
        for user_row in users_to_delete:
            telegram_id = user_row['telegram_id']
            skip_reason = None
            
            try:
                # Проверяем наличие связанных данных
                # 1. Проверяем payments
                payments_count = await async_query_db(
                    "SELECT COUNT(*) as count FROM payments WHERE telegram_id = ?",
                    (telegram_id,), one=True
                )
                if payments_count and payments_count.get('count', 0) > 0:
                    skip_reason = f"Есть платежи ({payments_count['count']})"
                
                # 2. Проверяем promo_codes (активированные этим пользователем)
                if not skip_reason:
                    promo_count = await async_query_db(
                        "SELECT COUNT(*) as count FROM promo_codes WHERE activated_by_telegram_id = ?",
                        (telegram_id,), one=True
                    )
                    if promo_count and promo_count.get('count', 0) > 0:
                        skip_reason = f"Активировал промокоды ({promo_count['count']})"
                
                # 3. Проверяем client_recreation_errors
                if not skip_reason:
                    errors_count = await async_query_db(
                        "SELECT COUNT(*) as count FROM client_recreation_errors WHERE telegram_id = ?",
                        (telegram_id,), one=True
                    )
                    if errors_count and errors_count.get('count', 0) > 0:
                        skip_reason = f"Есть ошибки восстановления ({errors_count['count']})"
                
                # 4. Проверяем partner_accruals (как партнер или плательщик)
                if not skip_reason:
                    partner_count = await async_query_db(
                        "SELECT COUNT(*) as count FROM partner_accruals WHERE partner_id = ? OR payer_id = ?",
                        (telegram_id, telegram_id), one=True
                    )
                    if partner_count and partner_count.get('count', 0) > 0:
                        skip_reason = f"Есть партнерские начисления ({partner_count['count']})"
                
                # 5. Проверяем, не является ли пользователь пригласившим других (invited_by)
                if not skip_reason:
                    referrals_count = await async_query_db(
                        "SELECT COUNT(*) as count FROM users WHERE invited_by = ?",
                        (telegram_id,), one=True
                    )
                    if referrals_count and referrals_count.get('count', 0) > 0:
                        skip_reason = f"Пригласил других пользователей ({referrals_count['count']})"
                
                # Если нет причин для пропуска - удаляем
                if not skip_reason:
                    await async_execute_db(
                        "DELETE FROM referral_payment_bonus WHERE ref_user_id = ? OR invited_user_id = ?",
                        (telegram_id, telegram_id),
                    )
                    await async_execute_db("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
                    deleted_count += 1
                else:
                    skipped_count += 1
                    skipped_reasons.append({
                        "telegram_id": telegram_id,
                        "reason": skip_reason
                    })
                    
            except Exception as user_error:
                # Если возникла ошибка при удалении конкретного пользователя (например, foreign key constraint)
                skipped_count += 1
                error_msg = str(user_error)
                if "foreign key" in error_msg.lower() or "constraint" in error_msg.lower():
                    skip_reason = "Ошибка foreign key constraint"
                else:
                    skip_reason = f"Ошибка: {error_msg[:100]}"
                skipped_reasons.append({
                    "telegram_id": telegram_id,
                    "reason": skip_reason
                })
                current_app.logger.warning(f"[API] Не удалось удалить пользователя {telegram_id}: {user_error}")
        
        current_app.logger.info(f"[API] Удалено пользователей без UUID: {deleted_count}, пропущено: {skipped_count}")
        
        result = {
            "success": True,
            "deleted_count": deleted_count,
            "skipped_count": skipped_count,
            "total_found": len(users_to_delete)
        }
        
        # Добавляем информацию о пропущенных только если их немного (чтобы не перегружать ответ)
        if skipped_count > 0 and skipped_count <= 50:
            result["skipped_reasons"] = skipped_reasons[:50]  # Ограничиваем до 50 записей
        
        return jsonify(result)
        
    except Exception as e:
        current_app.logger.error(f"Ошибка удаления пользователей без UUID: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500




@api_bp.route('/users/<int:telegram_id>')
async def api_user_details(telegram_id: int):
    """API endpoint для получения детальной информации о пользователе в JSON формате."""
    try:
        
        # Проверяем наличие колонки is_blocked
        column_names = await get_table_columns_cached('users')
        has_is_blocked = 'is_blocked' in column_names
        
        # Получаем пользователя (включая show_partner_program_button, partner_percent_rub и free_renewal_used)
        if has_is_blocked:
            user_row = await async_query_db(
                "SELECT *, COALESCE(subscription_mode, 'multi') as subscription_mode, COALESCE(is_blocked, 0) as is_blocked, COALESCE(show_partner_program_button, '') as show_partner_program_button, COALESCE(partner_percent_rub, '') as partner_percent_rub, COALESCE(free_renewal_used, 0) as free_renewal_used FROM users WHERE telegram_id = ?",
                (telegram_id,), one=True
            )
        else:
            user_row = await async_query_db(
                "SELECT *, COALESCE(subscription_mode, 'multi') as subscription_mode, 0 as is_blocked, COALESCE(show_partner_program_button, '') as show_partner_program_button, COALESCE(partner_percent_rub, '') as partner_percent_rub, COALESCE(free_renewal_used, 0) as free_renewal_used FROM users WHERE telegram_id = ?",
                (telegram_id,), one=True
            )
        
        if not user_row:
            return jsonify({
                'ok': False,
                'error': f'Пользователь с ID {telegram_id} не найден'
            }), 404
        
        user = dict(user_row)
        
        # Получаем данные Remnawave если есть любые идентификаторы Remnawave
        remnawave_data = None
        try_fetch_remna = (
            user.get('xui_client_uuid')
            or user.get('remnawave_short_uuid')
            or user.get('remnawave_username')
        )
        if try_fetch_remna:
            try:
                await app_conf.load_settings()
                
                await remnawave_manager_instance._ensure_initialized()

                async def _to_dict(remnawave_user):
                    if not remnawave_user:
                        return None
                    data = {
                        'username': remnawave_user.username,
                        'short_uuid': remnawave_user.short_uuid,
                        'uuid': str(remnawave_user.uuid),
                        'email': remnawave_user.email,
                        'status': remnawave_user.status,
                        'expire_at': getattr(remnawave_user, 'expire_at', None)
                            and remnawave_user.expire_at.isoformat(),
                        'traffic_limit_bytes': getattr(remnawave_user, 'traffic_limit_bytes', None),
                        'traffic_limit_strategy': getattr(remnawave_user, 'traffic_limit_strategy', None),
                    }
                    ut = getattr(remnawave_user, 'user_traffic', None)
                    if ut:
                        data['user_traffic'] = {
                            'used_traffic_bytes': getattr(ut, 'used_traffic_bytes', None),
                            'lifetime_used_traffic_bytes': getattr(ut, 'lifetime_used_traffic_bytes', None),
                            'online_at': getattr(ut, 'online_at', None) and ut.online_at.isoformat(),
                            'first_connected_at': getattr(ut, 'first_connected_at', None) and ut.first_connected_at.isoformat(),
                            'last_connected_node_uuid': getattr(ut, 'last_connected_node_uuid', None),
                        }
                    squads = getattr(remnawave_user, 'active_internal_squads', None) or []
                    if squads:
                        data['active_internal_squads'] = []
                        for sq in squads:
                            data['active_internal_squads'].append({
                                'uuid': getattr(sq, 'internal_squad_uuid', None) or getattr(sq, 'uuid', None) or getattr(sq, 'id', None),
                                'name': getattr(sq, 'name', None),
                                'tag': getattr(sq, 'tag', None),
                            })
                    return data

                remnawave_user = None
                remnawave_errors = []

                async def _try_fetch(label, coro):
                    nonlocal remnawave_user
                    if remnawave_user is not None:
                        return
                    try:
                        candidate = await coro
                        if candidate:
                            remnawave_user = candidate
                    except Exception as e:
                        remnawave_errors.append(f"{label}: {e}")

                # 1) По UUID (совпадает с xui_client_uuid)
                if user.get('xui_client_uuid'):
                    await _try_fetch('uuid', remnawave_manager_instance._sdk.users.get_user_by_uuid(user['xui_client_uuid']))

                # 2) По short UUID
                if remnawave_user is None and user.get('remnawave_short_uuid'):
                    await _try_fetch('short_uuid', remnawave_manager_instance._sdk.users.get_user_by_short_uuid(user['remnawave_short_uuid']))

                # 3) По Telegram ID (список; берем первого активного либо первого)
                if remnawave_user is None and user.get('telegram_id'):
                    async def _by_tg():
                        users_list = await remnawave_manager_instance._sdk.users.get_users_by_telegram_id(str(user['telegram_id']))
                        if users_list:
                            return next((u for u in users_list if getattr(u, 'status', '') == 'active'), users_list[0])
                        return None
                    await _try_fetch('telegram_id', _by_tg())

                # 4) По email
                if remnawave_user is None and user.get('xui_client_email'):
                    async def _by_email():
                        users_list = await remnawave_manager_instance._sdk.users.get_users_by_email(user['xui_client_email'])
                        return users_list[0] if users_list else None
                    await _try_fetch('email', _by_email())

                # 5) По remnawave_username (если хранится)
                if remnawave_user is None and user.get('remnawave_username'):
                    await _try_fetch('remnawave_username', remnawave_manager_instance._sdk.users.get_user_by_username(user['remnawave_username']))

                remnawave_data = await _to_dict(remnawave_user)
                if remnawave_data:
                    ut_data = remnawave_data.get('user_traffic') or {}
                    node_uuid = ut_data.get('last_connected_node_uuid')
                    if node_uuid:
                        try:
                            all_nodes = await remnawave_manager_instance.get_all_nodes()
                            if all_nodes:
                                node_map = {
                                    normalize_remnawave_node_uuid(n.get('uuid')): n.get('name')
                                    for n in all_nodes if n.get('uuid')
                                }
                                node_name = node_map.get(normalize_remnawave_node_uuid(str(node_uuid)))
                                if node_name:
                                    remnawave_data['user_traffic']['last_connected_node_name'] = node_name
                        except Exception as node_err:
                            current_app.logger.debug(
                                f"[API] Не удалось разрешить имя ноды для {telegram_id}: {node_err}"
                            )
                # Если пользователь не найден, логируем ошибки на сервере, но возвращаем None
                if remnawave_data is None and remnawave_errors:
                    current_app.logger.debug(f"[API] Remnawave пользователь не найден для {telegram_id}. Попытки поиска: {remnawave_errors}")
                    remnawave_data = None  # Возвращаем None вместо объекта с ошибками
            except Exception as e:
                current_app.logger.warning(f"[API] Ошибка получения данных Remnawave для пользователя {telegram_id}: {e}\n{traceback.format_exc()}")
                remnawave_data = None  # При исключении тоже возвращаем None
        
        # Получаем платежи
        payments_rows = await async_query_db(
            "SELECT * FROM payments WHERE telegram_id = ? ORDER BY created_at DESC LIMIT 50",
            (telegram_id,)
        )
        payments_raw = [dict(p) for p in payments_rows] if payments_rows else []
        
        # Обрабатываем платежи аналогично payments.py
        tariff_ids = set()
        traffic_topup_tariff_ids = set()
        payments = []
        for p in payments_raw:
            metadata_json = p.get('metadata_json')
            tariff_info = None
            payment_method = None
            payment_type = None
            
            # Определяем метод платежа из payment_id или metadata
            payment_id = p.get('payment_id', '')
            if payment_id.startswith('WATA_'):
                payment_method = 'Wata'
            elif payment_id.startswith('PLATEGA_'):
                payment_method = 'Platega'
            elif payment_id.startswith('YOOMONEY_'):
                payment_method = 'YooMoney'
            elif payment_id.startswith('YK_') or 'yookassa' in payment_id.lower():
                payment_method = 'YooKassa'
            elif payment_id.startswith('CRYPTOBOT_') or payment_id.startswith('CRYPTO_'):
                payment_method = 'CryptoBot'
            elif payment_id.startswith('TGSTAR_'):
                payment_method = 'TG Star'
            elif payment_id.startswith('USDT_'):
                payment_method = 'USDT'
            
            if metadata_json:
                try:
                    metadata = json.loads(metadata_json)
                    payment_type = metadata.get('payment_type')
                    
                    # Если метод не определен из payment_id, пробуем из metadata
                    if not payment_method:
                        payment_method = metadata.get('payment_method') or metadata.get('cms_name')
                        if payment_method:
                            # Нормализуем название метода
                            payment_method_lower = str(payment_method).lower()
                            if 'wata' in payment_method_lower:
                                payment_method = 'Wata'
                            elif 'platega' in payment_method_lower or 'sbp' in payment_method_lower:
                                payment_method = 'Platega'
                            elif 'yookassa' in payment_method_lower or 'yk' in payment_method_lower:
                                payment_method = 'YooKassa'
                            elif 'yoomoney' in payment_method_lower:
                                payment_method = 'YooMoney'
                            elif 'cryptobot' in payment_method_lower:
                                payment_method = 'CryptoBot'
                            elif 'tgstar' in payment_method_lower:
                                payment_method = 'TG Star'
                            elif 'usdt' in payment_method_lower:
                                payment_method = 'USDT'
                    
                    # Обработка тарифов в зависимости от типа платежа
                    if payment_type == 'device_limit_upgrade':
                        # Расширение лимита устройств
                        try:
                            old_limit = int(metadata.get('old_limit') or 0)
                        except (ValueError, TypeError):
                            old_limit = 0
                        try:
                            new_limit = int(metadata.get('new_limit') or 0)
                        except (ValueError, TypeError):
                            new_limit = 0
                        tariff_info = {
                            'type': 'device_upgrade',
                            'old_limit': old_limit,
                            'new_limit': new_limit,
                        }
                    elif payment_type == 'traffic_renewal':
                        # Для продления трафика используем тарифы докупки трафика
                        tariff_id = metadata.get('tariff_id')
                        traffic_to_add_gb = metadata.get('traffic_to_add_gb') or metadata.get('traffic_gb_to_add') or metadata.get('traffic_gb')
                        if tariff_id:
                            try:
                                tariff_id_int = int(tariff_id)
                                traffic_topup_tariff_ids.add(tariff_id_int)
                                tariff_info = {
                                    'id': tariff_id_int,
                                    'type': 'traffic_topup',
                                    'traffic_gb': traffic_to_add_gb
                                }
                            except (ValueError, TypeError):
                                if traffic_to_add_gb:
                                    tariff_info = {'type': 'traffic_topup', 'traffic_gb': traffic_to_add_gb}
                                else:
                                    tariff_info = None
                        elif traffic_to_add_gb:
                            tariff_info = {'type': 'traffic_topup', 'traffic_gb': traffic_to_add_gb}
                        else:
                            tariff_info = None
                    else:
                        # Для обычных платежей используем тарифы подписки
                        tariff_id = metadata.get('tariff_id')
                        subscription_days = metadata.get('subscription_days') or metadata.get('days')
                        
                        # Если tariff_id не найден в metadata, пытаемся извлечь его из payload (для CryptoBot)
                        if not tariff_id and payment_method == 'CryptoBot':
                            payload_str = metadata.get('payload', '')
                            if payload_str and '|' in payload_str:
                                try:
                                    # Формат payload: "user_id|tariff_id|days|limit_ip"
                                    parts = payload_str.split('|')
                                    if len(parts) >= 2:
                                        tariff_id = parts[1]
                                        if not subscription_days and len(parts) >= 3:
                                            subscription_days = parts[2]
                                except (ValueError, IndexError):
                                    pass
                        
                        # Лимит устройств из metadata (значение на момент покупки)
                        try:
                            md_limit_ip = int(metadata.get('limit_ip') or 0)
                        except (ValueError, TypeError):
                            md_limit_ip = 0
                        if tariff_id:
                            try:
                                tariff_id_int = int(tariff_id)
                                tariff_ids.add(tariff_id_int)
                                tariff_info = {
                                    'id': tariff_id_int,
                                    'type': 'subscription',
                                    'days': subscription_days,
                                    'limit_ip': md_limit_ip,
                                }
                            except (ValueError, TypeError):
                                if subscription_days:
                                    tariff_info = {'type': 'subscription', 'days': subscription_days, 'limit_ip': md_limit_ip}
                                else:
                                    tariff_info = None
                        elif subscription_days:
                            tariff_info = {'type': 'subscription', 'days': subscription_days, 'limit_ip': md_limit_ip}
                        else:
                            tariff_info = None
                except:
                    pass
            
            # Если метод все еще не определен, используем "Неизвестно"
            if not payment_method:
                payment_method = 'Неизвестно'
            
            # Нормализуем название метода для CSS класса
            payment_method_normalized = payment_method.lower().replace(' ', '-').replace('_', '-')
            if payment_method_normalized == 'tg-star':
                payment_method_normalized = 'tg-star'
            elif payment_method_normalized == 'tgstar':
                payment_method_normalized = 'tg-star'
            
            # Извлекаем ID без префикса для копирования
            if payment_id:
                clean_id = payment_id
                prefixes = ['WATA_', 'PLATEGA_', 'YOOMONEY_', 'YK_', 'CRYPTOBOT_', 'CRYPTO_', 'TGSTAR_', 'USDT_']
                for prefix in prefixes:
                    if clean_id.startswith(prefix):
                        clean_id = clean_id[len(prefix):]
                        break
                p['payment_id_clean'] = clean_id
            else:
                p['payment_id_clean'] = payment_id
            
            p['tariff_info'] = tariff_info
            p['payment_method'] = payment_method
            p['payment_method_normalized'] = payment_method_normalized
            p['payment_type'] = payment_type
            payments.append(p)
        
        # Получаем названия тарифов подписки (+ limit_ip / traffic_gb для подробного отображения)
        tariffs = {}
        if tariff_ids:
            tariff_ids_list = list(tariff_ids)
            q = (
                "SELECT id, name, days, "
                "COALESCE(limit_ip, 0)  AS limit_ip, "
                "COALESCE(traffic_gb, 0) AS traffic_gb "
                f"FROM tariffs WHERE id IN ({','.join(['?']*len(tariff_ids_list))})"
            )
            for t in (await async_query_db(q, tariff_ids_list)):
                t_dict = dict(t)
                tariffs[t_dict['id']] = {
                    'name':       t_dict['name'],
                    'days':       t_dict['days'],
                    'limit_ip':   int(t_dict.get('limit_ip')   or 0),
                    'traffic_gb': int(t_dict.get('traffic_gb') or 0),
                }
        
        # Получаем названия тарифов докупки трафика
        traffic_topup_tariffs = {}
        if traffic_topup_tariff_ids:
            traffic_topup_tariff_ids_list = list(traffic_topup_tariff_ids)
            q = f"SELECT id, name, traffic_gb FROM traffic_topup_tariffs WHERE id IN ({','.join(['?']*len(traffic_topup_tariff_ids_list))})"
            for t in (await async_query_db(q, traffic_topup_tariff_ids_list)):
                t_dict = dict(t)
                traffic_topup_tariffs[t_dict['id']] = {'name': t_dict['name'], 'traffic_gb': t_dict['traffic_gb']}
        
        # Добавляем информацию о тарифах в каждый платеж
        for p in payments:
            tariff_info = p.get('tariff_info')
            if tariff_info and tariff_info.get('id'):
                tariff_id = tariff_info['id']
                if tariff_info['type'] == 'traffic_topup' and tariff_id in traffic_topup_tariffs:
                    p['tariff_title'] = traffic_topup_tariffs[tariff_id]['name']
                    p['tariff_name'] = traffic_topup_tariffs[tariff_id]['name']
                elif tariff_info['type'] == 'subscription' and tariff_id in tariffs:
                    p['tariff_title'] = tariffs[tariff_id]['name']
                    p['tariff_name'] = tariffs[tariff_id]['name']
        
        # Получаем промокоды
        promo_rows = await async_query_db(
            "SELECT code, used_at FROM promo_redemptions WHERE telegram_id = ? ORDER BY used_at DESC",
            (telegram_id,)
        )
        promo = [dict(p) for p in promo_rows] if promo_rows else []
        
        # Получаем рефералов с информацией о бонусах
        referrals_rows = await async_query_db(
            "SELECT telegram_id, username, COALESCE(invited_by_method, '') as invited_by_method FROM users WHERE invited_by = ?",
            (telegram_id,)
        )
        referrals = []
        ref_ids = [int(r['telegram_id']) for r in (referrals_rows or [])]

        # ─── Агрегаты по успешным платежам всех приглашённых (один запрос) ───
        ref_payments_map = {}  # { ref_id: { 'count': N, 'sum_by_cur': { 'RUB': X, ... } } }
        if ref_ids:
            try:
                placeholders = ','.join(['?'] * len(ref_ids))
                pay_rows = await async_query_db(
                    f"""
                    SELECT telegram_id,
                           COALESCE(NULLIF(currency, ''), 'RUB') AS cur,
                           COUNT(*) AS cnt,
                           COALESCE(SUM(amount), 0) AS s
                    FROM payments
                    WHERE status = 'succeeded' AND telegram_id IN ({placeholders})
                    GROUP BY telegram_id, cur
                    """,
                    tuple(ref_ids)
                )
                for r in (pay_rows or []):
                    rid = int(r['telegram_id'])
                    cur = r['cur'] or 'RUB'
                    bucket = ref_payments_map.setdefault(rid, {'count': 0, 'sum_by_cur': {}})
                    bucket['count'] += int(r['cnt'] or 0)
                    bucket['sum_by_cur'][cur] = bucket['sum_by_cur'].get(cur, 0) + float(r['s'] or 0)
            except Exception as e:
                logger.warning(f"[USERS] referrals payments aggregate failed: {e}")

        # Сводки по обычным рефералам / партнёрам
        regular_summary = {
            'count': 0,
            'payers_count': 0,
            'payments_count': 0,
            'sum_by_currency': {},
            'bonus_join_count': 0,
            'bonus_first_payment_count': 0,
        }
        partners_summary = {
            'count': 0,
            'payers_count': 0,
            'payments_count': 0,
            'sum_by_currency': {},
            'total_bonus_rub': 0.0,
        }

        for ref_row in referrals_rows or []:
            ref_dict = dict(ref_row)
            ref_telegram_id = int(ref_dict['telegram_id'])
            is_partner = ref_dict.get('invited_by_method') == 'partner'

            stats = ref_payments_map.get(ref_telegram_id, {'count': 0, 'sum_by_cur': {}})
            ref_dict['payments_count'] = stats['count']
            ref_dict['payments_sum_by_currency'] = dict(stats['sum_by_cur'])
            has_paid = stats['count'] > 0

            if is_partner:
                total_bonus_from_ref = 0.0
                try:
                    total_bonus_row = await async_query_db(
                        "SELECT SUM(bonus) as total FROM partner_accruals WHERE partner_id = ? AND payer_id = ?",
                        (telegram_id, ref_telegram_id), one=True
                    )
                    if total_bonus_row and total_bonus_row.get('total'):
                        total_bonus_from_ref = float(total_bonus_row['total']) or 0.0
                except Exception:
                    pass

                ref_dict['invitation_bonus'] = 0
                ref_dict['first_payment_bonus'] = 0
                ref_dict['total_bonus'] = total_bonus_from_ref
                ref_dict['has_invitation_bonus_days'] = False
                ref_dict['has_first_payment_bonus_days'] = False

                partners_summary['count'] += 1
                if has_paid:
                    partners_summary['payers_count'] += 1
                partners_summary['payments_count'] += stats['count']
                for c, v in stats['sum_by_cur'].items():
                    partners_summary['sum_by_currency'][c] = partners_summary['sum_by_currency'].get(c, 0) + v
                partners_summary['total_bonus_rub'] += total_bonus_from_ref
            else:
                has_invitation_bonus = False
                try:
                    invitation_check = await async_query_db(
                        "SELECT 1 FROM referral_payment_bonus WHERE ref_user_id = ? AND invited_user_id = ? AND bonus_type = 'join' LIMIT 1",
                        (telegram_id, ref_telegram_id), one=True
                    )
                    has_invitation_bonus = invitation_check is not None
                except Exception:
                    pass

                has_first_payment_bonus = False
                try:
                    first_payment_check = await async_query_db(
                        "SELECT 1 FROM referral_payment_bonus WHERE ref_user_id = ? AND invited_user_id = ? AND bonus_type = 'payment' LIMIT 1",
                        (telegram_id, ref_telegram_id), one=True
                    )
                    has_first_payment_bonus = first_payment_check is not None
                except Exception:
                    pass

                ref_dict['invitation_bonus'] = 0
                ref_dict['first_payment_bonus'] = 0
                ref_dict['total_bonus'] = 0
                ref_dict['has_invitation_bonus_days'] = has_invitation_bonus
                ref_dict['has_first_payment_bonus_days'] = has_first_payment_bonus

                regular_summary['count'] += 1
                if has_paid:
                    regular_summary['payers_count'] += 1
                regular_summary['payments_count'] += stats['count']
                for c, v in stats['sum_by_cur'].items():
                    regular_summary['sum_by_currency'][c] = regular_summary['sum_by_currency'].get(c, 0) + v
                if has_invitation_bonus:
                    regular_summary['bonus_join_count'] += 1
                if has_first_payment_bonus:
                    regular_summary['bonus_first_payment_count'] += 1

            referrals.append(ref_dict)

        referrals_stats = {
            'regular':  regular_summary,
            'partners': partners_summary,
        }
        
        # Получаем информацию о пригласившем
        inviter = None
        if user.get('invited_by'):
            inviter_row = await async_query_db(
                "SELECT telegram_id, username FROM users WHERE telegram_id = ?",
                (user['invited_by'],), one=True
            )
            if inviter_row:
                inviter = dict(inviter_row)
        
        # Получаем ссылку на страницу подписки
        sub_page_link = None
        try:
            row = await async_query_db("SELECT value FROM settings WHERE key = 'sub_page_url'", (), one=True)
            base = (row['value'].strip() if row and row['value'] else '')
            uuid = (user.get('xui_client_uuid') or '').strip()
            if base and uuid:
                base = base.rstrip('/')
                sub_page_link = f"{base}/sub/{uuid}"
        except Exception:
            sub_page_link = None
        
        # Получаем ссылку на страницу подключения (для API запросов)
        connect_page_link = None
        try:
            row = await async_query_db("SELECT value FROM settings WHERE key = 'connect_page_url'", (), one=True)
            base = (row['value'].strip() if row and row['value'] else '')
            uuid = (user.get('xui_client_uuid') or '').strip()
            if base and uuid:
                base = base.rstrip('/')
                connect_page_link = f"{base}/sub/{uuid}"
        except Exception:
            connect_page_link = None
        
        # Дата окончания: naive в БД = МСК (как в xuiweb/subpage), не UTC
        user['subscription_end_date'] = subscription_end_to_utc_iso(user.get('subscription_end_date'))
        
        return jsonify({
            'ok': True,
            'data': {
                'user': user,
                'remnawave': remnawave_data,
                'payments': payments,
                'tariffs': tariffs,
                'traffic_topup_tariffs': traffic_topup_tariffs,
                'promo': promo,
                'referrals': referrals,
                'referrals_stats': referrals_stats,
                'inviter': inviter,
                'sub_page_link': sub_page_link,
                'connect_page_link': connect_page_link
            }
        })
        
    except Exception as e:
        error_traceback = traceback.format_exc()
        current_app.logger.error(f"[API] Ошибка при получении данных пользователя {telegram_id}: {e}\n{error_traceback}")
        return jsonify({
            'ok': False,
            'error': str(e),
            'traceback': error_traceback
        }), 500


@api_bp.route('/remnawave/add-traffic', methods=['POST'])
async def api_remnawave_add_traffic():
    """
    Добавить трафик (GB) пользователю Remnawave.
    Body: { telegram_id: int, gb: number }
    """
    try:
        data = await request.get_json(force=True)
        telegram_id = int(data.get('telegram_id', 0))
        gb = float(data.get('gb', 0))
        if telegram_id <= 0 or gb <= 0:
            return jsonify({'ok': False, 'error': 'invalid_payload'}), 400

        user_row = await async_query_db(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,), one=True
        )
        if not user_row:
            return jsonify({'ok': False, 'error': 'user_not_found'}), 404

        user = dict(user_row)

        # Получаем Remnawave пользователя (повторяем логику поиска)
        await app_conf.load_settings()
        
        await remnawave_manager_instance._ensure_initialized()

        async def _find_remna_user(u):
            remna_user = None
            # 1) UUID
            if u.get('xui_client_uuid'):
                try:
                    remna_user = await remnawave_manager_instance._sdk.users.get_user_by_uuid(u['xui_client_uuid'])
                except Exception:
                    remna_user = None
            # 2) short UUID
            if remna_user is None and u.get('remnawave_short_uuid'):
                try:
                    remna_user = await remnawave_manager_instance._sdk.users.get_user_by_short_uuid(u['remnawave_short_uuid'])
                except Exception:
                    remna_user = None
            # 3) telegram_id
            if remna_user is None and u.get('telegram_id'):
                try:
                    users_list = await remnawave_manager_instance._sdk.users.get_users_by_telegram_id(str(u['telegram_id']))
                    if users_list:
                        remna_user = next((ru for ru in users_list if getattr(ru, 'status', '') == 'active'), users_list[0])
                except Exception:
                    remna_user = None
            # 4) email
            if remna_user is None and u.get('xui_client_email'):
                try:
                    users_list = await remnawave_manager_instance._sdk.users.get_users_by_email(u['xui_client_email'])
                    if users_list:
                        remna_user = users_list[0]
                except Exception:
                    remna_user = None
            return remna_user

        remna_user = await _find_remna_user(user)
        if not remna_user:
            return jsonify({'ok': False, 'error': 'remnawave_user_not_found'}), 404

        current_limit = getattr(remna_user, 'traffic_limit_bytes', None) or 0
        add_bytes = int(gb * (1024 ** 3))
        new_limit = current_limit + add_bytes

        # Обновляем лимит
        await remnawave_manager_instance._sdk.users.update_user({
            "uuid": str(remna_user.uuid),
            "traffic_limit_bytes": new_limit
        })

        return jsonify({'ok': True, 'new_limit_bytes': new_limit})
    except Exception as e:
        error_traceback = traceback.format_exc()
        current_app.logger.error(f"[API] Ошибка при добавлении трафика Remnawave: {e}\n{error_traceback}")
        return jsonify({'ok': False, 'error': str(e), 'traceback': error_traceback}), 500


@api_bp.route('/remnawave/reduce-traffic', methods=['POST'])
async def api_remnawave_reduce_traffic():
    """
    Уменьшить трафик (GB) пользователю Remnawave.
    Body: { telegram_id: int, gb: number }
    """
    try:
        data = await request.get_json(force=True)
        telegram_id = int(data.get('telegram_id', 0))
        gb = float(data.get('gb', 0))
        if telegram_id <= 0 or gb <= 0:
            return jsonify({'ok': False, 'error': 'invalid_payload'}), 400

        user_row = await async_query_db(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,), one=True
        )
        if not user_row:
            return jsonify({'ok': False, 'error': 'user_not_found'}), 404

        user = dict(user_row)

        # Получаем Remnawave пользователя (повторяем логику поиска)
        await app_conf.load_settings()
        
        await remnawave_manager_instance._ensure_initialized()

        async def _find_remna_user(u):
            remna_user = None
            # 1) UUID
            if u.get('xui_client_uuid'):
                try:
                    remna_user = await remnawave_manager_instance._sdk.users.get_user_by_uuid(u['xui_client_uuid'])
                except Exception:
                    remna_user = None
            # 2) short UUID
            if remna_user is None and u.get('remnawave_short_uuid'):
                try:
                    remna_user = await remnawave_manager_instance._sdk.users.get_user_by_short_uuid(u['remnawave_short_uuid'])
                except Exception:
                    remna_user = None
            # 3) telegram_id
            if remna_user is None and u.get('telegram_id'):
                try:
                    users_list = await remnawave_manager_instance._sdk.users.get_users_by_telegram_id(str(u['telegram_id']))
                    if users_list:
                        remna_user = next((ru for ru in users_list if getattr(ru, 'status', '') == 'active'), users_list[0])
                except Exception:
                    remna_user = None
            # 4) email
            if remna_user is None and u.get('xui_client_email'):
                try:
                    users_list = await remnawave_manager_instance._sdk.users.get_users_by_email(u['xui_client_email'])
                    if users_list:
                        remna_user = users_list[0]
                except Exception:
                    remna_user = None
            return remna_user

        remna_user = await _find_remna_user(user)
        if not remna_user:
            return jsonify({'ok': False, 'error': 'remnawave_user_not_found'}), 404

        current_limit = getattr(remna_user, 'traffic_limit_bytes', None) or 0
        reduce_bytes = int(gb * (1024 ** 3))
        new_limit = max(0, current_limit - reduce_bytes)  # Не позволяем уйти в отрицательные значения

        # Обновляем лимит
        await remnawave_manager_instance._sdk.users.update_user({
            "uuid": str(remna_user.uuid),
            "traffic_limit_bytes": new_limit
        })

        return jsonify({'ok': True, 'new_limit_bytes': new_limit})
    except Exception as e:
        error_traceback = traceback.format_exc()
        current_app.logger.error(f"[API] Ошибка при уменьшении трафика Remnawave: {e}\n{error_traceback}")
        return jsonify({'ok': False, 'error': str(e), 'traceback': error_traceback}), 500


@api_bp.route('/remnawave/internal-squads', methods=['GET'])
async def api_remnawave_internal_squads():
    """Список internal squads из Remnawave (для выбора в карточке клиента)."""
    try:
        await app_conf.load_settings()
        await remnawave_manager_instance._ensure_initialized()
        squads = await remnawave_manager_instance.get_internal_squads()
        if squads is None:
            return jsonify({'ok': False, 'error': 'fetch_failed'}), 502
        return jsonify({'ok': True, 'squads': squads})
    except Exception as e:
        current_app.logger.error(f"[API] Ошибка получения internal squads: {e}\n{traceback.format_exc()}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_bp.route('/remnawave/set-squad', methods=['POST'])
async def api_remnawave_set_squad():
    """
    Назначить internal squad пользователю Remnawave.
    Body: { telegram_id: int, squad_uuid: str }
    """
    from uuid import UUID

    try:
        data = await request.get_json(force=True)
        telegram_id = int(data.get('telegram_id', 0))
        squad_uuid = (data.get('squad_uuid') or '').strip()
        if telegram_id <= 0 or not squad_uuid:
            return jsonify({'ok': False, 'error': 'invalid_payload'}), 400
        try:
            squad_uuid_obj = UUID(squad_uuid)
        except ValueError:
            return jsonify({'ok': False, 'error': 'invalid_squad_uuid'}), 400

        user_row = await async_query_db(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,), one=True
        )
        if not user_row:
            return jsonify({'ok': False, 'error': 'user_not_found'}), 404

        user = dict(user_row)
        await app_conf.load_settings()
        await remnawave_manager_instance._ensure_initialized()

        remna_user = None
        if user.get('xui_client_uuid'):
            try:
                remna_user = await remnawave_manager_instance._sdk.users.get_user_by_uuid(user['xui_client_uuid'])
            except Exception:
                remna_user = None
        if remna_user is None and user.get('remnawave_short_uuid'):
            try:
                remna_user = await remnawave_manager_instance._sdk.users.get_user_by_short_uuid(user['remnawave_short_uuid'])
            except Exception:
                remna_user = None
        if remna_user is None and user.get('telegram_id'):
            try:
                users_list = await remnawave_manager_instance._sdk.users.get_users_by_telegram_id(str(user['telegram_id']))
                if users_list:
                    remna_user = next((u for u in users_list if getattr(u, 'status', '') == 'active'), users_list[0])
            except Exception:
                remna_user = None
        if remna_user is None and user.get('xui_client_email'):
            try:
                users_list = await remnawave_manager_instance._sdk.users.get_users_by_email(user['xui_client_email'])
                remna_user = users_list[0] if users_list else None
            except Exception:
                remna_user = None

        if not remna_user:
            return jsonify({'ok': False, 'error': 'remnawave_user_not_found'}), 404

        await remnawave_manager_instance._sdk.users.update_user({
            'uuid': str(remna_user.uuid),
            'active_internal_squads': [squad_uuid_obj],
        })

        squad_name = str(squad_uuid_obj)
        squads_list = await remnawave_manager_instance.get_internal_squads()
        if squads_list:
            for sq in squads_list:
                if sq.get('uuid') == str(squad_uuid_obj):
                    squad_name = sq.get('name') or squad_name
                    break

        return jsonify({'ok': True, 'squad_uuid': str(squad_uuid_obj), 'squad_name': squad_name})
    except Exception as e:
        current_app.logger.error(f"[API] Ошибка смены squad Remnawave: {e}\n{traceback.format_exc()}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_bp.route('/users/<int:telegram_id>/partner-percent', methods=['POST'])
async def api_set_partner_percent(telegram_id: int):
    """
    Установить индивидуальный процент партнёра для пользователя.
    Body: { "percent": null | number } (null для сброса на глобальную настройку, число 0-100 для установки)
    """
    try:
        data = await request.get_json(force=True)
        percent = data.get('percent')
        
        
        # Проверяем существование пользователя
        user_row = await async_query_db(
            "SELECT telegram_id FROM users WHERE telegram_id = ?",
            (telegram_id,), one=True
        )
        if not user_row:
            return jsonify({'ok': False, 'error': 'user_not_found'}), 404
        
        # Проверяем наличие поля в таблице
        column_names = await get_table_columns_cached('users')
        has_field = 'partner_percent_rub' in column_names
        
        if not has_field:
            await async_execute_db("ALTER TABLE users ADD COLUMN partner_percent_rub REAL DEFAULT NULL", ())
        
        # Валидация процента
        if percent is not None:
            try:
                percent_float = float(percent)
                if percent_float < 0 or percent_float > 100:
                    return jsonify({'ok': False, 'error': 'invalid_percent_range', 'message': 'Процент должен быть от 0 до 100'}), 400
                percent_value = percent_float
            except (ValueError, TypeError):
                return jsonify({'ok': False, 'error': 'invalid_percent_format', 'message': 'Процент должен быть числом'}), 400
        else:
            percent_value = None
        
        # Обновляем значение
        await async_execute_db(
            "UPDATE users SET partner_percent_rub = ? WHERE telegram_id = ?",
            (percent_value, telegram_id)
        )
        
        return jsonify({
            'ok': True,
            'percent': percent_value,
            'message': 'Процент партнёра обновлён' if percent_value is not None else 'Процент сброшен на глобальную настройку'
        })
        
    except Exception as e:
        logger.error(f"Ошибка обновления процента партнёра для пользователя {telegram_id}: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_bp.route('/users/<int:telegram_id>/change-invite-method', methods=['POST'])
async def api_change_invite_method(telegram_id: int):
    """
    Сменить тип приглашения приглашённого клиента: реферал <-> партнёр.
    Меняет users.invited_by_method ('referral'/'partner') для указанного клиента.
    Body: { "method": "referral" | "partner" }
    """
    try:
        data = await request.get_json(force=True)
        method = str((data or {}).get('method', '')).strip().lower()
        if method not in ('referral', 'partner'):
            return jsonify({'ok': False, 'error': 'invalid_method'}), 400

        user_row = await async_query_db(
            "SELECT telegram_id, invited_by, invited_by_method FROM users WHERE telegram_id = ?",
            (telegram_id,), one=True
        )
        if not user_row:
            return jsonify({'ok': False, 'error': 'user_not_found'}), 404
        user = dict(user_row)
        if not user.get('invited_by'):
            return jsonify({'ok': False, 'error': 'no_inviter',
                            'message': 'У клиента нет пригласившего'}), 400

        old_method = (user.get('invited_by_method') or 'referral')
        await async_execute_db(
            "UPDATE users SET invited_by_method = ? WHERE telegram_id = ?",
            (method, telegram_id)
        )
        logger.info(
            f"[API] change-invite-method: клиент={telegram_id}, пригласил={user.get('invited_by')}, "
            f"{old_method} -> {method}"
        )
        return jsonify({'ok': True, 'method': method})
    except Exception as e:
        logger.error(f"Ошибка смены метода приглашения для {telegram_id}: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_bp.route('/users/<int:telegram_id>/toggle-partner-button', methods=['POST'])
async def api_toggle_partner_button(telegram_id: int):
    """
    Установить индивидуальную настройку показа кнопки партнёрской программы для пользователя.
    Body: { "value": "default" | "1" | "0" }
    """
    try:
        data = await request.get_json(force=True)
        value = data.get('value', '').lower()
        
        if value not in ['default', '1', '0']:
            return jsonify({'ok': False, 'error': 'invalid_value'}), 400
        
        
        # Проверяем существование пользователя
        user_row = await async_query_db(
            "SELECT telegram_id FROM users WHERE telegram_id = ?",
            (telegram_id,), one=True
        )
        if not user_row:
            return jsonify({'ok': False, 'error': 'user_not_found'}), 404
        
        # Проверяем наличие поля в таблице
        column_names = await get_table_columns_cached('users')
        has_field = 'show_partner_program_button' in column_names
        
        if not has_field:
            await async_execute_db("ALTER TABLE users ADD COLUMN show_partner_program_button TEXT DEFAULT NULL", ())
        
        # Обновляем значение
        if value == 'default':
            # Устанавливаем NULL для использования глобальной настройки
            await async_execute_db(
                "UPDATE users SET show_partner_program_button = NULL WHERE telegram_id = ?",
                (telegram_id,)
            )
        else:
            await async_execute_db(
                "UPDATE users SET show_partner_program_button = ? WHERE telegram_id = ?",
                (value, telegram_id)
            )
        
        return jsonify({
            'ok': True,
            'value': value if value != 'default' else None,
            'message': 'Настройка обновлена'
        })
        
    except Exception as e:
        logger.error(f"Ошибка обновления настройки партнёрской программы для пользователя {telegram_id}: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@api_bp.route('/users/<int:telegram_id>/toggle-block', methods=['POST'])
async def api_toggle_user_block(telegram_id: int):
    """
    Блокировать/разблокировать пользователя.
    Body: { action: 'block' | 'unblock' }
    """
    try:
        data = await request.get_json(force=True)
        action = data.get('action', '').lower()
        
        if action not in ['block', 'unblock']:
            return jsonify({'ok': False, 'error': 'invalid_action'}), 400

        # Получаем данные пользователя
        user_row = await async_query_db(
            "SELECT *, COALESCE(is_blocked, 0) as is_blocked FROM users WHERE telegram_id = ?",
            (telegram_id,), one=True
        )
        if not user_row:
            return jsonify({'ok': False, 'error': 'user_not_found'}), 404

        user = dict(user_row)
        current_is_blocked = bool(user.get('is_blocked', 0))

        # Проверяем, что действие соответствует текущему состоянию
        if action == 'block' and current_is_blocked:
            return jsonify({'ok': False, 'error': 'user_already_blocked'}), 400
        if action == 'unblock' and not current_is_blocked:
            return jsonify({'ok': False, 'error': 'user_not_blocked'}), 400

        await app_conf.load_settings()
        
        # Инициализируем Remnawave только если он используется (не блокируем блокировку, если Remnawave не настроен)
        remnawave_available = False
        try:
            await remnawave_manager_instance._ensure_initialized()
            remnawave_available = remnawave_manager_instance._sdk is not None
        except Exception as e:
            # Remnawave не настроен или недоступен - это нормально, продолжаем без него
            current_app.logger.debug(f"[API] Remnawave недоступен: {e}")

        if action == 'block':
            # БЛОКИРОВКА — только Remnawave
            remnawave_disabled = False
            remnawave_error = None
            if remnawave_available and user.get('xui_client_uuid'):
                try:
                    await remnawave_manager_instance._sdk.users.disable_user(user['xui_client_uuid'])
                    remnawave_disabled = True
                except Exception as e:
                    remnawave_error = str(e)

            column_names = await get_table_columns_cached('users')
            has_is_blocked = 'is_blocked' in column_names

            if not has_is_blocked:
                await async_execute_db("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0", ())

            await async_execute_db("UPDATE users SET is_blocked = 1 WHERE telegram_id = ?", (telegram_id,))

            # Сбрасываем флаги уведомлений при блокировке
            try:
                await async_execute_db(
                    "UPDATE users SET notified_expiring = 0, notified_expired = 0 WHERE telegram_id = ?",
                    (telegram_id,)
                )
                if 'is_active' in column_names:
                    await async_execute_db("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
            except Exception:
                pass

            result = {
                'ok': True,
                'action': 'blocked',
                'remnawave_disabled': remnawave_disabled,
                'remnawave_error': remnawave_error
            }

        else:
            # РАЗБЛОКИРОВКА
            await async_execute_db("UPDATE users SET is_blocked = 0 WHERE telegram_id = ?", (telegram_id,))

            restored = False
            restore_error = None
            if user.get('xui_client_uuid') and user.get('subscription_end_date'):
                # Проверяем, что дата окончания в будущем
                try:
                    end_date = datetime.fromisoformat(user['subscription_end_date'].replace('Z', '+00:00')) if isinstance(user['subscription_end_date'], str) else user['subscription_end_date']
                    if end_date.tzinfo is None:
                        end_date = end_date.replace(tzinfo=timezone.utc)
                    else:
                        end_date = end_date.astimezone(timezone.utc)
                    
                    now_utc = datetime.now(timezone.utc)
                    if end_date > now_utc:
                        # Восстанавливаем через grant_subscription с 0 дней
                        # Это создаст пользователя на всех серверах с текущей датой окончания из БД
                        limit_ip = user.get('limit_ip', 0) or 0
                        restore_result = await grant_subscription(
                            user_id=telegram_id,
                            days_to_add=0,  # Не добавляем дни, просто восстанавливаем
                            limit_ip=limit_ip,
                            reset_traffic_on_renewal=False
                        )
                        
                        if restore_result:
                            restored = True
                        else:
                            restore_error = "Не удалось восстановить через grant_subscription"
                    else:
                        restore_error = "Подписка истекла, восстановление невозможно"
                except Exception as e:
                    restore_error = f"Ошибка восстановления: {str(e)}"

            remnawave_enabled = False
            remnawave_error = None
            if remnawave_available and user.get('xui_client_uuid'):
                try:
                    await remnawave_manager_instance._sdk.users.enable_user(user['xui_client_uuid'])
                    remnawave_enabled = True
                except Exception as e:
                    remnawave_error = str(e)

            result = {
                'ok': True,
                'action': 'unblocked',
                'restored': restored,
                'restore_error': restore_error,
                'remnawave_enabled': remnawave_enabled,
                'remnawave_error': remnawave_error
            }

        return jsonify(result)

    except Exception as e:
        error_traceback = traceback.format_exc()
        current_app.logger.error(f"[API] Ошибка при блокировке/разблокировке пользователя: {e}\n{error_traceback}")
        return jsonify({'ok': False, 'error': str(e), 'traceback': error_traceback}), 500


@api_bp.route('/users/<int:telegram_id>/change-limit-ip', methods=['POST'])
async def api_change_limit_ip(telegram_id: int):
    """
    Изменить лимит устройств пользователя.

    Меняем только значение в БД (колонка users.limit_ip).
    На X-UI/Remnawave новый лимит применится при следующем продлении/обновлении.

    Body: { limit_ip: int (0 = без лимита) }
    """
    try:
        data = await request.get_json(force=True)
        limit_ip = data.get('limit_ip')

        if limit_ip is None:
            return jsonify({'ok': False, 'error': 'Параметр limit_ip обязателен'}), 400

        try:
            limit_ip = int(limit_ip)
        except (ValueError, TypeError):
            return jsonify({'ok': False, 'error': 'limit_ip должен быть числом'}), 400

        if limit_ip < 0:
            return jsonify({'ok': False, 'error': 'Лимит устройств не может быть отрицательным'}), 400

        # Проверяем существование пользователя
        user_row = await async_query_db(
            "SELECT telegram_id FROM users WHERE telegram_id = ?",
            (telegram_id,), one=True
        )
        if not user_row:
            return jsonify({'ok': False, 'error': 'Пользователь не найден'}), 404

        success, error_msg = await update_user_device_limit(
            telegram_id=telegram_id,
            limit_ip=limit_ip,
        )

        if success:
            return jsonify({
                'ok': True,
                'message': f'Лимит устройств изменен на {limit_ip if limit_ip > 0 else "без лимита"}',
            })
        return jsonify({'ok': False, 'error': error_msg or 'Ошибка обновления лимита устройств'}), 500

    except Exception as e:
        logger.error(f"[API] Ошибка изменения лимита устройств для {telegram_id}: {e}", exc_info=True)
        return jsonify({'ok': False, 'error': f'Внутренняя ошибка: {str(e)}'}), 500


@api_bp.route('/users/<int:telegram_id>/change-free-renewal-status', methods=['POST'])
async def api_change_free_renewal_status(telegram_id: int):
    """
    Изменить статус доп. триала пользователя (получен/не получен).
    Body: { free_renewal_used: 0 | 1 }
    """
    try:
        
        data = await request.get_json(force=True)
        new_status = data.get('free_renewal_used')
        
        if new_status not in [0, 1]:
            return jsonify({'ok': False, 'error': 'Некорректный статус. Используйте 0 (не получен) или 1 (получен)'}), 400
        
        # Проверяем существование пользователя
        user_row = await async_query_db(
            "SELECT telegram_id, COALESCE(free_renewal_used, 0) as free_renewal_used FROM users WHERE telegram_id = ?",
            (telegram_id,), one=True
        )
        
        if not user_row:
            return jsonify({'ok': False, 'error': 'Пользователь не найден'}), 404
        
        user = dict(user_row)
        current_status = int(user.get('free_renewal_used', 0) or 0)
        
        if current_status == new_status:
            status_text = 'Получен' if new_status == 1 else 'Не получен'
            return jsonify({'ok': False, 'error': f'Статус доп. триала уже установлен на "{status_text}"'}), 400
        
        # Обновляем статус
        await async_execute_db(
            "UPDATE users SET free_renewal_used = ? WHERE telegram_id = ?",
            (new_status, telegram_id)
        )
        
        status_text = 'Получен' if new_status == 1 else 'Не получен'
        current_app.logger.info(f"[API] Статус доп. триала для пользователя {telegram_id} изменен с '{current_status}' на '{new_status}' ({status_text})")
        
        return jsonify({
            'ok': True,
            'message': f'Статус доп. триала изменен на "{status_text}"',
            'telegram_id': telegram_id,
            'old_status': current_status,
            'new_status': new_status
        })
        
    except Exception as e:
        current_app.logger.error(f"[API] Ошибка при изменении статуса доп. триала для {telegram_id}: {e}\n{traceback.format_exc()}")
        return jsonify({'ok': False, 'error': f'Ошибка сервера: {str(e)}'}), 500

@api_bp.route('/users/<int:telegram_id>/change-provider', methods=['POST'])
async def api_change_provider(telegram_id: int):
    return jsonify({'ok': False, 'error': 'Провайдер подписки больше не используется (только Remnawave)'}), 410


@api_bp.route('/payment/update-status', methods=['POST'])
async def api_payment_update_status():
    """
    Изменить статус платежа.
    Body: { payment_id: str, status: str }
    """
    try:
        data = await request.get_json(force=True)
        payment_id = data.get('payment_id')
        new_status = data.get('status')
        
        if not payment_id or not new_status:
            return jsonify({'ok': False, 'error': 'payment_id и status обязательны'}), 400
        
        # Только админ может менять статус на canceled
        if new_status == 'canceled' and session.get('admin_role') != 'admin':
            return jsonify({'ok': False, 'error': 'Forbidden'}), 403
        
        # Проверяем валидность статуса
        valid_statuses = ['succeeded', 'pending', 'processing', 'failed', 'canceled']
        if new_status not in valid_statuses:
            return jsonify({'ok': False, 'error': f'Неверный статус. Допустимые: {", ".join(valid_statuses)}'}), 400
        
        
        # Проверяем существование платежа
        payment = await async_query_db(
            "SELECT payment_id, status FROM payments WHERE payment_id = ?",
            (payment_id,), one=True
        )
        
        if not payment:
            return jsonify({'ok': False, 'error': 'Платеж не найден'}), 404
        
        # Обновляем статус
        await async_execute_db(
            "UPDATE payments SET status = ? WHERE payment_id = ?",
            (new_status, payment_id)
        )
        
        current_app.logger.info(f"[API] Статус платежа {payment_id} изменен с '{payment['status']}' на '{new_status}'")
        
        return jsonify({
            'ok': True,
            'message': f'Статус платежа изменен на "{new_status}"',
            'payment_id': payment_id,
            'old_status': payment['status'],
            'new_status': new_status
        })
        
    except Exception as e:
        error_traceback = traceback.format_exc()
        current_app.logger.error(f"[API] Ошибка при изменении статуса платежа: {e}\n{error_traceback}")
        return jsonify({'ok': False, 'error': str(e), 'traceback': error_traceback}), 500




@api_bp.route('/users/<int:telegram_id>/security')
async def api_user_security(telegram_id: int):
    """Данные устройств пользователя.

    Ходит на вырезанный xuiweb и всегда получает отказ — дальше работает
    фолбэк на devices.db, ради него запрос и оставлен. Вычистить обращения
    к xuiweb отсюда нельзя, не переписав раздел «Мои устройства» целиком,
    а он профилем роутеров выключен: за роутером вся домашняя сеть,
    и считать её поштучно незачем.
    """
    try:
        # Получаем UUID пользователя из основной БД
        user_row = await async_query_db(
            "SELECT xui_client_uuid, remnawave_short_uuid FROM users WHERE telegram_id = ?",
            (telegram_id,), one=True
        )

        if not user_row:
            return jsonify({'ok': True, 'devices': [], 'latest_access': None, 'message': 'Пользователь не найден'})

        uuid = (user_row.get('xui_client_uuid') or user_row.get('remnawave_short_uuid') or '').strip()
        if not uuid:
            return jsonify({'ok': True, 'devices': [], 'latest_access': None, 'message': 'UUID не найден'})

        # ── Запрос к xuiweb ───────────────────────────────────────────────────
        xuiweb_base = os.getenv('XUIWEB_INTERNAL_URL', 'http://127.0.0.1:8282').rstrip('/')
        try:
            async with httpx.AsyncClient(timeout=6, verify=False) as client:
                resp = await client.get(f"{xuiweb_base}/devices/{uuid}")

            if resp.status_code == 200:
                data = resp.json()
                devices_raw = data.get('devices', [])

                formatted_devices = []
                for d in devices_raw:
                    created = d.get('created_at') or ''
                    last_acc = d.get('access_time', '')
                    formatted_devices.append({
                        'hwid':         d.get('hwid', ''),
                        'os':           d.get('os', ''),
                        'os_version':   d.get('os_version', ''),
                        'model':        d.get('model', ''),
                        'user_agent':   d.get('user_agent', ''),
                        'ip_address':   d.get('ip_address', ''),
                        'last_access':  last_acc,
                        'first_access': created or last_acc,
                        'access_count': d.get('access_count', 0),
                        'custom_name':  d.get('custom_name', ''),
                    })

                latest_access = None
                if formatted_devices:
                    f = max(
                        formatted_devices,
                        key=lambda d: d.get('last_access') or '',
                    )
                    latest_access = {
                        'ip_address':  f['ip_address'],
                        'access_time': f['last_access'],
                        'hwid':        f['hwid'],
                        'os':          f['os'],
                        'os_version':  f['os_version'],
                        'model':       f['model'],
                        'user_agent':  f['user_agent'],
                        'first_access': f.get('first_access') or f['last_access'],
                        'custom_name': f.get('custom_name', ''),
                    }

                formatted_devices.sort(
                    key=lambda d: (
                        d.get('first_access') or d.get('last_access') or '',
                        d.get('hwid') or '',
                    )
                )

                return jsonify({
                    'ok': True,
                    'devices': formatted_devices,
                    'latest_access': latest_access,
                    'uuid': uuid,
                    'source': 'xuiweb',
                })

            logger.warning(f"[SECURITY] xuiweb вернул {resp.status_code} для uuid={uuid}")

        except Exception as xuiweb_err:
            logger.warning(f"[SECURITY] xuiweb недоступен ({xuiweb_err}), fallback на devices.db")

        # ── Fallback: прямое чтение devices.db ───────────────────────────────
        try:
            from devices.database import get_devices_by_uuid, get_latest_access_by_uuid
            devices = await get_devices_by_uuid(uuid)
            latest_access = await get_latest_access_by_uuid(uuid)

            formatted_devices = [{
                'hwid':         d.get('hwid', ''),
                'os':           d.get('os', ''),
                'os_version':   d.get('os_version', ''),
                'model':        d.get('model', ''),
                'user_agent':   d.get('user_agent', ''),
                'ip_address':   d.get('ip_address', ''),
                'last_access':  d.get('last_access', ''),
                'first_access': d.get('first_access', '') or d.get('last_access', ''),
                'access_count': d.get('access_count', 0),
                'custom_name':  d.get('custom_name', ''),
            } for d in devices]

            latest_access_fmt = None
            if latest_access:
                latest_access_fmt = {
                    'ip_address':  latest_access.get('ip_address', ''),
                    'access_time': latest_access.get('access_time', ''),
                    'hwid':        latest_access.get('hwid', ''),
                    'os':          latest_access.get('os', ''),
                    'os_version':  latest_access.get('os_version', ''),
                    'model':       latest_access.get('model', ''),
                    'user_agent':  latest_access.get('user_agent', ''),
                    'first_access': latest_access.get('created_at')
                        or latest_access.get('access_time') or '',
                    'custom_name': latest_access.get('custom_name', ''),
                }

            return jsonify({
                'ok': True,
                'devices': formatted_devices,
                'latest_access': latest_access_fmt,
                'uuid': uuid,
                'source': 'db',
            })

        except Exception as db_err:
            logger.error(f"[SECURITY] Fallback на DB тоже упал: {db_err}")
            return jsonify({'ok': True, 'devices': [], 'latest_access': None,
                            'message': 'Сервис устройств недоступен'}), 200

    except Exception as e:
        logger.error(f"[SECURITY] Критическая ошибка для telegram_id={telegram_id}: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_bp.route('/users/<int:telegram_id>/devices/<path:hwid>', methods=['DELETE'])
async def api_delete_user_device(telegram_id: int, hwid: str):
    """Удаляет устройство пользователя по HWID через xuiweb."""
    try:
        if not hwid or len(hwid) < 4 or len(hwid) > 128:
            return jsonify({'ok': False, 'error': 'Неверный HWID'}), 400

        user_row = await async_query_db(
            "SELECT xui_client_uuid, remnawave_short_uuid FROM users WHERE telegram_id = ?",
            (telegram_id,), one=True
        )
        if not user_row:
            return jsonify({'ok': False, 'error': 'Пользователь не найден'}), 404

        uuid = (user_row.get('xui_client_uuid') or user_row.get('remnawave_short_uuid') or '').strip()
        if not uuid:
            return jsonify({'ok': False, 'error': 'UUID не найден'}), 404

        xuiweb_base = os.getenv('XUIWEB_INTERNAL_URL', 'http://127.0.0.1:8282').rstrip('/')
        async with httpx.AsyncClient(timeout=6, verify=False) as client:
            resp = await client.delete(f"{xuiweb_base}/devices/{uuid}/{hwid}")

        if resp.status_code == 200:
            logger.info(f"[ADMIN] Удалено устройство hwid={hwid[:20]}... для telegram_id={telegram_id}")
            return jsonify({'ok': True})
        if resp.status_code == 404:
            return jsonify({'ok': False, 'error': 'Устройство не найдено'}), 404
        return jsonify({'ok': False, 'error': f'xuiweb вернул {resp.status_code}'}), 502

    except Exception as e:
        logger.error(f"[ADMIN] Ошибка удаления устройства для telegram_id={telegram_id}: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_bp.route('/users/<int:telegram_id>/keys')
async def api_user_keys(telegram_id: int):
    """Ключи подписки пользователя — проксируем через xuiweb /api/sub/{uuid}."""
    try:
        user_row = await async_query_db(
            "SELECT xui_client_uuid, remnawave_short_uuid FROM users WHERE telegram_id = ?",
            (telegram_id,), one=True
        )

        if not user_row:
            return jsonify({'ok': False, 'error': 'Пользователь не найден'}), 404

        uuid = (user_row.get('xui_client_uuid') or user_row.get('remnawave_short_uuid') or '').strip()
        if not uuid:
            return jsonify({'ok': False, 'error': 'UUID не найден'}), 404

        xuiweb_base = os.getenv('XUIWEB_INTERNAL_URL', 'http://127.0.0.1:8282').rstrip('/')
        async with httpx.AsyncClient(timeout=8, verify=False) as client:
            resp = await client.get(
                f"{xuiweb_base}/api/sub/{uuid}",
                headers={'Accept': 'application/json'},
            )

        if resp.status_code != 200:
            return jsonify({'ok': False, 'error': f'xuiweb вернул {resp.status_code}'}), 502

        data = resp.json()
        return jsonify({'ok': True, **data})

    except Exception as e:
        logger.error(f"[KEYS] Ошибка для telegram_id={telegram_id}: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_bp.route('/bulk-delete-users', methods=['POST'])
async def api_bulk_delete_users():
    """
    Массовое удаление пользователей из БД.
    Body: { user_ids: [int, ...] }
    Ограничение: не более 50 пользователей за раз.
    Удаляет из БД и Remnawave (если провайдер Remnawave), но НЕ удаляет с серверов 3X-UI.
    """
    # Модератор не может удалять пользователей массово
    if session.get('admin_role') == 'moderator':
        return jsonify({'error': 'Недостаточно прав. Массовое удаление доступно только администратору.'}), 403

    try:
        data = await request.get_json(force=True)
        user_ids = data.get('user_ids', [])
        
        if not user_ids or not isinstance(user_ids, list):
            return jsonify({'success': False, 'error': 'user_ids должен быть списком'}), 400
        
        if len(user_ids) > 50:
            return jsonify({'success': False, 'error': 'Можно удалить не более 50 пользователей за раз'}), 400
        
        if len(user_ids) == 0:
            return jsonify({'success': False, 'error': 'Список пользователей пуст'}), 400
        
        
        await app_conf.load_settings()
        
        deleted_count = 0
        failed_users = []
        remnawave_deleted = 0
        remnawave_failed = 0
        
        # Получаем список серверов (но не используем для удаления с 3X-UI)
        servers_row = await async_query_db("SELECT value FROM settings WHERE key = 'xui_servers'", (), one=True)
        servers = json.loads(dict(servers_row)['value']) if servers_row else []
        
        for telegram_id in user_ids:
            try:
                # Получаем данные пользователя
                user = await async_query_db(
                    "SELECT *, COALESCE(subscription_mode, 'multi') as subscription_mode FROM users WHERE telegram_id = ?",
                    (telegram_id,), one=True
                )
                
                if not user:
                    failed_users.append(f"{telegram_id}: пользователь не найден")
                    continue
                
                user = dict(user)
                
                # Удаляем из Remnawave, если провайдер - Remnawave
                if user.get('xui_client_uuid'):
                    try:
                        await remnawave_manager_instance.delete_user(user['xui_client_uuid'])
                        remnawave_deleted += 1
                        current_app.logger.info(f"[BULK DELETE] Пользователь {telegram_id} удален из Remnawave (UUID: {user['xui_client_uuid']})")
                    except Exception as e:
                        remnawave_failed += 1
                        current_app.logger.warning(f"[BULK DELETE] Ошибка при удалении пользователя {telegram_id} из Remnawave: {e}")
                        # Продолжаем удаление из БД даже если Remnawave не удалось удалить
                
                # НЕ удаляем с серверов 3X-UI (как указано в требованиях)
                
                # Удаляем/обнуляем связанные записи из всех таблиц с внешними ключами
                try:
                    await async_execute_db("UPDATE payments SET telegram_id = NULL WHERE telegram_id = ?", (telegram_id,))
                except Exception as e:
                    current_app.logger.warning(f"[BULK DELETE] Не удалось отвязать платежи пользователя {telegram_id}: {e}")
                
                try:
                    await async_execute_db("UPDATE promo_codes SET activated_by_telegram_id = NULL WHERE activated_by_telegram_id = ?", (telegram_id,))
                except Exception as e:
                    current_app.logger.warning(f"[BULK DELETE] Не удалось отвязать промокоды пользователя {telegram_id}: {e}")
                
                try:
                    await async_execute_db("DELETE FROM client_recreation_errors WHERE telegram_id = ?", (telegram_id,))
                except Exception as e:
                    current_app.logger.warning(f"[BULK DELETE] Не удалось удалить ошибки восстановления пользователя {telegram_id}: {e}")
                
                try:
                    await async_execute_db("DELETE FROM promo_redemptions WHERE telegram_id = ?", (telegram_id,))
                except Exception as e:
                    current_app.logger.warning(f"[BULK DELETE] Не удалось удалить активации промокодов пользователя {telegram_id}: {e}")
                
                try:
                    await async_execute_db("DELETE FROM user_enabled_servers WHERE telegram_id = ?", (telegram_id,))
                except Exception as e:
                    current_app.logger.warning(f"[BULK DELETE] Не удалось удалить настройки серверов пользователя {telegram_id}: {e}")
                
                try:
                    await async_execute_db("DELETE FROM partner_accruals WHERE partner_id = ? OR payer_id = ?", (telegram_id, telegram_id))
                except Exception as e:
                    current_app.logger.warning(f"[BULK DELETE] Не удалось удалить партнерские начисления пользователя {telegram_id}: {e}")
                
                try:
                    await async_execute_db(
                        "DELETE FROM referral_payment_bonus WHERE ref_user_id = ? OR invited_user_id = ?",
                        (telegram_id, telegram_id),
                    )
                except Exception as e:
                    current_app.logger.warning(f"[BULK DELETE] Не удалось удалить реферальные бонусы пользователя {telegram_id}: {e}")
                
                try:
                    await async_execute_db("UPDATE users SET invited_by = NULL WHERE invited_by = ?", (telegram_id,))
                except Exception as e:
                    current_app.logger.warning(f"[BULK DELETE] Не удалось обнулить приглашения пользователя {telegram_id}: {e}")
                
                # Удаляем пользователя из БД
                await async_execute_db("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
                deleted_count += 1
                
            except Exception as e:
                failed_users.append(f"{telegram_id}: {str(e)}")
                current_app.logger.error(f"[BULK DELETE] Ошибка при удалении пользователя {telegram_id}: {e}")
        
        message = f'Удалено пользователей: {deleted_count}'
        if remnawave_deleted > 0:
            message += f'. Удалено из Remnawave: {remnawave_deleted}'
        if remnawave_failed > 0:
            message += f'. Ошибок удаления из Remnawave: {remnawave_failed}'
        if failed_users:
            message += f'. Ошибки: {len(failed_users)} пользователей'
        
        return jsonify({
            'success': True,
            'deleted_count': deleted_count,
            'failed_count': len(failed_users),
            'remnawave_deleted': remnawave_deleted,
            'remnawave_failed': remnawave_failed,
            'failed_users': failed_users[:10],  # Первые 10 ошибок
            'message': message
        })
        
    except Exception as e:
        current_app.logger.error(f"[BULK DELETE] Критическая ошибка массового удаления: {e}")
        current_app.logger.debug(f"[BULK DELETE] Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': f'Ошибка при массовом удалении: {str(e)}'}), 500


@api_bp.route('/users/by-uuid/<path:uuid>')
async def api_user_by_uuid(uuid: str):
    """API endpoint для получения Telegram ID пользователя по UUID."""
    try:
        
        # Получаем пользователя по UUID
        user = await async_query_db(
            "SELECT telegram_id FROM users WHERE xui_client_uuid = ? AND COALESCE(is_blocked, 0) = 0",
            (uuid,), one=True
        )
        
        if not user:
            return jsonify({
                'ok': False,
                'error': 'Пользователь с таким UUID не найден'
            }), 404
        
        return jsonify({
            'ok': True,
            'telegram_id': user['telegram_id'],
            'uuid': uuid
        })
    except Exception as e:
        error_traceback = traceback.format_exc()
        current_app.logger.error(f"[API] Ошибка при получении пользователя по UUID {uuid}: {e}\n{error_traceback}")
        return jsonify({
            'ok': False,
            'error': str(e),
            'traceback': error_traceback
        }), 500


@api_bp.route('/users/<int:telegram_id>/set-email', methods=['POST'])
async def set_user_email(telegram_id: int):
    """Устанавливает/обновляет email пользователя.

    Email используется для веб-логина (magic-link), поэтому
    проверяем, что он не занят другим пользователем.
    """
    data = await request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    if email and not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return {'ok': False, 'error': 'Неверный формат email'}
    try:
        if email:
            existing = await async_query_db(
                "SELECT telegram_id, username, real_username FROM users "
                "WHERE LOWER(email) = ? AND telegram_id != ? LIMIT 1",
                (email, telegram_id),
                one=True,
            )
            if existing:
                other_id = existing.get('telegram_id')
                other_name = (
                    existing.get('real_username')
                    or existing.get('username')
                    or f'ID {other_id}'
                )
                return {
                    'ok': False,
                    'error': f'Email уже занят: {other_name} (ID {other_id})',
                    'conflict_telegram_id': other_id,
                }
        await async_execute_db(
            "UPDATE users SET email = ? WHERE telegram_id = ?",
            (email or None, telegram_id)
        )
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@api_bp.route('/users/<int:telegram_id>/send-help', methods=['POST'])
async def send_client_help(telegram_id: int):
    """Отправляет пользователю инструкцию по подключению с кнопками приложений."""
    try:

        # Проверяем, что пользователь существует
        user = await async_query_db(
            "SELECT telegram_id, xui_client_uuid, remnawave_short_uuid FROM users WHERE telegram_id = ?",
            (telegram_id,), one=True
        )
        if not user:
            return jsonify({'ok': False, 'error': 'Пользователь не найден'}), 404

        # Текст сообщения (редактируется в админке: Доп возможности → Помощь клиенту)
        from web_admin.core.help_config import (
            DEFAULT_HELP_PHOTO_ID, DEFAULT_HELP_TEXT, get_effective_buttons,
        )
        text = (app_conf.get('help_text', '') or '').strip() or DEFAULT_HELP_TEXT

        # Кнопки магазинов приложений и «Добавить в Happ» вырезаны вместе
        # с группой «Подключение»: они ставили клиент на телефон, а роутер
        # получает подписку по SSH при активации. Осталась только «Назад».
        builder = InlineKeyboardBuilder()
        for b in get_effective_buttons(app_conf.get('help_buttons', '') or ''):
            if b['kind'] != 'back':
                continue
            kw = {'text': b['text'], 'callback_data': 'back_to_main'}
            if b['icon']:
                kw['icon_custom_emoji_id'] = b['icon']
            if b['style']:
                kw['style'] = b['style']
            builder.row(InlineKeyboardButton(**kw))

        # Фото-инструкция. file_id берётся из настройки `help_photo_file_id`,
        # если её нет — используется значение по умолчанию (привязано к
        # конкретному боту, при смене токена нужно перезагрузить фото в админке:
        # Доп возможности → Помощь клиенту).
        help_photo_id = (app_conf.get('help_photo_file_id', '') or '').strip() or DEFAULT_HELP_PHOTO_ID

        markup = builder.as_markup()
        try:
            await send_telegram_photo(
                telegram_id, help_photo_id, caption=text, reply_markup=markup
            )
        except Exception as photo_err:
            # Telegram file_id привязан к конкретному боту: при смене токена
            # старый id будет невалиден. Логируем и фолбэкаем на текст.
            current_app.logger.warning(
                f"[API] send_photo не удался для {telegram_id}: {photo_err}. "
                f"Отправляем текстовое сообщение."
            )
            await send_telegram_message(telegram_id, text, reply_markup=markup)
        return jsonify({'ok': True, 'message': 'Инструкция отправлена'})

    except Exception as e:
        current_app.logger.error(f"[API] Ошибка отправки помощи пользователю {telegram_id}: {e}\n{traceback.format_exc()}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@api_bp.route('/help-photo/upload', methods=['POST'])
async def upload_help_photo():
    """Загружает фото-инструкцию: шлёт его админу через бота (чтобы получить
    file_id), сохраняет file_id в settings.help_photo_file_id и путь для превью
    в settings.help_photo_local. Возвращает file_id и preview_url."""
    if session.get('admin_role') == 'moderator':
        return jsonify({'ok': False, 'error': 'Недостаточно прав.'}), 403

    from aiogram.types import FSInputFile
    from src.telegram_bot_factory import make_aiogram_bot
    from tg_sender import async_get_bot_token
    from web_admin.routes.news import (
        _detect_kind, _sanitize_filename, _build_preview_url, _get_admin_ids_list,
    )

    try:
        files = await request.files
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Не удалось прочитать форму: {e}'}), 400
    f = files.get('file')
    if not f:
        return jsonify({'ok': False, 'error': 'Файл не получен (поле "file").'}), 400

    filename = f.filename or 'help.jpg'
    content_type = f.content_type or ''
    kind = _detect_kind(filename, content_type)
    if kind != 'photo':
        return jsonify({'ok': False, 'error': 'Доступно только фото: JPG / PNG / WEBP.'}), 400

    try:
        data = await asyncio.to_thread(f.read)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Не удалось прочитать файл: {e}'}), 400
    size = len(data) if data else 0
    if size == 0:
        return jsonify({'ok': False, 'error': 'Файл пуст.'}), 400
    if size > 10 * 1024 * 1024:
        return jsonify({'ok': False, 'error': f'Фото слишком большое: {size/1024/1024:.1f} МБ (лимит 10 МБ).'}), 400

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    yyyy_mm = datetime.now().strftime('%Y-%m')
    base_dir = os.path.join(project_root, 'media', 'news', 'uploads', yyyy_mm)
    try:
        os.makedirs(base_dir, exist_ok=True)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Не удалось создать папку: {e}'}), 500

    safe_name = _sanitize_filename(filename)
    final_name = f"help_{int(time.time() * 1000)}_{safe_name}"
    local_path = os.path.join(base_dir, final_name)
    rel_path = (yyyy_mm + '/' + final_name).replace('\\', '/')
    try:
        async with aiofiles.open(local_path, 'wb') as out:
            await out.write(data)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Не удалось записать файл: {e}'}), 500

    admin_ids = await _get_admin_ids_list()
    if not admin_ids:
        try: os.remove(local_path)
        except Exception: pass
        return jsonify({'ok': False, 'error': 'Не настроены администраторы (admin_ids).'}), 503

    bot_token = await async_get_bot_token()
    if not bot_token:
        try: os.remove(local_path)
        except Exception: pass
        return jsonify({'ok': False, 'error': 'Bot token не найден.'}), 503

    proxy_url = (app_conf.get('telegram_proxy_url') or '').strip() or None
    bot = make_aiogram_bot(bot_token, proxy_url)
    file_id = None
    last_err = None
    try:
        for adm in admin_ids:
            try:
                msg = await bot.send_photo(
                    adm, FSInputFile(local_path),
                    caption="🖼 Фото-инструкция «Помощь клиенту» загружено в админке.",
                )
                if msg and msg.photo:
                    file_id = msg.photo[-1].file_id
                    break
            except Exception as e:
                last_err = e
                continue
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass

    if not file_id:
        try: os.remove(local_path)
        except Exception: pass
        return jsonify({
            'ok': False,
            'error': 'Не удалось зарегистрировать фото в Telegram (бот заблокирован у админов?).',
            'detail': (str(last_err)[:300] if last_err else None),
        }), 502

    try:
        await async_execute_db(
            "INSERT INTO settings (key, value) VALUES ('help_photo_file_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (file_id,)
        )
        await async_execute_db(
            "INSERT INTO settings (key, value) VALUES ('help_photo_local', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (rel_path,)
        )
        await app_conf.load_settings()
    except Exception as e:
        current_app.logger.error(f"[API] Не удалось сохранить help_photo_file_id: {e}")
        return jsonify({'ok': False, 'error': f'Файл загружен, но не сохранён: {e}'}), 500

    return jsonify({
        'ok': True,
        'file_id': file_id,
        'preview_url': _build_preview_url(rel_path),
    })


