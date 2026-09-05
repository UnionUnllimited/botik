from quart import render_template, request, redirect, url_for, flash, jsonify, session
import json


def attach_payment_routes(admin_bp):
    async def payments_view():
        page = request.args.get('page', 1, type=int)
        per_page = 20
        offset = (page - 1) * per_page
        status_filter = request.args.get('status', 'all')
        # Поисковый запрос: по payment_id, telegram_id, username, real_username, email, xui_client_email
        search_query = (request.args.get('q') or '').strip()
        from web_admin.async_db import async_query_db, async_execute_db

        # Строим динамический WHERE: статус + (опционально) поиск
        where_parts = []
        where_args = []

        if status_filter == 'succeeded':
            where_parts.append("status = 'succeeded'")
        elif status_filter == 'processing':
            where_parts.append("status = 'processing'")
        elif status_filter == 'pending':
            where_parts.append("status = 'pending'")
        elif status_filter == 'failed':
            where_parts.append("status NOT IN ('succeeded', 'processing', 'pending')")

        if search_query:
            # Сначала ищем подходящих юзеров (по username/real_username/email/xui_client_email)
            user_like = f"%{search_query}%"
            matched_user_rows = await async_query_db(
                """SELECT telegram_id FROM users
                   WHERE COALESCE(username,'')         LIKE ? COLLATE NOCASE
                      OR COALESCE(real_username,'')    LIKE ? COLLATE NOCASE
                      OR COALESCE(email,'')            LIKE ? COLLATE NOCASE
                      OR COALESCE(xui_client_email,'') LIKE ? COLLATE NOCASE
                   LIMIT 500""",
                (user_like, user_like, user_like, user_like)
            )
            matched_tg_ids = [int(dict(r)['telegram_id']) for r in matched_user_rows if dict(r).get('telegram_id') is not None]

            search_or = []
            search_args = []
            # ищем по payment_id (содержит подстроку)
            search_or.append("payment_id LIKE ? COLLATE NOCASE")
            search_args.append(user_like)
            # если запрос — число, ищем по telegram_id напрямую
            if search_query.lstrip('-').isdigit():
                search_or.append("telegram_id = ?")
                search_args.append(int(search_query))
            # юзеры, найденные по username/email
            if matched_tg_ids:
                placeholders = ','.join(['?'] * len(matched_tg_ids))
                search_or.append(f"telegram_id IN ({placeholders})")
                search_args.extend(matched_tg_ids)
            where_parts.append("(" + " OR ".join(search_or) + ")")
            where_args.extend(search_args)

        where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

        payments = await async_query_db(
            f"SELECT * FROM payments {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(where_args) + (per_page, offset)
        )
        total_row = await async_query_db(
            f"SELECT COUNT(*) AS cnt FROM payments {where_sql}",
            tuple(where_args), one=True
        )
        total = (total_row['cnt'] if total_row else 0)
        total_pages = (total + per_page - 1) // per_page

        # Получаем username и registration_type для каждого платежа
        user_ids = [dict(p)['telegram_id'] for p in payments]
        users = {}
        user_sources = {}
        if user_ids:
            q = f"SELECT telegram_id, username, real_username, registration_type FROM users WHERE telegram_id IN ({','.join(['?']*len(user_ids))})"
            for u in (await async_query_db(q, user_ids)):
                u_dict = dict(u)
                # Используем username (first_name) - может быть кириллицей, английским и со смайликами
                # Если username нет, используем real_username без @, иначе telegram_id
                display_name = u_dict.get('username')
                if not display_name:
                    real_username = u_dict.get('real_username')
                    if real_username:
                        # Убираем @ если он есть в начале
                        display_name = real_username.lstrip('@')
                    else:
                        display_name = str(u_dict['telegram_id'])
                users[u_dict['telegram_id']] = display_name
                # Источник регистрации: 'site' для веб, иначе 'telegram'
                reg_type = (u_dict.get('registration_type') or '').lower()
                if reg_type == 'site' or int(u_dict['telegram_id']) >= 10000000000:
                    user_sources[u_dict['telegram_id']] = 'site'
                else:
                    user_sources[u_dict['telegram_id']] = 'telegram'

        # Извлекаем информацию о тарифах и методах платежа из metadata_json
        tariff_ids = set()
        traffic_topup_tariff_ids = set()
        payments_with_metadata = []
        for p in payments:
            p_dict = dict(p)
            metadata_json = p_dict.get('metadata_json')
            tariff_info = None
            payment_method = None
            payment_type = None
            
            # Определяем метод платежа из payment_id или metadata
            payment_id = p_dict.get('payment_id', '')
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
                    if payment_type == 'traffic_renewal':
                        # Для продления трафика используем тарифы докупки трафика
                        tariff_id = metadata.get('tariff_id')
                        traffic_to_add_gb = metadata.get('traffic_to_add_gb')
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
                    elif payment_type == 'device_limit_upgrade':
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
                    elif payment_type in ('router_order', 'delivery'):
                        # Платёж из магазина роутеров: за ним заказ, а не тариф.
                        tariff_info = {
                            'type': payment_type,
                            'order_number': metadata.get('order_number') or '',
                            'title': metadata.get('description') or '',
                        }
                    else:
                        # Для обычных платежей используем тарифы подписки
                        tariff_id = metadata.get('tariff_id')
                        subscription_days = metadata.get('subscription_days') or metadata.get('days')  # Поддержка разных форматов
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
                                # Если tariff_id не число, используем только дни
                                if subscription_days:
                                    tariff_info = {'type': 'subscription', 'days': subscription_days, 'limit_ip': md_limit_ip}
                                else:
                                    tariff_info = None
                        elif subscription_days:
                            # Если нет tariff_id, но есть дни, показываем только дни
                            tariff_info = {'type': 'subscription', 'days': subscription_days, 'limit_ip': md_limit_ip}
                        else:
                            tariff_info = None
                except:
                    pass
            
            # Если метод все еще не определен, используем "Неизвестно"
            if not payment_method:
                payment_method = 'Неизвестно'
            
            # Нормализуем название метода для CSS класса (убираем пробелы, приводим к нижнему регистру)
            payment_method_normalized = payment_method.lower().replace(' ', '-').replace('_', '-')
            # Специальная обработка для "TG Star" -> "tg-star"
            if payment_method_normalized == 'tg-star':
                payment_method_normalized = 'tg-star'
            elif payment_method_normalized == 'tgstar':
                payment_method_normalized = 'tg-star'
            
            p_dict['tariff_info'] = tariff_info
            p_dict['payment_method'] = payment_method
            p_dict['payment_method_normalized'] = payment_method_normalized
            p_dict['payment_type'] = payment_type
            # Источник регистрации клиента
            p_dict['registration_source'] = user_sources.get(p_dict.get('telegram_id'))
            # Извлекаем ID без префикса для копирования
            if payment_id:
                # Убираем префиксы типа "PLATEGA_", "YOOMONEY_", "YK_", "CRYPTO_", "CRYPTOBOT_" и т.д.
                clean_id = payment_id
                prefixes = ['WATA_', 'PLATEGA_', 'YOOMONEY_', 'YK_', 'CRYPTOBOT_', 'CRYPTO_', 'TGSTAR_', 'USDT_']
                for prefix in prefixes:
                    if clean_id.startswith(prefix):
                        clean_id = clean_id[len(prefix):]
                        break
                p_dict['payment_id_clean'] = clean_id
            else:
                p_dict['payment_id_clean'] = payment_id
            
            payments_with_metadata.append(p_dict)

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

        # Статистика — только успешные RUB-платежи
        stats = {}
        # Всего
        row = await async_query_db("SELECT COUNT(*) AS cnt, SUM(amount) AS total FROM payments WHERE status = 'succeeded' AND currency = 'RUB'", (), one=True)
        stats['all_count'] = (row[0] if isinstance(row, tuple) else row['cnt']) or 0
        stats['all_sum']   = (row[1] if isinstance(row, tuple) else row['total']) or 0
        stats['by_currency'] = []
        # За месяц
        row = await async_query_db("SELECT COUNT(*) AS cnt, SUM(amount) AS total FROM payments WHERE status = 'succeeded' AND currency = 'RUB' AND datetime(created_at) >= datetime('now', '+3 hours', 'start of month', '-3 hours')", (), one=True)
        stats['month_count'] = (row[0] if isinstance(row, tuple) else row['cnt']) or 0
        stats['month_sum']   = (row[1] if isinstance(row, tuple) else row['total']) or 0
        stats['month_by_currency'] = []
        # За неделю
        row = await async_query_db("SELECT COUNT(*) AS cnt, SUM(amount) AS total FROM payments WHERE status = 'succeeded' AND currency = 'RUB' AND created_at >= datetime('now', '-7 days')", (), one=True)
        stats['week_count'] = (row[0] if isinstance(row, tuple) else row['cnt']) or 0
        stats['week_sum']   = (row[1] if isinstance(row, tuple) else row['total']) or 0
        stats['week_by_currency'] = []
        # За сегодня
        row = await async_query_db("SELECT COUNT(*) AS cnt, SUM(amount) AS total FROM payments WHERE status = 'succeeded' AND currency = 'RUB' AND datetime(created_at) >= datetime('now', '+3 hours', 'start of day', '-3 hours')", (), one=True)
        stats['today_count'] = (row[0] if isinstance(row, tuple) else row['cnt']) or 0
        stats['today_sum']   = (row[1] if isinstance(row, tuple) else row['total']) or 0
        stats['today_by_currency'] = []
        return await render_template('payments.html', payments=payments_with_metadata, users=users, tariffs=tariffs, traffic_topup_tariffs=traffic_topup_tariffs, page=page, total_pages=total_pages, status_filter=status_filter, search_query=search_query, stats=stats)

    async def clear_pending_payments_view():
        if session.get('admin_role') != 'admin':
            return '', 403
        from web_admin.async_db import async_query_db, async_execute_db
        try:
            row = await async_query_db("SELECT COUNT(*) AS cnt FROM payments WHERE status = 'canceled'", (), one=True)
            count = row['cnt'] if row else 0
            await async_execute_db("DELETE FROM payments WHERE status = 'canceled'", ())
            await flash(f'Удалено записей в статусе canceled: {count}', 'success')
        except Exception as e:
            await flash(f'Ошибка очистки canceled: {e}', 'danger')
        status = request.args.get('status') or 'all'
        page = int(request.args.get('page', 1))
        return redirect(url_for('admin.payments', status=status, page=page))

    async def payments_data_api():
        """API endpoint для получения данных платежей в JSON формате (для автообновления)"""
        from web_admin.async_db import async_query_db
        try:
            page = request.args.get('page', 1, type=int)
            per_page = 20
            offset = (page - 1) * per_page
            status_filter = request.args.get('status', 'all')
            
            # Получаем платежи (та же логика, что и в payments_view)
            if status_filter == 'succeeded':
                payments = await async_query_db("SELECT * FROM payments WHERE status = 'succeeded' ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset))
                total_row = await async_query_db("SELECT COUNT(*) AS cnt FROM payments WHERE status = 'succeeded'", (), one=True)
                total = (total_row['cnt'] if total_row else 0)
            elif status_filter == 'processing':
                payments = await async_query_db("SELECT * FROM payments WHERE status = 'processing' ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset))
                total_row = await async_query_db("SELECT COUNT(*) AS cnt FROM payments WHERE status = 'processing'", (), one=True)
                total = (total_row['cnt'] if total_row else 0)
            elif status_filter == 'pending':
                payments = await async_query_db("SELECT * FROM payments WHERE status = 'pending' ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset))
                total_row = await async_query_db("SELECT COUNT(*) AS cnt FROM payments WHERE status = 'pending'", (), one=True)
                total = (total_row['cnt'] if total_row else 0)
            elif status_filter == 'failed':
                payments = await async_query_db("SELECT * FROM payments WHERE status NOT IN ('succeeded', 'processing', 'pending') ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset))
                total_row = await async_query_db("SELECT COUNT(*) AS cnt FROM payments WHERE status NOT IN ('succeeded', 'processing', 'pending')", (), one=True)
                total = (total_row['cnt'] if total_row else 0)
            else:
                payments = await async_query_db("SELECT * FROM payments ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset))
                total_row = await async_query_db("SELECT COUNT(*) AS cnt FROM payments", (), one=True)
                total = (total_row['cnt'] if total_row else 0)
            total_pages = (total + per_page - 1) // per_page

            # Получаем username и registration_type для каждого платежа
            user_ids = [dict(p)['telegram_id'] for p in payments]
            users = {}
            user_sources = {}
            if user_ids:
                q = f"SELECT telegram_id, username, real_username, registration_type FROM users WHERE telegram_id IN ({','.join(['?']*len(user_ids))})"
                for u in (await async_query_db(q, user_ids)):
                    u_dict = dict(u)
                    display_name = u_dict.get('username')
                    if not display_name:
                        real_username = u_dict.get('real_username')
                        if real_username:
                            display_name = real_username.lstrip('@')
                        else:
                            display_name = str(u_dict['telegram_id'])
                    users[u_dict['telegram_id']] = display_name
                    reg_type = (u_dict.get('registration_type') or '').lower()
                    if reg_type == 'site' or int(u_dict['telegram_id']) >= 10000000000:
                        user_sources[u_dict['telegram_id']] = 'site'
                    else:
                        user_sources[u_dict['telegram_id']] = 'telegram'

            # Извлекаем информацию о тарифах и методах платежа из metadata_json
            tariff_ids = set()
            traffic_topup_tariff_ids = set()
            payments_with_metadata = []
            for p in payments:
                p_dict = dict(p)
                metadata_json = p_dict.get('metadata_json')
                tariff_info = None
                payment_method = None
                payment_type = None
                
                payment_id = p_dict.get('payment_id', '')
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
                        
                        if not payment_method:
                            payment_method = metadata.get('payment_method') or metadata.get('cms_name')
                            if payment_method:
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
                        
                        if payment_type == 'traffic_renewal':
                            tariff_id = metadata.get('tariff_id')
                            traffic_to_add_gb = metadata.get('traffic_to_add_gb')
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
                        elif payment_type == 'device_limit_upgrade':
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
                        else:
                            tariff_id = metadata.get('tariff_id')
                            subscription_days = metadata.get('subscription_days') or metadata.get('days')
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
                
                if not payment_method:
                    payment_method = 'Неизвестно'
                
                payment_method_normalized = payment_method.lower().replace(' ', '-').replace('_', '-')
                if payment_method_normalized == 'tg-star':
                    payment_method_normalized = 'tg-star'
                elif payment_method_normalized == 'tgstar':
                    payment_method_normalized = 'tg-star'
                
                p_dict['tariff_info'] = tariff_info
                p_dict['payment_method'] = payment_method
                p_dict['payment_method_normalized'] = payment_method_normalized
                p_dict['payment_type'] = payment_type
                p_dict['registration_source'] = user_sources.get(p_dict.get('telegram_id'))
                
                if payment_id:
                    clean_id = payment_id
                    prefixes = ['WATA_', 'PLATEGA_', 'YOOMONEY_', 'YK_', 'CRYPTOBOT_', 'CRYPTO_', 'TGSTAR_', 'USDT_']
                    for prefix in prefixes:
                        if clean_id.startswith(prefix):
                            clean_id = clean_id[len(prefix):]
                            break
                    p_dict['payment_id_clean'] = clean_id
                else:
                    p_dict['payment_id_clean'] = payment_id
                
                payments_with_metadata.append(p_dict)

            # Получаем названия тарифов подписки (+ limit_ip / traffic_gb)
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

            # Статистика — только успешные RUB-платежи (для админа)
            stats = {}
            if session.get('admin_role') == 'admin':
                row = await async_query_db("SELECT COUNT(*) AS cnt, SUM(amount) AS total FROM payments WHERE status = 'succeeded' AND currency = 'RUB'", (), one=True)
                stats['all_count'] = (row[0] if isinstance(row, tuple) else row['cnt']) or 0
                stats['all_sum']   = (row[1] if isinstance(row, tuple) else row['total']) or 0
                stats['by_currency'] = []
                row = await async_query_db("SELECT COUNT(*) AS cnt, SUM(amount) AS total FROM payments WHERE status = 'succeeded' AND currency = 'RUB' AND datetime(created_at) >= datetime('now', '+3 hours', 'start of month', '-3 hours')", (), one=True)
                stats['month_count'] = (row[0] if isinstance(row, tuple) else row['cnt']) or 0
                stats['month_sum']   = (row[1] if isinstance(row, tuple) else row['total']) or 0
                stats['month_by_currency'] = []
                row = await async_query_db("SELECT COUNT(*) AS cnt, SUM(amount) AS total FROM payments WHERE status = 'succeeded' AND currency = 'RUB' AND created_at >= datetime('now', '-7 days')", (), one=True)
                stats['week_count'] = (row[0] if isinstance(row, tuple) else row['cnt']) or 0
                stats['week_sum']   = (row[1] if isinstance(row, tuple) else row['total']) or 0
                stats['week_by_currency'] = []
                row = await async_query_db("SELECT COUNT(*) AS cnt, SUM(amount) AS total FROM payments WHERE status = 'succeeded' AND currency = 'RUB' AND datetime(created_at) >= datetime('now', '+3 hours', 'start of day', '-3 hours')", (), one=True)
                stats['today_count'] = (row[0] if isinstance(row, tuple) else row['cnt']) or 0
                stats['today_sum']   = (row[1] if isinstance(row, tuple) else row['total']) or 0
                stats['today_by_currency'] = []
            
            return jsonify({
                'success': True,
                'payments': payments_with_metadata,
                'users': users,
                'tariffs': tariffs,
                'traffic_topup_tariffs': traffic_topup_tariffs,
                'stats': stats,
                'page': page,
                'total_pages': total_pages,
                'status_filter': status_filter
            })
        except Exception as e:
            import traceback
            return jsonify({
                'success': False,
                'error': str(e),
                'traceback': traceback.format_exc()
            }), 500

    # Регистрация маршрутов
    admin_bp.add_url_rule('/payments', view_func=payments_view, endpoint='payments')
    admin_bp.add_url_rule('/payments/clear_pending', view_func=clear_pending_payments_view, methods=['POST'], endpoint='clear_pending_payments')
    admin_bp.add_url_rule('/payments/data', view_func=payments_data_api, endpoint='payments_data')


