import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import time
from datetime import datetime

from quart import render_template, request, redirect, url_for, flash, current_app, jsonify, session, abort
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile

from app_config import app_conf
from src.telegram_bot_factory import make_aiogram_bot

from web_admin.async_db import async_execute_db, async_query_db, async_get_db_setting
from tg_sender import async_get_bot_token
from button_helpers import btn

logger = logging.getLogger(__name__)

# ─── Константы для медиа-аттачей рассылок ─────────────────────────────────────
# kind ∈ {'photo', 'animation', 'video'}
MEDIA_LIMITS = {
    'photo':     10 * 1024 * 1024,   # 10 МБ
    'animation': 50 * 1024 * 1024,   # 50 МБ
    'video':     50 * 1024 * 1024,   # 50 МБ
}
ALLOWED_EXT = {
    # ext.lower() → kind
    'jpg':  'photo', 'jpeg': 'photo', 'png': 'photo', 'webp': 'photo',
    'gif':  'animation',
    'mp4':  'video', 'mov': 'video', 'm4v': 'video',
}
# Caption Telegram: 1024 символа
TG_CAPTION_LIMIT = 1024


def _detect_kind(filename: str, content_type: str = '') -> str | None:
    """Определяет тип медиа по расширению и content-type. Возвращает kind или None."""
    ext = (os.path.splitext(filename or '')[1] or '').lower().lstrip('.')
    if ext in ALLOWED_EXT:
        return ALLOWED_EXT[ext]
    # Fallback по content_type
    ct = (content_type or '').lower()
    if ct == 'image/gif':
        return 'animation'
    if ct.startswith('image/'):
        return 'photo'
    if ct.startswith('video/'):
        return 'video'
    return None


def _sanitize_filename(name: str, max_len: int = 80) -> str:
    """Безопасное имя файла: только латиница, цифры, _-., с обрезанием по длине."""
    base = os.path.basename(name or '').strip()
    base = re.sub(r'[^A-Za-z0-9._-]+', '_', base)
    base = re.sub(r'_{2,}', '_', base).strip('._-') or 'file'
    if len(base) > max_len:
        # сохраняем расширение
        root, ext = os.path.splitext(base)
        ext = ext[:10]
        root = root[: max(1, max_len - len(ext))]
        base = root + ext
    return base


def _build_preview_url(rel_path: str) -> str:
    """Собирает URL превью под admin_secret_path."""
    if not rel_path:
        return ''
    secret = current_app.config.get('ADMIN_SECRET_PATH', '').strip('/')
    safe = (rel_path or '').replace('\\', '/').lstrip('/')
    if secret:
        return f"/{secret}/news_media/{safe}"
    return f"/news_media/{safe}"


async def _get_admin_ids_list() -> list[int]:
    """Возвращает список Telegram-ID администраторов (для отправки служебных аплоадов)."""
    try:
        row = await async_query_db("SELECT value FROM settings WHERE key = 'admin_ids'", (), one=True)
        raw = (row['value'] if row and row.get('value') else '').strip()
    except Exception:
        raw = ''
    ids: list[int] = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                for v in parsed:
                    try:
                        ids.append(int(v))
                    except Exception:
                        continue
        except Exception:
            for chunk in raw.split(','):
                s = chunk.strip()
                if s.isdigit() or (s.startswith('-') and s[1:].isdigit()):
                    try:
                        ids.append(int(s))
                    except Exception:
                        continue
    if not ids:
        try:
            row = await async_query_db(
                "SELECT admin_telegram_id FROM backup_settings LIMIT 1", (), one=True
            )
            v = (row.get('admin_telegram_id') or '').strip() if row else ''
            if v.isdigit():
                ids.append(int(v))
        except Exception:
            pass
    return ids


