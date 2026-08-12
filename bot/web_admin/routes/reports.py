from quart import render_template, jsonify, request
from datetime import datetime, timezone
import logging
import aiosqlite
import json
import os
import sys

logger = logging.getLogger(__name__)


def _format_first_seen_utc(raw: str) -> str:
    """Человекочитаемое время первого появления устройства (UTC)."""
    if not raw:
        return '—'
    try:
        s = raw.replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime('%d.%m.%Y %H:%M') + ' UTC'
    except Exception:
        return raw[:19] if len(raw) > 19 else raw


def _short_hwid(hwid: str, front: int = 10, back: int = 6) -> str:
    h = hwid or ''
    if len(h) <= front + back + 1:
        return h
    return f'{h[:front]}…{h[-back:]}'


def _device_stats_user_display(username: str, telegram_id) -> tuple[str, bool]:
    """Подпись пользователя для отчёта и флаг «найден в users»."""
    tid = telegram_id
    if tid is None:
        return ('Не привязан к клиенту', False)
    un = (username or '').strip()
    if un:
        label = un[1:] if un.startswith('@') else un
        return (label, True)
    return (str(tid), True)


def _devices_db_check():
    """Проверка доступности devices.db. None если ок, иначе (response, status) для return."""
    try:
        from devices.config import DEVICES_DB_PATH as PARSER_DB_PATH
    except ImportError as import_err:
        logger.warning(f"Модуль парсера логов не найден: {import_err}")
        return jsonify({
            'success': False,
            'error': 'Модуль парсера логов не установлен. Убедитесь, что парсер установлен и доступен.'
        }), 404

    parser_db_path = os.path.abspath(PARSER_DB_PATH)
    if not os.path.exists(parser_db_path):
        logger.warning(f"База данных парсера не найдена: {parser_db_path}")
        return jsonify({
            'success': False,
            'error': 'База данных парсера логов не найдена. Убедитесь, что парсер установлен и запущен.'
        }), 404

    return None


