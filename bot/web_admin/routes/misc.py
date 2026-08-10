import asyncio
import os
import tempfile
import zipfile
from datetime import datetime, timezone

import aiofiles
import psutil
from quart import (
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

from web_admin.async_db import async_query_db


def attach_misc_routes(admin_bp_instance, query_db_func, execute_db_func):
    # Раздел «Логи бота / веб-админки» удалён — теперь логи смотрим через journalctl
    # на странице /tools/services (см. /api/services/<unit>/log).

    # Ленивое создание индекса для /referrals — выполняется один раз за процесс.
    # Без индекса на users(invited_by) сортировка по реферальному счётчику делает
    # N коррелированных подзапросов, что больно на 20K+ пользователях.
    _referrals_idx_ensured = {'done': False}

    async def _ensure_referrals_indexes():
        if _referrals_idx_ensured['done']:
            return
        try:
            from web_admin.async_db import async_execute_db
            await async_execute_db(
                "CREATE INDEX IF NOT EXISTS idx_users_invited_by ON users(invited_by)"
            )
        except Exception as _e:
            current_app.logger.warning(f"[referrals] не удалось создать индекс: {_e}")
        finally:
            _referrals_idx_ensured['done'] = True

    @admin_bp_instance.route('/referrals')
    async def referral_stats():
        """Реферальная статистика — SQL-only, поиск/фильтр/пагинация на стороне БД.

        Параметры запроса:
          - page: int  — номер страницы (>= 1)
          - per_page: 15|30|50|100 — размер страницы (по умолчанию 30)
          - view: inviters|invited|none|all — фильтр (по умолчанию inviters)
          - q: str — поиск по telegram_id / username / real_username (и invited_by, если q — число)
        """
        await _ensure_referrals_indexes()
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

        view = (request.args.get('view') or 'inviters').strip().lower()
        if view not in ('inviters', 'invited', 'none', 'all'):
            view = 'inviters'

        q = (request.args.get('q') or '').strip()

        # ВАЖНО: на странице /referrals считаем ТОЛЬКО органические рефералы.
        # Партнёрские приглашения (invited_by_method='partner') живут в /partners
        # и в коде везде разделены (см. main.py, начисление бонусов идёт только
        # при method != 'partner'). Поэтому тут везде фильтруем «не partner».
        # Условие: invited_by_method IS NULL (легаси) OR != 'partner' → 'referral' / NULL.
        not_partner_u2 = "COALESCE(u2.invited_by_method, '') <> 'partner'"
        not_partner_u  = "COALESCE(u.invited_by_method, '')  <> 'partner'"

        # Подзапрос «сколько органически приглашённых у u»
        sub_count = (
            "(SELECT COUNT(*) FROM users u2 "
            "  WHERE u2.invited_by = u.telegram_id "
            "    AND COALESCE(u2.is_blocked, 0) = 0 "
            f"   AND {not_partner_u2})"
        )
        # Активные приглашённые (подписка ещё не истекла) — для разбивки
        # «активных | всего | истёкших» в колонке «Рефералов». datetime() парсит
        # ISO-строку (naive трактуется как UTC, как и в per-row sub_status ниже).
        active_sub_count = (
            "(SELECT COUNT(*) FROM users u2 "
            "  WHERE u2.invited_by = u.telegram_id "
            "    AND COALESCE(u2.is_blocked, 0) = 0 "
            f"   AND {not_partner_u2} "
            "    AND u2.subscription_end_date IS NOT NULL "
            "    AND datetime(u2.subscription_end_date) > datetime('now'))"
        )
        # «Заработано» — сумма успешных RUB-платежей органически приглашённых
        # (учитываем только клиентов, приглашённых как реферал; партнёрские и
        # заблокированные исключены).
        earned_sql = (
            "(SELECT COALESCE(SUM(p.amount), 0) FROM payments p "
            "  JOIN users u2 ON u2.telegram_id = p.telegram_id "
            "  WHERE u2.invited_by = u.telegram_id "
            "    AND COALESCE(u2.is_blocked, 0) = 0 "
            f"   AND {not_partner_u2} "
            "    AND p.status = 'succeeded' AND p.currency = 'RUB')"
        )

        # 4 KPI одним запросом (без партнёров)
        kpi_row = await async_query_db(
            f"""
            SELECT
                (SELECT COUNT(*) FROM users WHERE COALESCE(is_blocked, 0) = 0) AS total_users,
                (SELECT COUNT(*) FROM users
                  WHERE COALESCE(is_blocked, 0) = 0
                    AND invited_by IS NOT NULL
                    AND COALESCE(invited_by_method, '') <> 'partner') AS invited_count,
                (SELECT COUNT(DISTINCT u.telegram_id) FROM users u
                  WHERE COALESCE(u.is_blocked, 0) = 0 AND {sub_count} > 0) AS inviters_count,
                (SELECT MAX(c) FROM (
                    SELECT {sub_count} AS c FROM users u
                    WHERE COALESCE(u.is_blocked, 0) = 0
                )) AS max_referrals
            """,
            (), one=True,
        ) or {}

        # Top-10 — отдельным эффективным запросом
        top_rows = await async_query_db(
            f"""
            SELECT u.telegram_id,
                   u.username,
                   u.real_username,
                   {sub_count} AS referrals_count
              FROM users u
             WHERE COALESCE(u.is_blocked, 0) = 0
             ORDER BY referrals_count DESC, u.telegram_id DESC
             LIMIT 10
            """
        )
        top_referrals = [r for r in (top_rows or []) if (r.get('referrals_count') or 0) > 0]

        # WHERE для основного списка.
        # not_partner_u отсекает тех, КОГО пригласили через партнёрку (их место в
        # /partners). Применять его глобально нельзя: реферер-лидер, которого самого
        # привели партнёрской ссылкой, тогда пропадал бы из таблицы, хотя в подиуме и
        # KPI он присутствует (там считаются его ОРГАНИЧЕСКИЕ приглашённые, а метод
        # самого реферера не важен). Поэтому фильтр по u применяем по-видово.
        where_clauses = ["COALESCE(u.is_blocked, 0) = 0"]
        params: list = []

        if view == 'inviters':
            # Рефереры: у кого есть органически приглашённые — как в топ-10 и KPI,
            # без учёта того, как пригласили самого реферера.
            where_clauses.append(f"{sub_count} > 0")
        elif view == 'invited':
            # «приглашённые по реферальной ссылке» — invited_by есть и метод не partner
            where_clauses.append(
                "u.invited_by IS NOT NULL AND COALESCE(u.invited_by_method, '') <> 'partner'"
            )
        elif view == 'none':
            where_clauses.append(f"u.invited_by IS NULL AND {sub_count} = 0")
        elif view == 'all':
            # 'all' исключает приглашённых через партнёрку — иначе смешивается с /partners
            where_clauses.append(f"{not_partner_u}")

        if q:
            like = f"%{q}%"
            if q.isdigit() and len(q) <= 18:
                where_clauses.append(
                    "(CAST(u.telegram_id AS TEXT) LIKE ? "
                    " OR u.username LIKE ? "
                    " OR u.real_username LIKE ? "
                    " OR u.email LIKE ? "
                    " OR u.invited_by = ?)"
                )
                params.extend([like, like, like, like, int(q)])
            else:
                where_clauses.append(
                    "(u.username LIKE ? "
                    " OR u.real_username LIKE ? "
                    " OR u.email LIKE ?)"
                )
                params.extend([like, like, like])

        where_sql = " AND ".join(where_clauses)

        cnt_row = await async_query_db(
            f"SELECT COUNT(*) AS c FROM users u WHERE {where_sql}",
            tuple(params), one=True,
        ) or {}
        total_users = int(cnt_row.get('c') or 0)
        total_pages = max(1, (total_users + per_page - 1) // per_page)
        page = min(page, total_pages)
        offset = (page - 1) * per_page

        rows = await async_query_db(
            f"""
            SELECT u.telegram_id,
                   u.username,
                   u.real_username,
                   u.invited_by,
                   u.invited_by_method,
                   u.created_at,
                   u.subscription_end_date,
                   {sub_count} AS referrals_count,
                   {active_sub_count} AS active_referrals_count,
                   {earned_sql} AS referrals_earned,
                   (SELECT u3.username      FROM users u3 WHERE u3.telegram_id = u.invited_by) AS inviter_username,
                   (SELECT u3.real_username FROM users u3 WHERE u3.telegram_id = u.invited_by) AS inviter_real_username
              FROM users u
             WHERE {where_sql}
             ORDER BY referrals_count DESC, u.telegram_id DESC
             LIMIT ? OFFSET ?
            """,
            tuple(params) + (per_page, offset),
        ) or []

        # Помечаем статус подписки на сервере, чтобы шаблон не парсил даты
        now_utc = datetime.now(timezone.utc)
        for r in rows:
            sub = r.get('subscription_end_date')
            r['sub_status'] = 'none'
            if sub:
                try:
                    end = datetime.fromisoformat(str(sub).replace('Z', '+00:00'))
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=timezone.utc)
                    r['sub_status'] = 'active' if end > now_utc else 'expired'
                except (ValueError, TypeError):
                    r['sub_status'] = 'none'
            # Разбивка приглашённых: активных | всего | истёкших (истёкшие =
            # всего − активные, сюда же попадают приглашённые без подписки).
            total_ref = int(r.get('referrals_count') or 0)
            active_ref = int(r.get('active_referrals_count') or 0)
            r['total_referrals'] = total_ref
            r['active_referrals'] = active_ref
            r['expired_referrals'] = max(0, total_ref - active_ref)
            r['earned'] = float(r.get('referrals_earned') or 0)

        return await render_template(
            'referral_stats.html',
            stats=rows,
            page=page,
            per_page=per_page,
            total_pages=total_pages,
            total_users=total_users,
            top_referrals=top_referrals,
            kpi=kpi_row,
            view=view,
            q=q,
        )

    @admin_bp_instance.route('/referrals/export.csv')
    async def referral_export_csv():
        """Полный CSV-дамп реферальной таблицы (все non-blocked пользователи). Только для админа."""
        import csv
        import io
        from quart import Response
        from web_admin.run import current_user
        if not current_user.is_admin:
            return '', 403

        # CSV — только органические рефералы (без партнёров).
        rows = await async_query_db(
            """
            SELECT u.telegram_id,
                   u.username,
                   u.real_username,
                   u.invited_by,
                   u.invited_by_method,
                   u.created_at,
                   u.subscription_end_date,
                   (SELECT COUNT(*) FROM users u2
                     WHERE u2.invited_by = u.telegram_id
                       AND COALESCE(u2.is_blocked, 0) = 0
                       AND COALESCE(u2.invited_by_method, '') <> 'partner') AS referrals_count,
                   (SELECT u3.username FROM users u3 WHERE u3.telegram_id = u.invited_by) AS inviter_username
              FROM users u
             WHERE COALESCE(u.is_blocked, 0) = 0
               AND COALESCE(u.invited_by_method, '') <> 'partner'
             ORDER BY referrals_count DESC, u.telegram_id DESC
            """
        ) or []

        buf = io.StringIO()
        buf.write('\ufeff')  # BOM, чтобы Excel понял UTF-8
        w = csv.writer(buf, delimiter=';', lineterminator='\n')
        w.writerow([
            'telegram_id', 'username', 'real_username',
            'invited_by', 'inviter_username', 'invited_by_method',
            'created_at', 'subscription_end_date', 'referrals_count',
        ])
        for r in rows:
            w.writerow([
                r.get('telegram_id') or '',
                r.get('username') or '',
                r.get('real_username') or '',
                r.get('invited_by') or '',
                r.get('inviter_username') or '',
                r.get('invited_by_method') or '',
                r.get('created_at') or '',
                r.get('subscription_end_date') or '',
                r.get('referrals_count') or 0,
            ])
        payload = buf.getvalue().encode('utf-8')
        ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')
        return Response(
            payload,
            mimetype='text/csv; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="referrals_{ts}.csv"'},
        )

    @admin_bp_instance.route('/api/referrals/<int:tg_id>/invited')
    async def api_referral_invited_list(tg_id):
        """JSON-список органически приглашённых данным пользователем — для inline-раскрытия.

        Партнёрские приглашения исключены: их видно отдельно в /partners.
        """
        rows = await async_query_db(
            """
            SELECT telegram_id, username, real_username, invited_by_method,
                   created_at, subscription_end_date
              FROM users
             WHERE invited_by = ?
               AND COALESCE(is_blocked, 0) = 0
               AND COALESCE(invited_by_method, '') <> 'partner'
             ORDER BY created_at DESC, telegram_id DESC
             LIMIT 200
            """,
            (tg_id,),
        ) or []
        now_utc = datetime.now(timezone.utc)
        for r in rows:
            sub = r.get('subscription_end_date')
            r['sub_status'] = 'none'
            if sub:
                try:
                    end = datetime.fromisoformat(str(sub).replace('Z', '+00:00'))
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=timezone.utc)
                    r['sub_status'] = 'active' if end > now_utc else 'expired'
                except (ValueError, TypeError):
                    r['sub_status'] = 'none'
        return jsonify({'success': True, 'count': len(rows), 'invited': rows})

    @admin_bp_instance.route('/updates', methods=['GET', 'POST'])
    async def updates():
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

        if request.method == 'POST':
            is_ajax = (request.headers.get('X-Requested-With') == 'XMLHttpRequest')
            files = await request.files
            file = files.get('update_zip')

            def _resp(ok: bool, msg: str, **extra):
                if is_ajax:
                    payload = {'success': ok, 'message': msg}
                    payload.update(extra)
                    return jsonify(payload), (200 if ok else 400)
                cat = 'success' if ok else 'danger'
                # flash вызовет corotine — синхронно вернуть нельзя; подкормим из обёртки.
                return ('flash', cat, msg)

            if file is None or file.filename == '':
                ret = _resp(False, 'Файл не выбран.')
                if is_ajax:
                    return ret
                _, cat, msg = ret
                await flash(msg, cat); return redirect(url_for('admin.updates'))
            if not file.filename.lower().endswith('.zip'):
                ret = _resp(False, 'Поддерживаются только .zip архивы.')
                if is_ajax:
                    return ret
                _, cat, msg = ret
                await flash(msg, cat); return redirect(url_for('admin.updates'))

            filename = secure_filename(file.filename)
            tmp_dir = tempfile.gettempdir()
            update_path = os.path.join(tmp_dir, filename)

            file_count = 0
            archive_size = 0
            try:
                file_content = file.read()
                archive_size = len(file_content)
                async with aiofiles.open(update_path, 'wb') as f:
                    await f.write(file_content)
                if not os.path.exists(update_path):
                    raise RuntimeError('Файл не был сохранён во временную папку.')

                # Валидация zip
                with zipfile.ZipFile(update_path, 'r') as zip_ref:
                    bad = zip_ref.testzip()
                    if bad is not None:
                        raise RuntimeError(f'Архив повреждён, не читается файл: {bad}')
                    # Защита от path-traversal: ни одна запись не должна выходить за project_root
                    safe_root = os.path.realpath(project_root)
                    for name in zip_ref.namelist():
                        target = os.path.realpath(os.path.join(project_root, name))
                        if not (target == safe_root or target.startswith(safe_root + os.sep)):
                            raise RuntimeError(f'Заблокировано: запись «{name}» выходит за корень проекта.')
                    file_count = len(zip_ref.namelist())
                    zip_ref.extractall(project_root)

                ok_msg = f'Обновление применено: распаковано {file_count} файлов ({archive_size // 1024} KB).'
                if is_ajax:
                    return jsonify({'success': True, 'message': ok_msg,
                                    'file_count': file_count, 'archive_size': archive_size}), 200
                await flash(ok_msg, 'success')
            except Exception as e:
                err = f'Ошибка при применении обновления: {e}'
                if is_ajax:
                    return jsonify({'success': False, 'message': err}), 400
                await flash(err, 'danger')
            finally:
                if os.path.exists(update_path):
                    try:
                        os.remove(update_path)
                    except Exception:
                        pass
            return redirect(url_for('admin.updates'))

        # GET — собираем мета-инфу
        version_mtime = None
        try:
            vp = os.path.join(project_root, 'version.txt')
            if os.path.exists(vp):
                version_mtime = datetime.fromtimestamp(os.path.getmtime(vp)).strftime('%Y-%m-%d %H:%M')
        except Exception:
            version_mtime = None

        whats_new = None
        whats_new_mtime = None
        try:
            whats_new_path = os.path.join(project_root, 'whats_new.txt')
            if os.path.exists(whats_new_path):
                async with aiofiles.open(whats_new_path, 'r', encoding='utf-8') as f:
                    whats_new = (await f.read()).strip()
                whats_new_mtime = datetime.fromtimestamp(os.path.getmtime(whats_new_path)).strftime('%Y-%m-%d %H:%M')
        except Exception:
            whats_new = None

        return await render_template(
            'updates.html',
            whats_new=whats_new,
            whats_new_mtime=whats_new_mtime,
            version_mtime=version_mtime,
        )

    @admin_bp_instance.route('/instructions')
    async def instructions():
        return redirect('https://docs.3xstore.ru/useful.html#secret-path', code=302)

    @admin_bp_instance.route('/admin-static/<path:filename>')
    async def admin_static(filename):
        try:
            static_dir = current_app.static_folder
            return await send_from_directory(static_dir, filename)
        except FileNotFoundError:
            return abort(404)

    @admin_bp_instance.route('/download/3xuistorelocal.zip')
    async def download_local_zip():
        try:
            file_path = os.path.join(current_app.static_folder, '3xuistorelocal.zip')
            if not os.path.isfile(file_path):
                current_app.logger.error(f"ZIP not found at: {file_path}")
                try:
                    current_app.logger.error(f"Static dir contents: {os.listdir(current_app.static_folder)}")
                except Exception:
                    pass
                return abort(404)
            return await send_file(file_path, as_attachment=True, download_name='3xuistorelocal.zip', mimetype='application/zip')
        except Exception as e:
            current_app.logger.error(f"Ошибка выдачи файла 3xuistorelocal.zip: {e}")
            return abort(404)

    @admin_bp_instance.route('/restart_all', methods=['POST'])
    async def restart_all():
        try:
            process = await asyncio.create_subprocess_exec(
                'sudo', 'systemctl', 'restart', 'vpn-bot.service'
            )
            # Не ждём завершения для restart
            await flash('Сервис vpn-bot перезапускается...', 'success')
        except Exception as e:
            await flash(f'Ошибка перезапуска: {e}', 'danger')
        return redirect(url_for('admin.remnawave_dashboard'))

    @admin_bp_instance.route('/restart_services', methods=['POST'])
    async def restart_services():
        try:
            # Запускаем все команды параллельно
            await asyncio.gather(
                asyncio.create_subprocess_exec('sudo', 'systemctl', 'restart', 'vpn-bot.service'),
                asyncio.create_subprocess_exec('sudo', 'systemctl', 'restart', 'vpn-webadmin.service'),
                asyncio.create_subprocess_exec('sudo', 'systemctl', 'restart', 'xuiweb.service'),
                return_exceptions=True
            )
            await flash('Все сервисы перезапускаются: vpn-bот, vpn-webadmin, xuiweb...', 'success')
        except Exception as e:
            await flash(f'Ошибка перезапуска сервисов: {e}', 'danger')
        return redirect(url_for('admin.updates'))

    # --- Сервисы (Инструменты) ---
    # icon — Lucide icon name (без data-lucide=); accent — hex/rgb для подложки иконки.
    SERVICES_WHITELIST = {
        'xuiweb.service':       {'label': 'XUI Web',       'icon': 'link-2',         'accent': '#6366f1', 'desc': 'Веб-сервер подписок'},
        'vpn-bot.service':      {'label': 'VPN Bot',       'icon': 'bot',            'accent': '#10b981', 'desc': 'Telegram-бот'},
        'vpn-webadmin.service': {'label': 'VPN WebAdmin',  'icon': 'sliders',        'accent': '#f59e0b', 'desc': 'Веб-админка'},
        'website.service':      {'label': 'Website',       'icon': 'globe',          'accent': '#06b6d4', 'desc': 'Личный кабинет клиентов'},
    }

    async def _systemctl_cmd(*args, timeout=10):
        """Выполнить systemctl. Возвращает (rc, stdout, stderr). На Windows возвращает (-1, '', 'systemctl недоступен')."""
        try:
            proc = await asyncio.create_subprocess_exec(
                'systemctl', *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                return proc.returncode or 0, (out or b'').decode('utf-8', errors='replace').strip(), (err or b'').decode('utf-8', errors='replace').strip()
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return -1, '', 'Timeout'
        except FileNotFoundError:
            return -1, '', 'systemctl недоступен'
        except Exception as e:
            return -1, '', str(e)

    async def _systemctl_sudo(*args, timeout=10):
        """systemctl с sudo (для start/stop/restart)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                'sudo', 'systemctl', *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                return proc.returncode or 0, (out or b'').decode('utf-8', errors='replace').strip(), (err or b'').decode('utf-8', errors='replace').strip()
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return -1, '', 'Timeout'
        except FileNotFoundError:
            return -1, '', 'systemctl недоступен'
        except Exception as e:
            return -1, '', str(e)

    @admin_bp_instance.route('/tools/services')
    async def services_page():
        return await render_template('services.html', services=SERVICES_WHITELIST)

    def _get_process_stats(pid: int):
        """Возвращает cpu_percent и memory_mb для процесса и его потомков. При ошибке — None."""
        try:
            p = psutil.Process(pid)
            # CPU: interval=0.2 даёт более точный замер (короткий интервал часто даёт 0%)
            cpu = p.cpu_percent(interval=0.2)
            for c in p.children(recursive=True):
                try:
                    cpu += c.cpu_percent(interval=0)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            # Память: RSS в МБ (процесс + дети)
            rss = p.memory_info().rss
            for c in p.children(recursive=True):
                try:
                    rss += c.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            mem_mb = round(rss / (1024 * 1024), 1)
            return {'cpu_percent': round(min(100.0, max(0.0, cpu)), 1), 'memory_mb': mem_mb}
        except Exception:
            return None

    async def _collect_unit_status(unit: str) -> dict:
        """Собирает по одному unit: статус, описание, uptime, pid, потребление.
        Делает только один вызов `systemctl show ...` на основные поля + один `is-active`.
        """
        rc_show, out_show, _ = await _systemctl_cmd(
            'show', unit,
            '--property=LoadState,ActiveState,SubState,Description,MainPID,'
            'ActiveEnterTimestampMonotonic,UnitFileState,FragmentPath',
        )
        props: dict[str, str] = {}
        if rc_show == 0 and out_show:
            for line in out_show.splitlines():
                if '=' in line:
                    k, _, v = line.partition('=')
                    props[k.strip()] = v.strip()

        load_state = (props.get('LoadState') or '').lower()
        active_state = (props.get('ActiveState') or '').lower()
        sub_state = (props.get('SubState') or '').lower()
        installed = load_state in ('loaded', 'stub')
        # is-active даёт «канонический» статус для совместимости с прежним фронтом
        rc_a, out_a, err_a = await _systemctl_cmd('is-active', unit)
        status = (out_a or err_a or active_state or 'unknown').strip().lower()
        if not installed:
            status = 'not-found'
        active = status == 'active'

        entry: dict = {
            'active':        active,
            'status':        status or 'unknown',
            'installed':     installed,
            'sub_state':     sub_state or None,
            'description':   props.get('Description') or '',
            'unit_file':     (props.get('UnitFileState') or '').lower() or None,
            'fragment_path': props.get('FragmentPath') or None,
        }

        # Аптайм (на основе ActiveEnterTimestampMonotonic — длительность с момента активации)
        try:
            mono_us = int(props.get('ActiveEnterTimestampMonotonic') or '0')
            if active and mono_us > 0:
                # /proc/uptime даёт текущий monotonic в секундах
                with open('/proc/uptime', 'r', encoding='utf-8') as fh:
                    up_now = float(fh.read().split()[0])
                up_secs = max(0, int(up_now - mono_us / 1_000_000))
                entry['uptime_seconds'] = up_secs
        except Exception:
            pass

        # Ресурсы
        if active:
            try:
                pid = int((props.get('MainPID') or '0').strip())
                entry['pid'] = pid if pid > 0 else None
                if pid > 0:
                    stats = _get_process_stats(pid)
                    if stats:
                        entry['cpu_percent'] = stats['cpu_percent']
                        entry['memory_mb'] = stats['memory_mb']
            except (ValueError, TypeError):
                pass

        return entry

    @admin_bp_instance.route('/api/services/status')
    async def api_services_status():
        # Параллельно собираем все юниты — на десктопе ~5×8ms вместо ~5×40ms.
        units = list(SERVICES_WHITELIST.keys())
        results = await asyncio.gather(*[_collect_unit_status(u) for u in units], return_exceptions=True)
        out: dict = {}
        for unit, res in zip(units, results):
            if isinstance(res, Exception):
                out[unit] = {'active': False, 'status': 'error', 'installed': False, 'error': str(res)}
            else:
                out[unit] = res
        return jsonify(out)

    @admin_bp_instance.route('/api/services/<path:unit>/log')
    async def api_services_log(unit):
        """Возвращает последние N строк journalctl для сервиса (только из whitelist)."""
        unit = (unit or '').strip().strip('/')
        if unit not in SERVICES_WHITELIST:
            return jsonify({'success': False, 'error': 'Сервис не в списке разрешённых'}), 400
        try:
            lines = int(request.args.get('lines', 200))
            lines = max(20, min(2000, lines))
        except (TypeError, ValueError):
            lines = 200
        try:
            proc = await asyncio.create_subprocess_exec(
                'journalctl', '-u', unit, '-n', str(lines), '--no-pager', '--output=short-iso',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            rc, out, err = -1, '', ''
            try:
                so, se = await asyncio.wait_for(proc.communicate(), timeout=15)
                rc = proc.returncode or 0
                out = (so or b'').decode('utf-8', errors='replace')
                err = (se or b'').decode('utf-8', errors='replace')
            except asyncio.TimeoutError:
                try: proc.kill()
                except Exception: pass
                return jsonify({'success': False, 'error': 'journalctl: таймаут'}), 504
        except FileNotFoundError:
            return jsonify({'success': False, 'error': 'journalctl недоступен на этом хосте'}), 501
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

        if rc != 0 and not out:
            return jsonify({'success': False, 'error': err.strip() or f'journalctl rc={rc}'}), 500
        return jsonify({'success': True, 'lines': lines, 'log': out})

    @admin_bp_instance.route('/api/services/restart-all', methods=['POST'])
    async def api_services_restart_all():
        """Перезапускает все УСТАНОВЛЕННЫЕ сервисы из whitelist (vpn-webadmin — отложенно)."""
        units = list(SERVICES_WHITELIST.keys())
        # Отфильтруем неустановленные
        async def _is_installed(u):
            rc, out, _ = await _systemctl_cmd('show', u, '--property=LoadState', '--value')
            return u, (rc == 0 and (out or '').strip().lower() in ('loaded', 'stub'))
        statuses = await asyncio.gather(*[_is_installed(u) for u in units])
        installed = [u for u, ok in statuses if ok]

        # vpn-webadmin рестартим в самом конце с задержкой (нам нужно успеть отдать ответ)
        deferred = []
        immediate = []
        for u in installed:
            (deferred if u == 'vpn-webadmin.service' else immediate).append(u)

        results = []
        if immediate:
            res = await asyncio.gather(*[_systemctl_sudo('restart', u) for u in immediate], return_exceptions=True)
            for u, r in zip(immediate, res):
                if isinstance(r, Exception):
                    results.append({'unit': u, 'ok': False, 'error': str(r)})
                else:
                    rc, _o, e = r
                    results.append({'unit': u, 'ok': rc == 0, 'error': (e or '').strip() if rc != 0 else None})

        for u in deferred:
            async def _delayed(unit=u):
                await asyncio.sleep(2)
                await _systemctl_sudo('restart', unit)
            asyncio.create_task(_delayed())
            results.append({'unit': u, 'ok': True, 'deferred': True})

        return jsonify({
            'success': all(r['ok'] for r in results),
            'restarted': results,
            'skipped': [u for u, ok in statuses if not ok],
        })

    @admin_bp_instance.route('/api/services/<path:unit>/<action>', methods=['POST'])
    async def api_services_action(unit, action):
        # Нормализуем unit (без слэшей)
        unit = unit.strip().strip('/')
        if unit not in SERVICES_WHITELIST:
            return jsonify({'success': False, 'error': 'Сервис не в списке разрешённых'}), 400
        if action not in ('start', 'stop', 'restart'):
            return jsonify({'success': False, 'error': 'Неверное действие'}), 400
        # Проверяем, установлен ли сервис
        rc_load, out_load, _ = await _systemctl_cmd('show', unit, '--property=LoadState', '--value')
        load_state = (out_load or '').strip().lower() if rc_load == 0 else ''
        if load_state not in ('loaded', 'stub'):
            return jsonify({'success': False, 'error': 'Сервис не установлен (systemd unit не найден)'}), 400

        # Для vpn-webadmin restart — откладываем перезапуск, чтобы успеть отправить JSON ответ
        if unit == 'vpn-webadmin.service' and action == 'restart':
            async def delayed_restart():
                await asyncio.sleep(2)
                await _systemctl_sudo('restart', unit)

            asyncio.create_task(delayed_restart())
            return jsonify({
                'success': True,
                'status': 'activating',
                'active': False,
                'delayed': True
            })

        rc, out, err = await _systemctl_sudo(action, unit)
        if rc != 0:
            return jsonify({'success': False, 'error': err or out or 'Неизвестная ошибка'}), 500
        # Получаем новый статус
        rc2, out2, _ = await _systemctl_cmd('is-active', unit)
        new_status = (out2 or '').strip().lower()
        return jsonify({'success': True, 'status': new_status, 'active': new_status == 'active'})