def attach_news_routes(admin_bp_instance, query_db_func, execute_db_func):

    async def _ensure_news_template_columns():
        """Добавляет недостающие колонки в news_templates (не падает на уже существующих)
        и создаёт таблицу дедупликации news_media_uploads."""
        try:
            cols = await async_query_db("PRAGMA table_info(news_templates)")
            col_names = [c['name'] for c in cols] if cols else []
            for col, ddl in [
                ('custom_btn_text',   "ALTER TABLE news_templates ADD COLUMN custom_btn_text TEXT"),
                ('custom_btn_url',    "ALTER TABLE news_templates ADD COLUMN custom_btn_url TEXT"),
                ('updated_at',        "ALTER TABLE news_templates ADD COLUMN updated_at TEXT"),
                ('media_kind',        "ALTER TABLE news_templates ADD COLUMN media_kind TEXT"),
                ('media_file_id',     "ALTER TABLE news_templates ADD COLUMN media_file_id TEXT"),
                ('media_local_path',  "ALTER TABLE news_templates ADD COLUMN media_local_path TEXT"),
                ('media_meta_json',   "ALTER TABLE news_templates ADD COLUMN media_meta_json TEXT"),
            ]:
                if col not in col_names:
                    try:
                        await async_execute_db(ddl, ())
                    except Exception:
                        pass
        except Exception:
            pass
        # Таблица дедупа загруженных медиа: hash → file_id
        try:
            await async_execute_db(
                "CREATE TABLE IF NOT EXISTS news_media_uploads ("
                "  hash TEXT PRIMARY KEY,"
                "  kind TEXT NOT NULL,"
                "  file_id TEXT NOT NULL,"
                "  local_path TEXT,"
                "  size INTEGER,"
                "  width INTEGER,"
                "  height INTEGER,"
                "  duration INTEGER,"
                "  original_name TEXT,"
                "  mime_type TEXT,"
                "  uploaded_by TEXT,"
                "  created_at TEXT"
                ")", ()
            )
        except Exception:
            pass

    @admin_bp_instance.route('/news_templates')
    async def news_templates_list():
        await _ensure_news_template_columns()
        templates = await async_query_db(
            "SELECT id, title, body, "
            "COALESCE(custom_btn_text,'') as custom_btn_text, "
            "COALESCE(custom_btn_url,'')  as custom_btn_url, "
            "COALESCE(media_kind,'') as media_kind, "
            "COALESCE(media_file_id,'') as media_file_id, "
            "COALESCE(media_local_path,'') as media_local_path, "
            "COALESCE(media_meta_json,'') as media_meta_json, "
            "created_at, COALESCE(updated_at,'') as updated_at "
            "FROM news_templates ORDER BY id DESC"
        )
        # Добавляем preview_url для UI
        out = []
        for t in (templates or []):
            d = dict(t)
            d['media_preview_url'] = _build_preview_url(d.get('media_local_path') or '') if d.get('media_kind') else ''
            out.append(d)
        return await render_template('news_templates_list.html', templates=out)

    # ─── Upload endpoint для медиа-аттачей ─────────────────────────────────
    @admin_bp_instance.route('/api/news/upload-media', methods=['POST'])
    async def news_upload_media():
        """Принимает multipart-файл, валидирует, сохраняет на диск, шлёт первому
        живому админу через бота для получения file_id, дедуплицирует по SHA-256."""
        # Только админ; модератор и неавторизованный — нет
        if not session.get('admin_user_id'):
            return jsonify({'error': 'Не авторизован.'}), 401
        if session.get('admin_role') == 'moderator':
            return jsonify({'error': 'Недостаточно прав. Загрузка медиа доступна только администратору.'}), 403

        await _ensure_news_template_columns()

        try:
            files = await request.files
        except Exception as e:
            return jsonify({'error': f'Не удалось прочитать форму: {e}'}), 400
        f = files.get('file')
        if not f:
            return jsonify({'error': 'Файл не получен (поле "file").'}), 400

        filename = f.filename or 'upload.bin'
        content_type = f.content_type or mimetypes.guess_type(filename)[0] or ''
        kind = _detect_kind(filename, content_type)
        if not kind:
            return jsonify({'error': 'Неподдерживаемый формат. Доступно: JPG/PNG/WEBP/GIF/MP4/MOV.'}), 400

        # Чтение файла (FileStorage.read() — sync; гоним в executor для крупных файлов)
        try:
            data = await asyncio.to_thread(f.read)
        except Exception as e:
            return jsonify({'error': f'Не удалось прочитать файл: {e}'}), 400
        size = len(data) if data else 0
        if size == 0:
            return jsonify({'error': 'Файл пуст.'}), 400
        limit = MEDIA_LIMITS[kind]
        if size > limit:
            return jsonify({
                'error': f'Файл слишком большой: {size/1024/1024:.1f} МБ. Лимит для {kind}: {limit/1024/1024:.0f} МБ.'
            }), 400

        # Хэш для дедупликации
        file_hash = await asyncio.to_thread(lambda: hashlib.sha256(data).hexdigest())

        # Если такой файл уже загружали — возвращаем file_id из кеша
        try:
            existing = await async_query_db(
                "SELECT * FROM news_media_uploads WHERE hash = ?", (file_hash,), one=True
            )
        except Exception:
            existing = None
        if existing:
            ex = dict(existing)
            local_rel = ex.get('local_path') or ''
            # Файл может быть удалён с диска — превью просто не будет
            return jsonify({
                'success': True,
                'cached': True,
                'kind': ex.get('kind'),
                'file_id': ex.get('file_id'),
                'preview_url': _build_preview_url(local_rel) if local_rel else '',
                'size': ex.get('size'),
                'width': ex.get('width'),
                'height': ex.get('height'),
                'duration': ex.get('duration'),
                'original_name': ex.get('original_name') or filename,
            })

        # Сохраняем на диск
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        yyyy_mm = datetime.now().strftime('%Y-%m')
        base_dir = os.path.join(project_root, 'media', 'news', 'uploads', yyyy_mm)
        try:
            os.makedirs(base_dir, exist_ok=True)
        except Exception as e:
            return jsonify({'error': f'Не удалось создать папку: {e}'}), 500

        safe_name = _sanitize_filename(filename)
        ts = int(time.time() * 1000)
        final_name = f"{ts}_{safe_name}"
        local_path = os.path.join(base_dir, final_name)
        rel_path = (yyyy_mm + '/' + final_name).replace('\\', '/')

        try:
            def _write():
                with open(local_path, 'wb') as out:
                    out.write(data)
            await asyncio.to_thread(_write)
        except Exception as e:
            return jsonify({'error': f'Не удалось записать файл: {e}'}), 500

        # Получаем список админов и токен бота
        admin_ids = await _get_admin_ids_list()
        if not admin_ids:
            try: os.remove(local_path)
            except Exception: pass
            return jsonify({
                'error': 'Не настроены администраторы (admin_ids). Невозможно зарегистрировать файл в Telegram.'
            }), 503

        bot_token = await async_get_bot_token()
        if not bot_token:
            try: os.remove(local_path)
            except Exception: pass
            return jsonify({'error': 'Bot token не найден.'}), 503

        proxy_url = (app_conf.get('telegram_proxy_url') or '').strip() or None
        bot = make_aiogram_bot(bot_token, proxy_url)
        file_id = None
        width = height = duration = None
        last_err = None
        try:
            uploader = session.get('admin_user_id') or '?'
            caption = (
                "🗂 Загрузка медиа для рассылки\n"
                f"Тип: {kind}\n"
                f"Размер: {size/1024/1024:.2f} МБ\n"
                f"Файл: {safe_name}\n"
                f"Загрузил: {uploader}"
            )
            for adm in admin_ids:
                try:
                    fs = FSInputFile(local_path)
                    if kind == 'photo':
                        msg = await bot.send_photo(adm, fs, caption=caption)
                        if msg and msg.photo:
                            ph = msg.photo[-1]
                            file_id = ph.file_id
                            width, height = ph.width, ph.height
                    elif kind == 'animation':
                        msg = await bot.send_animation(adm, fs, caption=caption)
                        if msg and msg.animation:
                            file_id = msg.animation.file_id
                            width, height = msg.animation.width, msg.animation.height
                            duration = msg.animation.duration
                    elif kind == 'video':
                        msg = await bot.send_video(adm, fs, caption=caption)
                        if msg and msg.video:
                            file_id = msg.video.file_id
                            width, height = msg.video.width, msg.video.height
                            duration = msg.video.duration
                    if file_id:
                        logger.info(f"[NEWS_MEDIA] file_id получен через админа {adm}: {file_id[:32]}…")
                        break
                except Exception as e:
                    last_err = e
                    logger.warning(f"[NEWS_MEDIA] Не удалось отправить админу {adm}: {e}")
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
                'error': ('Не удалось зарегистрировать файл в Telegram. '
                          'Возможно, бот заблокирован у всех администраторов — '
                          'разблокируйте бота в Telegram и попробуйте снова.'),
                'detail': (str(last_err)[:300] if last_err else None)
            }), 502

        # Кешируем
        try:
            await async_execute_db(
                "INSERT INTO news_media_uploads "
                "(hash, kind, file_id, local_path, size, width, height, duration, original_name, mime_type, uploaded_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (file_hash, kind, file_id, rel_path, size, width, height, duration,
                 filename, content_type, str(session.get('admin_user_id') or ''),
                 datetime.now().isoformat())
            )
        except Exception as e:
            logger.warning(f"[NEWS_MEDIA] Не удалось записать в news_media_uploads: {e}")

        return jsonify({
            'success': True,
            'cached': False,
            'kind': kind,
            'file_id': file_id,
            'preview_url': _build_preview_url(rel_path),
            'size': size,
            'width': width,
            'height': height,
            'duration': duration,
            'original_name': filename,
        })

    def _validate_news_form(title: str, body: str, custom_btn_text: str, custom_btn_url: str):
        """Возвращает None если ок, иначе текст ошибки.
        Непарные значения кнопки (только текст или только URL) не считаются ошибкой —
        они просто не сформируют кнопку при отправке (см. send_news)."""
        if not title:
            return 'Введите название шаблона.'
        if not body:
            return 'Введите текст шаблона.'
        if len(body) > 4096:
            return f'Текст слишком длинный: {len(body)} символов (Telegram лимит — 4096).'
        if custom_btn_url and not (custom_btn_url.startswith('http://') or custom_btn_url.startswith('https://')):
            return 'Ссылка для кнопки должна начинаться с http:// или https://'
        return None

    def _read_media_fields(form):
        """Собирает media из form. Возвращает (kind, file_id, local_path, meta_json) — все или Nones."""
        kind = (form.get('media_kind') or '').strip().lower() or None
        if kind not in ('photo', 'animation', 'video'):
            return None, None, None, None
        file_id = (form.get('media_file_id') or '').strip() or None
        local_path = (form.get('media_local_path') or '').strip() or None
        meta = (form.get('media_meta_json') or '').strip() or None
        if not file_id:
            return None, None, None, None
        # Лёгкая валидация JSON, чтобы не пихать мусор
        if meta:
            try:
                json.loads(meta)
            except Exception:
                meta = None
        return kind, file_id, local_path, meta

    @admin_bp_instance.route('/news_templates/add', methods=['GET', 'POST'])
    async def news_template_add():
        await _ensure_news_template_columns()
        if request.method == 'POST':
            form = await request.form
            title = (form.get('title') or '').strip()
            body = (form.get('body') or '').strip()
            custom_btn_text = (form.get('custom_btn_text') or '').strip()
            custom_btn_url = (form.get('custom_btn_url') or '').strip()
            err = _validate_news_form(title, body, custom_btn_text, custom_btn_url)
            if err:
                await flash(err, 'danger')
                return redirect(url_for('admin.news_template_add'))
            m_kind, m_fid, m_path, m_meta = _read_media_fields(form)
            now = datetime.now().isoformat()
            await async_execute_db(
                "INSERT INTO news_templates "
                "(title, body, custom_btn_text, custom_btn_url, "
                " media_kind, media_file_id, media_local_path, media_meta_json, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, body, custom_btn_text, custom_btn_url,
                 m_kind, m_fid, m_path, m_meta, now, now)
            )
            await flash('Шаблон создан.', 'success')
            return redirect(url_for('admin.news_templates_list'))
        return await render_template('news_template_form.html', template=None, action='add', build_preview_url=_build_preview_url)

    @admin_bp_instance.route('/news_templates/edit/<int:template_id>', methods=['GET', 'POST'])
    async def news_template_edit(template_id):
        await _ensure_news_template_columns()
        template = await async_query_db("SELECT * FROM news_templates WHERE id = ?", (template_id,), one=True)
        if not template:
            abort(404)
        if request.method == 'POST':
            form = await request.form
            title = (form.get('title') or '').strip()
            body = (form.get('body') or '').strip()
            custom_btn_text = (form.get('custom_btn_text') or '').strip()
            custom_btn_url = (form.get('custom_btn_url') or '').strip()
            err = _validate_news_form(title, body, custom_btn_text, custom_btn_url)
            if err:
                await flash(err, 'danger')
                return redirect(url_for('admin.news_template_edit', template_id=template_id))
            m_kind, m_fid, m_path, m_meta = _read_media_fields(form)
            await async_execute_db(
                "UPDATE news_templates SET "
                "  title = ?, body = ?, custom_btn_text = ?, custom_btn_url = ?, "
                "  media_kind = ?, media_file_id = ?, media_local_path = ?, media_meta_json = ?, "
                "  updated_at = ? "
                "WHERE id = ?",
                (title, body, custom_btn_text, custom_btn_url,
                 m_kind, m_fid, m_path, m_meta,
                 datetime.now().isoformat(), template_id)
            )
            await flash('Шаблон сохранён.', 'success')
            return redirect(url_for('admin.news_templates_list'))
        template = dict(template)
        return await render_template('news_template_form.html', template=template, action='edit', build_preview_url=_build_preview_url)

    @admin_bp_instance.route('/news_templates/delete/<int:template_id>', methods=['POST'])
    async def news_template_delete(template_id):
        template = await async_query_db("SELECT * FROM news_templates WHERE id = ?", (template_id,), one=True)
        if not template:
            abort(404)
        await async_execute_db("DELETE FROM news_templates WHERE id = ?", (template_id,))
        # AJAX → JSON; обычный submit → flash + redirect
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True})
        await flash('Шаблон удалён.', 'success')
        return redirect(url_for('admin.news_templates_list'))

    @admin_bp_instance.route('/news_templates/duplicate/<int:template_id>', methods=['POST'])
    async def news_template_duplicate(template_id):
        await _ensure_news_template_columns()
        template = await async_query_db("SELECT * FROM news_templates WHERE id = ?", (template_id,), one=True)
        if not template:
            abort(404)
        t = dict(template)
        # Делаем уникальное имя: «<original> (копия)», «<original> (копия 2)», ...
        base_title = (t.get('title') or '').strip() or 'Шаблон'
        new_title = f"{base_title} (копия)"
        existing = await async_query_db(
            "SELECT title FROM news_templates WHERE title LIKE ?",
            (f"{base_title} (копия%",)
        )
        existing_titles = {row['title'] for row in (existing or [])}
        if new_title in existing_titles:
            n = 2
            while f"{base_title} (копия {n})" in existing_titles:
                n += 1
            new_title = f"{base_title} (копия {n})"
        now = datetime.now().isoformat()
        await async_execute_db(
            "INSERT INTO news_templates "
            "(title, body, custom_btn_text, custom_btn_url, "
            " media_kind, media_file_id, media_local_path, media_meta_json, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_title, t.get('body') or '', t.get('custom_btn_text') or '',
             t.get('custom_btn_url') or '',
             t.get('media_kind') or None, t.get('media_file_id') or None,
             t.get('media_local_path') or None, t.get('media_meta_json') or None,
             now, now)
        )
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'title': new_title})
        await flash(f'Шаблон продублирован как «{new_title}».', 'success')
        return redirect(url_for('admin.news_templates_list'))

    @admin_bp_instance.route('/send_news', methods=['POST'])
    async def send_news():
        # Модератор не может отправлять рассылки
        if session.get('admin_role') == 'moderator':
            is_ajax = (request.content_type and 'application/json' in request.content_type) or \
                      (request.headers.get('X-Requested-With') == 'XMLHttpRequest')
            if is_ajax:
                return jsonify({'error': 'Недостаточно прав. Отправка рассылок доступна только администратору.'}), 403
            await flash('Недостаточно прав. Отправка рассылок доступна только администратору.', 'danger')
            return redirect(url_for('admin.news_templates_list'))

        # Определяем, это AJAX запрос или нет
        is_ajax = (request.content_type and 'application/json' in request.content_type) or \
                  (request.headers.get('X-Requested-With') == 'XMLHttpRequest')
        
        # Поддержка как JSON (для AJAX), так и form-data (для обратной совместимости)
        media_kind = None
        media_file_id = None
        try:
            if request.content_type and 'application/json' in request.content_type:
                data = await request.get_json()
                user_ids = list(set(data.get('user_ids', [])))
                news_text = (data.get('news_text', '') or '').strip()
                add_renew_btn = data.get('add_renew_btn', False)
                add_free_renew_btn = data.get('add_free_renew_btn', False)
                add_referral_btn = data.get('add_referral_btn', False)
                send_ins_video = data.get('send_ins_video', False)
                add_website_btn = data.get('add_website_btn', False)
                custom_btn_text = (data.get('custom_btn_text', '') or '').strip()
                custom_btn_url = (data.get('custom_btn_url', '') or '').strip()
                _media = data.get('media') or {}
                if isinstance(_media, dict):
                    mk = (_media.get('kind') or '').strip().lower()
                    mfid = (_media.get('file_id') or '').strip()
                    if mk in ('photo', 'animation', 'video') and mfid:
                        media_kind, media_file_id = mk, mfid
            else:
                form = await request.form
                user_ids = list(set(form.getlist('user_ids')))
                news_text = (form.get('news_text', '') or '').strip()
                # Чекбоксы приходят только если отмечены
                add_renew_btn = 'add_renew_btn' in form
                add_free_renew_btn = 'add_free_renew_btn' in form
                add_referral_btn = 'add_referral_btn' in form
                send_ins_video = 'send_ins_video' in form
                add_website_btn = 'add_website_btn' in form
                custom_btn_text = (form.get('custom_btn_text', '') or '').strip()
                custom_btn_url = (form.get('custom_btn_url', '') or '').strip()
                mk = (form.get('media_kind') or '').strip().lower()
                mfid = (form.get('media_file_id') or '').strip()
                if mk in ('photo', 'animation', 'video') and mfid:
                    media_kind, media_file_id = mk, mfid
        except Exception as parse_err:
            # Fallback на form-data
            try:
                form = await request.form
                user_ids = list(set(form.getlist('user_ids')))
                news_text = (form.get('news_text', '') or '').strip()
                add_renew_btn = 'add_renew_btn' in form
                add_free_renew_btn = 'add_free_renew_btn' in form
                add_referral_btn = 'add_referral_btn' in form
                send_ins_video = 'send_ins_video' in form
                add_website_btn = 'add_website_btn' in form
                custom_btn_text = (form.get('custom_btn_text', '') or '').strip()
                custom_btn_url = (form.get('custom_btn_url', '') or '').strip()
                mk = (form.get('media_kind') or '').strip().lower()
                mfid = (form.get('media_file_id') or '').strip()
                if mk in ('photo', 'animation', 'video') and mfid:
                    media_kind, media_file_id = mk, mfid
            except Exception as e:
                # Если и это не сработало, возвращаем ошибку
                if is_ajax:
                    return jsonify({'error': f'Ошибка при обработке запроса: {str(e)}'}), 400
                await flash(f'Ошибка при обработке запроса: {e}', 'danger')
                return redirect(url_for('admin.users_list'))
        
        logger.info(f"[NEWS] Параметры: add_renew_btn={add_renew_btn}, add_free_renew_btn={add_free_renew_btn}, add_referral_btn={add_referral_btn}, add_website_btn={add_website_btn}")
        if media_file_id:
            logger.info(f"[NEWS] Медиа-аттач: kind={media_kind}, file_id={(media_file_id[:32] + '…') if media_file_id else '-'}, "
                        f"caption_inline={'yes' if len(news_text) <= TG_CAPTION_LIMIT else 'no (text>1024, отдельным сообщением)'}")

        if not user_ids or not news_text:
            # Если это AJAX запрос, возвращаем JSON
            if is_ajax:
                return jsonify({'error': 'Выберите хотя бы одного пользователя и введите текст новости.'}), 400
            await flash('Выберите хотя бы одного пользователя и введите текст новости.', 'danger')
            return redirect(url_for('admin.users_list'))
        
        task_id = f"news_{int(time.time())}"

        try:
            try:
                await async_execute_db("ALTER TABLE news_tasks ADD COLUMN news_text TEXT", ())
            except Exception:
                pass

            await async_execute_db(
                "INSERT INTO news_tasks (task_id, user_count, status, created_at, news_text) VALUES (?, ?, ?, ?, ?)",
                (task_id, len(user_ids), 'running', datetime.now().isoformat(), news_text)
            )

            async def send_messages():
                        logger.info(f"[NEWS] Начало отправки новостей. Task ID: {task_id}, Пользователей: {len(user_ids)}")
                        
                        try:
                            # Кнопки берутся из реестра через btn() — текст,
                            # стиль и premium-эмодзи синхронизируются с разделом
                            # «Стиль кнопок» автоматически.
                            buttons = []
                            logger.info(f"[NEWS] Создание кнопок: add_renew_btn={add_renew_btn}, add_free_renew_btn={add_free_renew_btn}, add_referral_btn={add_referral_btn}")
                            if add_renew_btn:
                                buttons.append([btn('btn_renew_sub', callback_data='renew_choose_payment')])
                            if add_free_renew_btn:
                                buttons.append([btn('btn_free_renew', callback_data='admin_renew_subscription_free')])
                            if add_referral_btn:
                                buttons.append([btn('btn_referral', callback_data='referral_program')])
                            if add_website_btn:
                                buttons.append([btn('btn_website_access', callback_data='website_access')])
                            # Кастомная кнопка, если корректно заполнены поля
                            if custom_btn_text and custom_btn_url and (custom_btn_url.startswith('http://') or custom_btn_url.startswith('https://')):
                                buttons.append([InlineKeyboardButton(text=custom_btn_text, url=custom_btn_url)])
                            buttons.append([btn('btn_back_to_main', callback_data='back_to_main')])
                            reply_markup = InlineKeyboardMarkup(inline_keyboard=buttons)
                            
                            # --- ОПТИМИЗИРОВАННАЯ ОТПРАВКА ---
                            # 1) Создаем ОДИН экземпляр Bot для всей рассылки
                            logger.info(f"[NEWS] Получаем bot token...")
                            bot_token = await async_get_bot_token()
                            if not bot_token:
                                logger.error(f"[NEWS] Bot token не найден!")
                                await async_execute_db(
                                    "UPDATE news_tasks SET status = ?, error_details = ?, completed_at = ? WHERE task_id = ?",
                                    ('failed', json.dumps({'error': 'Bot token not found'}), datetime.now().isoformat(), task_id)
                                )
                                return
                            
                            logger.info(f"[NEWS] Создаем Bot экземпляр...")
                            proxy_url = (app_conf.get('telegram_proxy_url') or '').strip() or None
                            bot = make_aiogram_bot(bot_token, proxy_url)
                            sent = 0
                            failed = 0
                            errors = []
                            
                            # 2) Подготовка данных для видео (если нужно)
                            video_file_id = None
                            video_path = None
                            if send_ins_video:
                                try:
                                    row_fid = await async_query_db("SELECT value FROM settings WHERE key = 'ins_video_file_id'", (), one=True)
                                    video_file_id = row_fid['value'] if row_fid and row_fid['value'] else None
                                    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
                                    video_path = os.path.join(project_root, 'ins.mp4')
                                    if not os.path.isfile(video_path):
                                        video_path = None
                                except Exception:
                                    pass
                            
                            # 3) Параллельная отправка с ограничением и задержками
                            # Уменьшаем параллелизм и увеличиваем задержки для соблюдения rate limits Telegram
                            # Telegram ограничивает: ~30 сообщений/сек глобально, ~1 сообщение/сек на один чат
                            semaphore = asyncio.Semaphore(3)  # Максимум 3 одновременных отправки (было 10)
                            delay_between_messages = 0.1  # 100ms между сообщениями (было 50ms)
                            
                            # Определяем стратегию для медиа-аттача
                            media_inline_caption = bool(media_file_id) and len(news_text) <= TG_CAPTION_LIMIT
                            media_separate = bool(media_file_id) and not media_inline_caption

                            async def _send_media_with_caption(uid: int):
                                """Одно сообщение: media + caption + reply_markup. Для текста ≤1024 симв."""
                                if media_kind == 'photo':
                                    await bot.send_photo(int(uid), media_file_id, caption=news_text,
                                                         parse_mode="HTML", reply_markup=reply_markup)
                                elif media_kind == 'animation':
                                    await bot.send_animation(int(uid), media_file_id, caption=news_text,
                                                             parse_mode="HTML", reply_markup=reply_markup)
                                elif media_kind == 'video':
                                    await bot.send_video(int(uid), media_file_id, caption=news_text,
                                                         parse_mode="HTML", reply_markup=reply_markup)

                            async def _send_media_no_caption(uid: int):
                                """Медиа без подписи (когда текст длиннее 1024 — пойдёт отдельным сообщением)."""
                                if media_kind == 'photo':
                                    await bot.send_photo(int(uid), media_file_id)
                                elif media_kind == 'animation':
                                    await bot.send_animation(int(uid), media_file_id)
                                elif media_kind == 'video':
                                    await bot.send_video(int(uid), media_file_id)

                            async def send_to_user(uid: int):
                                nonlocal sent, failed
                                async with semaphore:
                                    try:
                                        # Задержка для соблюдения rate limits (перед отправкой)
                                        await asyncio.sleep(delay_between_messages)
                                        
                                        # Отправка видео (если нужно)
                                        if send_ins_video:
                                            try:
                                                caption = "📹 Пожалуйста, внимательно посмотрите короткую видео‑инструкцию перед началом!"
                                                if video_file_id:
                                                    await bot.send_video(int(uid), video_file_id, caption=caption)
                                                    await asyncio.sleep(0.1)  # Задержка после видео
                                                elif video_path:
                                                    await bot.send_video(int(uid), FSInputFile(video_path), caption=caption)
                                                    await asyncio.sleep(0.1)  # Задержка после видео
                                            except Exception as ve:
                                                # Ошибка видео не критична, продолжаем отправку текста
                                                logger.warning(f"[NEWS] Ошибка отправки видео пользователю {uid}: {ve}")

                                        # Если есть медиа-аттач без caption (текст длинный) —
                                        # шлём медиа отдельным сообщением до текста.
                                        if media_separate:
                                            try:
                                                await _send_media_no_caption(uid)
                                                await asyncio.sleep(0.1)
                                            except Exception as me:
                                                # Ошибка медиа не критична — продолжаем текст без медиа
                                                logger.warning(f"[NEWS] Ошибка отправки медиа ({media_kind}) пользователю {uid}: {me}")

                                        # Основная отправка с обработкой Flood control:
                                        # • если media_inline_caption → одно сообщение «media + caption + кнопки»
                                        # • иначе → обычное текстовое сообщение
                                        max_retries = 3
                                        retry_delay = 1.0
                                        for attempt in range(max_retries):
                                            try:
                                                if media_inline_caption:
                                                    await _send_media_with_caption(uid)
                                                else:
                                                    await bot.send_message(int(uid), news_text, reply_markup=reply_markup, parse_mode="HTML")
                                                sent += 1
                                                # Логируем успешные отправки каждые 100 сообщений
                                                if sent % 100 == 0:
                                                    logger.info(f"[NEWS] ✅ Отправлено {sent} сообщений из {len(user_ids)}")
                                                break  # Успешно отправлено
                                            except Exception as e:
                                                error_str = str(e)
                                                # Проверяем на Flood control
                                                if "Flood control" in error_str or "Too Many Requests" in error_str or "retry after" in error_str:
                                                    retry_match = re.search(r'retry after (\d+)', error_str, re.IGNORECASE)
                                                    if retry_match:
                                                        retry_seconds = int(retry_match.group(1)) + 1  # +1 для запаса
                                                        logger.warning(f"[NEWS] Flood control для {uid}, ждем {retry_seconds} сек (попытка {attempt + 1}/{max_retries})")
                                                        await asyncio.sleep(retry_seconds)
                                                    else:
                                                        # Если не удалось извлечь время, используем экспоненциальную задержку
                                                        wait_time = retry_delay * (2 ** attempt)
                                                        logger.warning(f"[NEWS] Flood control для {uid}, ждем {wait_time} сек (попытка {attempt + 1}/{max_retries})")
                                                        await asyncio.sleep(wait_time)
                                                    
                                                    if attempt == max_retries - 1:
                                                        # Последняя попытка не удалась
                                                        raise
                                                else:
                                                    # Другая ошибка - пробрасываем дальше
                                                    raise
                                        
                                        # Задержка после успешной отправки для соблюдения rate limits
                                        await asyncio.sleep(0.05)
                                        
                                    except Exception as e:
                                        failed += 1
                                        error_str = str(e)
                                        errors.append({'telegram_id': uid, 'error': error_str})
                                        # Логируем ошибки, но не останавливаем процесс
                                        if failed <= 50:  # Логируем первые 50 ошибок
                                            logger.warning(f"[NEWS] Ошибка отправки пользователю {uid}: {error_str[:200]}")
                                        elif failed == 51:
                                            logger.info(f"[NEWS] Слишком много ошибок, прекращаем детальное логирование. Всего ошибок будет записано в БД.")
                            
                            # 4) Запускаем параллельную отправку с защитой от зависания
                            logger.info(f"[NEWS] Запускаем отправку для {len(user_ids)} пользователей...")
                            tasks = [send_to_user(uid) for uid in user_ids]
                            
                            # Добавляем задачу для логирования прогресса
                            last_sent = 0
                            last_failed = 0
                            async def log_progress():
                                nonlocal last_sent, last_failed
                                while True:
                                    await asyncio.sleep(30)  # Каждые 30 секунд
                                    current_sent = sent
                                    current_failed = failed
                                    sent_diff = current_sent - last_sent
                                    failed_diff = current_failed - last_failed
                                    logger.info(f"[NEWS] Прогресс: отправлено {current_sent} (+{sent_diff}), ошибок {current_failed} (+{failed_diff}) из {len(user_ids)}. Осталось: {len(user_ids) - current_sent - current_failed}")
                                    last_sent = current_sent
                                    last_failed = current_failed
                            
                            progress_task = asyncio.create_task(log_progress())
                            
                            try:
                                # Используем gather с return_exceptions=True, чтобы ошибки не останавливали весь процесс
                                results = await asyncio.gather(*tasks, return_exceptions=True)
                                
                                # Отменяем задачу логирования прогресса
                                progress_task.cancel()
                                try:
                                    await progress_task
                                except asyncio.CancelledError:
                                    pass
                                
                                # Подсчитываем реальные результаты из результатов gather
                                # Но счетчики sent/failed уже обновляются внутри send_to_user через nonlocal
                                # Проверяем только на случай расхождений
                                completed_count = len([r for r in results if not isinstance(r, Exception) or r is None])
                                exception_count = len([r for r in results if isinstance(r, Exception)])
                                
                                # Логируем финальную статистику
                                logger.info(f"[NEWS] Gather завершен: {completed_count} задач завершено, {exception_count} с исключениями")
                                logger.info(f"[NEWS] Счетчики: sent={sent}, failed={failed}, total={sent + failed}, expected={len(user_ids)}")
                                
                                logger.info(f"[NEWS] Отправка завершена. Успешно: {sent}, Ошибок: {failed} из {len(user_ids)}")
                            except Exception as gather_err:
                                progress_task.cancel()
                                logger.error(f"[NEWS] Ошибка в gather: {gather_err}", exc_info=True)
                                logger.info(f"[NEWS] Текущий прогресс: отправлено {sent}, ошибок {failed} из {len(user_ids)}")
                            
                            # 5) Закрываем сессию бота
                            try:
                                await bot.session.close()
                                logger.info(f"[NEWS] Сессия бота закрыта")
                            except Exception as close_err:
                                logger.warning(f"[NEWS] Ошибка при закрытии сессии: {close_err}")
                            
                            # Обновляем статус задачи асинхронно
                            # Ограничиваем размер данных об ошибках (первые 100 ошибок)
                            error_data = {
                                'error_details': [e['error'][:200] for e in errors[:100]],  # Ограничиваем длину и количество
                                'failed_users': errors[:100], 
                                'failed_user_ids': [e['telegram_id'] for e in errors[:100]],
                                'total_errors': len(errors)  # Общее количество ошибок
                            }
                            
                            try:
                                await async_execute_db(
                                    "UPDATE news_tasks SET status = ?, success_count = ?, failed_count = ?, error_details = ?, completed_at = ? WHERE task_id = ?",
                                    ('completed', sent, failed, json.dumps(error_data), datetime.now().isoformat(), task_id)
                                )
                                logger.info(f"[NEWS] Задача {task_id} завершена. Статус обновлен в БД. Успешно: {sent}, Ошибок: {failed}")
                            except Exception as db_err:
                                logger.error(f"[NEWS] Ошибка обновления статуса задачи в БД: {db_err}")
                        except Exception as e:
                            logger.error(f"[NEWS] КРИТИЧЕСКАЯ ОШИБКА в send_messages: {e}", exc_info=True)
                            try:
                                await async_execute_db(
                                    "UPDATE news_tasks SET status = ?, error_details = ?, completed_at = ? WHERE task_id = ?",
                                    ('failed', json.dumps({'error': str(e)}), datetime.now().isoformat(), task_id)
                                )
                            except Exception:
                                pass
            
            logger.info(f"[NEWS] Создаем фоновую задачу для отправки новостей. Task ID: {task_id}, Пользователей: {len(user_ids)}")
            try:
                task = asyncio.create_task(send_messages())
                logger.info(f"[NEWS] Фоновая задача создана: {task}")
            except Exception as task_err:
                logger.error(f"[NEWS] ОШИБКА создания фоновой задачи: {task_err}", exc_info=True)
                # Пытаемся обновить статус задачи
                try:
                    await async_execute_db(
                        "UPDATE news_tasks SET status = ?, error_details = ?, completed_at = ? WHERE task_id = ?",
                        ('failed', json.dumps({'error': f'Failed to create task: {task_err}'}), datetime.now().isoformat(), task_id)
                    )
                except Exception:
                    pass
            
            # Если это AJAX запрос, возвращаем JSON
            if is_ajax:
                return jsonify({'success': True, 'task_id': task_id, 'user_count': len(user_ids)}), 200
            
            await flash(f'Отправка новости {len(user_ids)} пользователям запущена. ID задачи: {task_id}', 'info')
            return redirect(url_for('admin.users_list'))
            
        except Exception as e:
            logger.error(f"[NEWS] Ошибка при создании задачи отправки новости: {e}", exc_info=True)
            if is_ajax:
                return jsonify({'error': f'Ошибка при создании задачи отправки новости: {str(e)}'}), 500
            await flash(f'Ошибка при создании задачи отправки новости: {e}', 'danger')
            return redirect(url_for('admin.users_list'))