def attach_reports_routes(admin_bp_instance, query_db_func, execute_db_func):
    """Прикрепляет роуты для раздела Отчеты"""
    
    @admin_bp_instance.route('/reports')
    async def reports_index():
        """Главная страница раздела Отчеты"""
        return await render_template('reports_index.html')
    
    @admin_bp_instance.route('/reports/subscription-access')
    async def reports_subscription_access():
        """Страница с последними обновлениями подписки в реальном времени"""
        return await render_template('reports_subscription_access.html')
    
    @admin_bp_instance.route('/api/reports/subscription-access')
    async def api_subscription_access():
        """API endpoint для получения последних записей доступа к подпискам"""
        try:
            from devices.database import get_recent_access_records
            
            limit = request.args.get('limit', 30, type=int)
            if limit > 100:
                limit = 100  # Максимум 100 записей
            
            records = await get_recent_access_records(limit=limit * 2)  # Берем больше, чтобы после фильтрации осталось достаточно
            
            # Функция для определения браузера
            def is_browser(user_agent: str) -> bool:
                """Определяет, является ли запрос от браузера"""
                if not user_agent:
                    return False
                ua_lower = user_agent.lower()
                browser_indicators = ['mozilla', 'chrome', 'safari', 'firefox', 'edge', 'opera', 'msie', 'yabrowser', 'ya-browser']
                app_indicators = [
                    'clash', 'v2ray', 'v2raytun', 'v2rayn', 'v2rayng', 'v2box', 'sing-box',
                    'shadowrocket', 'happ', 'hiddify', 'shadowsocks', 'nekoray', 'nekobox',
                    'mihomo', 'clash-verge', 'clash-meta'
                ]
                # Если есть индикаторы приложений, это не браузер
                if any(indicator in ua_lower for indicator in app_indicators):
                    return False
                # Если есть индикаторы браузера, это браузер
                return any(indicator in ua_lower for indicator in browser_indicators)
            
            # Фильтруем браузерные запросы
            filtered_records = []
            for record in records:
                user_agent = record.get('user_agent') or ''
                if not is_browser(user_agent):
                    filtered_records.append(record)
                    if len(filtered_records) >= limit:
                        break
            
            # Форматируем данные для удобного отображения
            from datetime import timezone
            try:
                import pytz
                _MSK_TZ = pytz.timezone('Europe/Moscow')
            except Exception:
                _MSK_TZ = None
            formatted_records = []
            for record in filtered_records:
                # Парсим время доступа.
                # В БД оно пишется как datetime.utcnow().isoformat() — naive UTC.
                # Считаем naive как UTC и выводим в МСК.
                try:
                    access_time = datetime.fromisoformat(record['access_time'].replace('Z', '+00:00'))
                    if access_time.tzinfo is None:
                        access_time = access_time.replace(tzinfo=timezone.utc)
                    if _MSK_TZ is not None:
                        access_time_msk = access_time.astimezone(_MSK_TZ)
                    else:
                        access_time_msk = access_time
                    access_time_str = access_time_msk.strftime('%d.%m.%Y %H:%M:%S')
                    time_ago = _format_time_ago(access_time)
                except Exception:
                    access_time_str = record['access_time']
                    time_ago = '—'
                
                formatted_records.append({
                    'id': record['id'],
                    'uuid': record['uuid'],
                    'ip_address': record['ip_address'],
                    'hwid': record['hwid'] or '—',
                    'os': record['os'] or '—',
                    'os_version': record['os_version'] or '—',
                    'model': record['model'] or '—',
                    'user_agent': record['user_agent'] or '—',
                    'access_time': access_time_str,
                    'time_ago': time_ago,
                    'method': record['method'] or '—',
                    'path': record['path'] or '—',
                    'status_code': record['status_code'] or '—',
                    'response_size': _format_bytes(record['response_size']) if record['response_size'] else '—'
                })
            
            return jsonify({
                'success': True,
                'records': formatted_records,
                'total': len(formatted_records)
            })
        except ImportError as e:
            logger.warning(f"Ошибка импорта subscription_log_parser: {e}")
            return jsonify({
                'success': False,
                'error': 'Модуль парсера логов не доступен. Убедитесь, что парсер установлен.'
            }), 404
        except Exception as e:
            logger.error(f"Ошибка при получении записей доступа: {e}", exc_info=True)
            return jsonify({
                'success': False,
                'error': f'Ошибка при получении данных: {str(e)}'
            }), 500
    
    @admin_bp_instance.route('/reports/device-limit-violations')
    async def reports_device_limit_violations():
        """Страница с проверкой нарушителей лимита устройств"""
        return await render_template('reports_device_limit_violations.html')
    
    @admin_bp_instance.route('/api/reports/check-device-violations', methods=['POST'])
    async def api_check_device_violations():
        """API endpoint для проверки нарушителей лимита устройств"""
        try:
            err_resp = _devices_db_check()
            if err_resp is not None:
                return err_resp

            from devices.database import get_device_violations
            from web_admin.async_db import async_query_db
            
            # Получаем данные из базы парсера (без списка HWID для скорости)
            violations_data = await get_device_violations(include_hwid_list=False)
            
            if not violations_data or not isinstance(violations_data, list):
                return jsonify({
                    'success': True,
                    'violations': [],
                    'message': 'Нарушителей не найдено'
                })
            
            # Оптимизация: получаем всех пользователей одним запросом (избегаем N+1 проблемы)
            # Убеждаемся, что violations_data содержит словари
            uuids = []
            for v in violations_data:
                if isinstance(v, dict):
                    uuid_val = v.get('uuid')
                    if uuid_val:
                        uuids.append(uuid_val)
                else:
                    # Если это Row объект, преобразуем в словарь
                    v_dict = dict(v)
                    uuid_val = v_dict.get('uuid')
                    if uuid_val:
                        uuids.append(uuid_val)
            
            if not uuids:
                return jsonify({
                    'success': True,
                    'violations': [],
                    'message': 'Нарушителей не найдено'
                })
            
            placeholders = ','.join(['?'] * len(uuids))
            
            users_data = await async_query_db(
                f"SELECT telegram_id, username, xui_client_email, limit_ip, subscription_end_date, xui_client_uuid FROM users WHERE xui_client_uuid IN ({placeholders})",
                tuple(uuids)
            )
            
            # Создаем словарь для быстрого поиска пользователей по UUID
            # async_query_db возвращает список словарей
            users_by_uuid = {}
            if users_data and isinstance(users_data, list):
                for user in users_data:
                    # Преобразуем в словарь, если это еще не словарь
                    if not isinstance(user, dict):
                        user = dict(user)
                    uuid_key = user.get('xui_client_uuid')
                    if uuid_key:
                        users_by_uuid[uuid_key] = user
            
            # Получаем информацию о пользователях из основной БД
            violations_with_users = []
            
            for violation in violations_data:
                # Убеждаемся, что violation - это словарь
                if not isinstance(violation, dict):
                    violation = dict(violation)
                
                uuid = violation.get('uuid')
                if not uuid:
                    continue
                
                # Ищем пользователя в словаре (O(1) вместо запроса к БД)
                user_data = users_by_uuid.get(uuid)
                
                if user_data:
                    limit_ip = user_data.get('limit_ip', 0) or 0
                    device_count = violation['device_count']
                    
                    # Пропускаем записи без устройств
                    if device_count == 0:
                        continue
                    
                    # Проверяем, превышен ли лимит (только если лимит установлен и превышен)
                    if limit_ip > 0 and device_count > limit_ip:
                        # Получаем список HWID только для нарушителей (ленивая загрузка)
                        from devices.database import get_hwid_list_for_uuid
                        try:
                            hwid_list = await get_hwid_list_for_uuid(uuid)
                            # Убеждаемся, что это список
                            if not isinstance(hwid_list, list):
                                hwid_list = []
                            logger.debug(f"Получено {len(hwid_list)} HWID для UUID {uuid}, device_count={device_count}")
                        except Exception as e:
                            logger.error(f"Ошибка получения HWID для UUID {uuid}: {e}")
                            hwid_list = []
                        
                        violation_info = {
                            'uuid': uuid,
                            'telegram_id': user_data.get('telegram_id'),
                            'username': user_data.get('username', 'N/A'),
                            'email': user_data.get('xui_client_email', 'N/A'),
                            'limit_ip': limit_ip,
                            'device_count': device_count,
                            'excess_count': device_count - limit_ip,
                            'hwid_list': hwid_list if isinstance(hwid_list, list) else [],
                            'first_access': violation.get('first_access'),
                            'last_access': violation.get('last_access'),
                            'subscription_end_date': user_data.get('subscription_end_date')
                        }
                        violations_with_users.append(violation_info)
            
            # Сортируем по количеству превышения (от большего к меньшему)
            violations_with_users.sort(key=lambda x: x['excess_count'], reverse=True)
            
            return jsonify({
                'success': True,
                'violations': violations_with_users,
                'total': len(violations_with_users)
            })
            
        except FileNotFoundError:
            return jsonify({
                'success': False,
                'error': 'База данных парсера логов не найдена. Убедитесь, что парсер установлен и запущен.'
            }), 404
        except ImportError as import_err:
            logger.warning(f"Ошибка импорта модуля парсера: {import_err}")
            return jsonify({
                'success': False,
                'error': 'Модуль парсера логов не установлен. Убедитесь, что парсер установлен и доступен.'
            }), 404
        except Exception as e:
            import traceback
            error_traceback = traceback.format_exc()
            logger.error(f"Ошибка при проверке нарушителей лимита устройств: {e}\n{error_traceback}")
            return jsonify({
                'success': False,
                'error': f'Ошибка при проверке: {str(e)}'
            }), 500

    @admin_bp_instance.route('/api/reports/check-duplicate-hwids', methods=['POST'])
    async def api_check_duplicate_hwids():
        """API: один HWID на нескольких клиентах (возможный абуз триала и т.п.)."""
        try:
            err_resp = _devices_db_check()
            if err_resp is not None:
                return err_resp

            from devices.database import get_duplicate_hwids
            from web_admin.async_db import async_query_db

            duplicates_data = await get_duplicate_hwids()
            if not duplicates_data:
                return jsonify({
                    'success': True,
                    'duplicates': [],
                    'message': 'Дубликатов HWID не найдено',
                })

            uuids = set()
            for item in duplicates_data:
                for client in item.get('clients') or []:
                    uuid_val = client.get('uuid')
                    if uuid_val:
                        uuids.add(uuid_val)

            users_by_uuid = {}
            if uuids:
                placeholders = ','.join(['?'] * len(uuids))
                users_data = await async_query_db(
                    f"""SELECT telegram_id, username, xui_client_email, limit_ip,
                               subscription_end_date, xui_client_uuid, is_trial_used
                        FROM users WHERE xui_client_uuid IN ({placeholders})""",
                    tuple(uuids),
                )
                if users_data and isinstance(users_data, list):
                    for user in users_data:
                        if not isinstance(user, dict):
                            user = dict(user)
                        uuid_key = user.get('xui_client_uuid')
                        if uuid_key:
                            users_by_uuid[uuid_key] = user

            enriched = []
            for item in duplicates_data:
                clients_out = []
                trial_count = 0
                for client in item.get('clients') or []:
                    uuid = client.get('uuid')
                    user_data = users_by_uuid.get(uuid) if uuid else None
                    is_trial = bool(user_data and user_data.get('is_trial_used'))
                    if is_trial:
                        trial_count += 1
                    clients_out.append({
                        **client,
                        'telegram_id': user_data.get('telegram_id') if user_data else None,
                        'username': user_data.get('username') if user_data else None,
                        'email': user_data.get('xui_client_email') if user_data else None,
                        'limit_ip': user_data.get('limit_ip') if user_data else None,
                        'subscription_end_date': user_data.get('subscription_end_date') if user_data else None,
                        'is_trial': is_trial,
                        'in_users': user_data is not None,
                    })
                enriched.append({
                    'hwid': item.get('hwid'),
                    'client_count': item.get('client_count') or len(clients_out),
                    'trial_count': trial_count,
                    'clients': clients_out,
                })

            enriched.sort(key=lambda x: (x.get('client_count') or 0, x.get('trial_count') or 0), reverse=True)

            return jsonify({
                'success': True,
                'duplicates': enriched,
                'total': len(enriched),
            })

        except FileNotFoundError:
            return jsonify({
                'success': False,
                'error': 'База данных парсера логов не найдена. Убедитесь, что парсер установлен и запущен.',
            }), 404
        except ImportError as import_err:
            logger.warning(f"Ошибка импорта модуля парсера: {import_err}")
            return jsonify({
                'success': False,
                'error': 'Модуль парсера логов не установлен. Убедитесь, что парсер установлен и доступен.',
            }), 404
        except Exception as e:
            import traceback
            error_traceback = traceback.format_exc()
            logger.error(f"Ошибка при проверке дубликатов HWID: {e}\n{error_traceback}")
            return jsonify({
                'success': False,
                'error': f'Ошибка при проверке: {str(e)}',
            }), 500
    
    # ⚠️ ОПАСНЫЕ ОПЕРАЦИИ
    # Эндпоинт безвозвратно стирает таблицу subscription_access в devices.db
    # (логи доступа по HWID, на которых строится отчёт о нарушениях лимита).
    @admin_bp_instance.route('/api/reports/clear-subscription-access-db', methods=['POST'])
    async def api_clear_subscription_access_db():
        """API endpoint для полной очистки базы данных subscription_access.db"""
        try:
            try:
                from devices.database import get_db_connection
                from devices.config import DEVICES_DB_PATH as PARSER_DB_PATH
            except ImportError as import_err:
                logger.warning(f"Модуль парсера логов не найден: {import_err}")
                return jsonify({
                    'success': False,
                    'error': 'Модуль парсера логов не установлен. Убедитесь, что парсер установлен и доступен.'
                }), 404
            
            import os
            
            # Проверяем существование базы парсера
            parser_db_path = os.path.abspath(PARSER_DB_PATH)
            if not os.path.exists(parser_db_path):
                logger.warning(f"База данных парсера не найдена: {parser_db_path}")
                return jsonify({
                    'success': False,
                    'error': 'База данных парсера логов не найдена. Убедитесь, что парсер установлен и запущен.'
                }), 404
            
            # Получаем количество записей перед удалением
            async with get_db_connection() as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute('SELECT COUNT(*) as count FROM subscription_access')
                count_row = await cursor.fetchone()
                deleted_count = count_row['count'] if count_row else 0
                
                # Удаляем все записи
                await db.execute('DELETE FROM subscription_access')
                await db.commit()
            
            logger.info(f"[REPORTS] База данных subscription_access.db очищена. Удалено записей: {deleted_count}")
            
            return jsonify({
                'success': True,
                'deleted_count': deleted_count,
                'message': f'База данных успешно очищена. Удалено записей: {deleted_count}'
            })
            
        except ImportError as import_err:
            logger.warning(f"Ошибка импорта модуля парсера: {import_err}")
            return jsonify({
                'success': False,
                'error': 'Модуль парсера логов не установлен. Убедитесь, что парсер установлен и доступен.'
            }), 404
        except Exception as e:
            import traceback
            error_traceback = traceback.format_exc()
            logger.error(f"Ошибка при очистке базы данных subscription_access.db: {e}\n{error_traceback}")
            return jsonify({
                'success': False,
                'error': f'Ошибка при очистке: {str(e)}'
            }), 500


    @admin_bp_instance.route('/reports/device-stats')
    async def reports_device_stats():
        """Страница статистики устройств: ОС и приложения"""
        return await render_template('reports_device_stats.html')

    @admin_bp_instance.route('/api/reports/device-stats')
    async def api_device_stats():
        """JSON-данные для статистики устройств"""
        try:
            from devices.database import (
                get_device_stats,
                count_new_app_devices_first_seen_today,
                count_stale_active_subscription_users,
            )

            stats = await get_device_stats()
            today_new = await count_new_app_devices_first_seen_today()
            stale_stats = await count_stale_active_subscription_users(stale_hours=24)
            return jsonify({
                'success': True,
                **stats,
                'today_new_devices': today_new,
                **stale_stats,
            })
        except ImportError:
            return jsonify({'success': False, 'error': 'Модуль devices недоступен'}), 404
        except Exception as e:
            logger.error(f"[REPORTS] Ошибка статистики устройств: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @admin_bp_instance.route('/api/reports/device-stats/today-new-devices')
    async def api_device_stats_today_new_devices():
        """Новые устройства (HWID) за календарные сутки UTC с пагинацией."""
        try:
            from devices.database import (
                get_new_app_devices_first_seen_today_paginated,
                classify_subscription_user_agent,
            )
            import db_helpers

            page = request.args.get('page', 1, type=int) or 1
            per_page = request.args.get('per_page', 20, type=int) or 20

            batch = await get_new_app_devices_first_seen_today_paginated(
                page=max(1, page),
                per_page=max(1, min(per_page, 100)),
            )
            uuids = [it['uuid'] for it in batch['items'] if it.get('uuid')]
            users_map = await db_helpers.get_users_by_subscription_uuids(uuids)

            items_out = []
            for it in batch['items']:
                uid = it.get('uuid') or ''
                uinfo = users_map.get(uid) if uid else None
                tid = uinfo['telegram_id'] if uinfo else None
                uname = uinfo['username'] if uinfo else ''
                label, resolved = _device_stats_user_display(uname, tid)

                app_name = classify_subscription_user_agent(it.get('user_agent') or '')
                first_raw = it.get('first_seen_raw') or ''
                items_out.append({
                    'first_seen_raw': first_raw,
                    'first_seen_display': _format_first_seen_utc(first_raw),
                    'uuid': uid,
                    'uuid_short': (uid[:8] + '…') if len(uid) > 10 else uid,
                    'hwid': it.get('hwid') or '',
                    'hwid_short': _short_hwid(it.get('hwid') or ''),
                    'os': it.get('os') or '',
                    'os_version': it.get('os_version') or '',
                    'model': it.get('model') or '',
                    'user_agent': it.get('user_agent') or '',
                    'app_name': app_name,
                    'ip_address': it.get('ip_address') or '',
                    'telegram_id': tid,
                    'username': uname,
                    'user_display': label,
                    'user_resolved': resolved,
                    'access_count': it.get('access_count') or 1,
                })

            return jsonify({
                'success': True,
                'total': batch['total'],
                'page': batch['page'],
                'per_page': batch['per_page'],
                'pages': batch['pages'],
                'items': items_out,
                'day_note': 'Только устройства, впервые зарегистрированные сегодня (UTC), по полю created_at — не по последнему обновлению подписки. Приложения подписки с HWID.',
            })
        except ImportError:
            return jsonify({'success': False, 'error': 'Модуль devices недоступен'}), 404
        except Exception as e:
            logger.error(f"[REPORTS] Ошибка списка новых устройств за сегодня: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @admin_bp_instance.route('/api/reports/device-stats/stale-subscriptions')
    async def api_device_stats_stale_subscriptions():
        """Активные клиенты без обновления текстовой подписки дольше порога (обращения приложений)."""
        try:
            from devices.database import get_stale_active_subscription_users_paginated

            page = request.args.get('page', 1, type=int) or 1
            per_page = request.args.get('per_page', 20, type=int) or 20
            stale_hours = request.args.get('hours', 24, type=int) or 24

            batch = await get_stale_active_subscription_users_paginated(
                page=max(1, page),
                per_page=max(1, min(per_page, 100)),
                stale_hours=max(1, min(stale_hours, 24 * 30)),
            )

            items_out = []
            for it in batch['items']:
                tid = it.get('telegram_id')
                uname = it.get('username') or ''
                label, resolved = _device_stats_user_display(uname, tid)
                last_raw = it.get('last_access_raw') or ''
                if it.get('never_accessed'):
                    last_display = 'нет обращений'
                    time_ago = '—'
                else:
                    last_display = _format_first_seen_utc(last_raw)
                    dt = None
                    try:
                        s = last_raw.replace('Z', '+00:00')
                        dt = datetime.fromisoformat(s)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass
                    time_ago = _format_time_ago(dt) if dt else '—'

                items_out.append({
                    'uuid': it.get('uuid') or '',
                    'uuid_short': ((it.get('uuid') or '')[:8] + '…') if len(it.get('uuid') or '') > 10 else (it.get('uuid') or ''),
                    'telegram_id': tid,
                    'username': uname,
                    'user_display': label,
                    'user_resolved': resolved,
                    'last_access_raw': last_raw,
                    'last_access_display': last_display,
                    'time_ago': time_ago,
                    'never_accessed': bool(it.get('never_accessed')),
                    'subscription_end_date': it.get('subscription_end_date') or '',
                })

            hours = batch.get('stale_hours', 24)
            return jsonify({
                'success': True,
                'total': batch['total'],
                'page': batch['page'],
                'per_page': batch['per_page'],
                'pages': batch['pages'],
                'stale_hours': hours,
                'active_subscription_users': batch.get('active_subscription_users', 0),
                'items': items_out,
                'day_note': (
                    f'Только активные подписки, не обновлявшиеся более {hours} ч. '
                    f'(по последнему access_time в журнале устройств).'
                ),
            })
        except ImportError:
            return jsonify({'success': False, 'error': 'Модуль devices недоступен'}), 404
        except Exception as e:
            logger.error(f"[REPORTS] Ошибка списка устаревших подписок: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500


def _format_time_ago(dt: datetime) -> str:
    """Форматирует время в формат 'X секунд/минут/часов назад'.

    Время в БД (subscription_access.access_time) пишется как
    datetime.utcnow().isoformat() — naive UTC. Поэтому naive datetime
    трактуем как UTC, чтобы корректно сравнивать с текущим временем
    независимо от часового пояса сервера (MSK/UTC и т.д.).
    """
    from datetime import timezone
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - dt
    total_seconds = delta.total_seconds()

    # Защита от отрицательной разницы (рассинхрон часов): считаем как «только что»
    if total_seconds < 0:
        return 'только что'

    if total_seconds < 60:
        seconds = int(total_seconds)
        if seconds == 0:
            return 'только что'
        return f'{seconds} сек. назад'
    elif total_seconds < 3600:
        minutes = int(total_seconds / 60)
        return f'{minutes} мин. назад'
    elif total_seconds < 86400:
        hours = int(total_seconds / 3600)
        return f'{hours} ч. назад'
    else:
        days = int(total_seconds / 86400)
        return f'{days} дн. назад'


def _format_bytes(bytes_value: int) -> str:
    """Форматирует байты в читаемый формат"""
    if bytes_value is None:
        return '—'
    
    for unit in ['Б', 'КБ', 'МБ', 'ГБ']:
        if bytes_value < 1024.0:
            return f'{bytes_value:.1f} {unit}'
        bytes_value /= 1024.0
    return f'{bytes_value:.1f} ТБ'

