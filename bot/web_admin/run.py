# ─── Стандартная библиотека ──────────────────────────────────────────────────
import asyncio
import base64
import json
import logging
import math
import mimetypes
import os
import random
import re
import shutil
import sqlite3
import string
import subprocess
import sys
import tempfile
import traceback

# Регистрируем MIME-типы шрифтов до импорта/инициализации Quart.
# На Windows mimetypes.guess_type не знает про .woff2 → send_from_directory
# отдаёт application/octet-stream, и Chrome отказывается парсить его как
# шрифт в CSS @font-face (флаги стран не отображаются).
mimetypes.add_type('font/woff2', '.woff2')
mimetypes.add_type('font/woff', '.woff')
mimetypes.add_type('font/ttf', '.ttf')
mimetypes.add_type('font/otf', '.otf')
# .mjs Python тоже исторически знает не везде — на всякий случай форсим.
mimetypes.add_type('text/javascript', '.mjs')
from datetime import datetime, timezone, timedelta
from functools import wraps
from typing import Optional

# Корень проекта в sys.path — должно быть ДО сторонних/локальных импортов
_project_root = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# ─── Сторонние библиотеки ────────────────────────────────────────────────────
import aiofiles
import aiosqlite
import httpx
import pytz
from aiogram.types.input_file import FSInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from hypercorn.asyncio import serve as hypercorn_serve
from hypercorn.config import Config as HypercornConfig
from loguru import logger
from quart import (Quart, Blueprint, Response, abort, flash, g, jsonify,
                   redirect, render_template, request, send_file,
                   send_from_directory, session, url_for)
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

# ─── Локальные модули ────────────────────────────────────────────────────────
import db_helpers
from app_config import app_conf
from src.telegram_bot_factory import make_aiogram_bot
from keyboards import get_back_to_main_keyboard
from subscription_manager import grant_subscription
from tg_sender import send_telegram_message
from web_admin.core.sqlite_hot_backup import build_backup_zip
from web_admin.core.s3_uploader import (
    S3NotConfigured,
    S3UploadError,
    upload_file_async,
)
from web_admin.async_db import async_query_db, async_execute_db
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')
os.makedirs(LOGS_DIR, exist_ok=True)

# Настройка loguru
logger.remove()
# Используем enqueue=True для безопасной записи в файл в Windows (избегает блокировок)
# И delay=True чтобы не блокировать при старте
logger.add(
    os.path.join(LOGS_DIR, "web_admin.log"), 
    rotation="10 MB", 
    retention="10 days", 
    encoding="utf-8", 
    level="INFO",
    enqueue=True,  # Потокобезопасная запись
    backtrace=True,
    diagnose=True
)

# Настройка стандартного logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, "web_admin.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)

logger.info("Веб-админка запущена")

for _name in (
    'apscheduler',
    'apscheduler.executors.default',
    'apscheduler.scheduler',
    'apscheduler.jobstores.default',
):
    _lg = logging.getLogger(_name)
    _lg.setLevel(logging.WARNING)
    _lg.propagate = False

# --- Настройки ---
# Путь к БД берём у бота: он лежит в корне проекта, а не в папке web_admin,
# и вычислять его тут вторым литералом значит разъехаться при переименовании.
from config import DATABASE_NAME as DATABASE_PATH

# SECRET_KEY для сессий - храним в БД для работы с несколькими workers
# Это критично для работы с несколькими workers, чтобы сессии не терялись

async def _get_or_create_secret_key_async():
    """Асинхронно получает SECRET_KEY из БД или создает новый"""
    SECRET_KEY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.web_admin_secret_key')
    
    try:
        async with aiosqlite.connect(DATABASE_PATH, timeout=10) as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            
            # Пытаемся получить ключ из БД
            async with conn.execute("SELECT value FROM settings WHERE key = 'web_admin_secret_key'") as cursor:
                row = await cursor.fetchone()
            
            if row and row[0]:
                # Ключ найден в БД - декодируем из base64
                try:
                    secret_key = base64.b64decode(row[0])
                    logger.info("[SECRET_KEY] Загружен из БД")
                    return secret_key
                except Exception as e:
                    logger.warning(f"[SECRET_KEY] Ошибка декодирования ключа из БД: {e}, генерируем новый")
            
            # Ключа нет в БД - проверяем файл для миграции
            secret_key = None
            if os.path.exists(SECRET_KEY_FILE):
                try:
                    with open(SECRET_KEY_FILE, 'rb') as f:
                        secret_key = f.read()
                    logger.info("[SECRET_KEY] Найден ключ в файле, переносим в БД")
                except Exception as e:
                    logger.warning(f"[SECRET_KEY] Ошибка чтения ключа из файла: {e}")
            
            # Если ключа нет ни в БД, ни в файле - генерируем новый
            if not secret_key:
                secret_key = os.urandom(32)
                logger.info("[SECRET_KEY] Сгенерирован новый ключ")
            
            # Сохраняем в БД
            secret_key_b64 = base64.b64encode(secret_key).decode('utf-8')
            try:
                await conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value, description) VALUES (?, ?, ?)",
                    ('web_admin_secret_key', secret_key_b64, 'Секретный ключ для сессий веб-админки (base64)')
                )
                await conn.commit()
                logger.info("[SECRET_KEY] Ключ сохранен в БД")
            except Exception as e:
                logger.error(f"[SECRET_KEY] Ошибка сохранения ключа в БД: {e}")
                # Fallback: сохраняем в файл для обратной совместимости
                try:
                    with open(SECRET_KEY_FILE, 'wb') as f:
                        f.write(secret_key)
                    os.chmod(SECRET_KEY_FILE, 0o600)
                    logger.info("[SECRET_KEY] Сохранен в файл как fallback")
                except Exception:
                    pass
            
            return secret_key
            
    except Exception as e:
        logger.error(f"[SECRET_KEY] Ошибка при работе с БД: {e}, используем fallback на файл")
        # Fallback на файл для обратной совместимости
        if os.path.exists(SECRET_KEY_FILE):
            try:
                with open(SECRET_KEY_FILE, 'rb') as f:
                    secret_key = f.read()
                logger.info("[SECRET_KEY] Загружен из файла (fallback)")
                return secret_key
            except Exception:
                pass
        
        # Если ничего не получилось - генерируем новый
        secret_key = os.urandom(32)
        try:
            with open(SECRET_KEY_FILE, 'wb') as f:
                f.write(secret_key)
            os.chmod(SECRET_KEY_FILE, 0o600)
            logger.warning("[SECRET_KEY] Сгенерирован новый ключ и сохранен в файл (fallback)")
        except Exception:
            logger.error("[SECRET_KEY] Не удалось сохранить ключ ни в БД, ни в файл!")
        return secret_key

# Выполняем async функцию синхронно при импорте модуля
# Это безопасно, так как происходит до создания event loop приложения
try:
    SECRET_KEY = asyncio.run(_get_or_create_secret_key_async())
except RuntimeError:
    # Если уже есть запущенный event loop (редкий случай),
    # используем fallback на синхронный доступ
    SECRET_KEY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.web_admin_secret_key')
    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = 'web_admin_secret_key'")
        row = cursor.fetchone()
        if row and row[0]:
            SECRET_KEY = base64.b64decode(row[0])
            logger.info("[SECRET_KEY] Загружен из БД (синхронный fallback)")
        else:
            # Проверяем файл для миграции
            if os.path.exists(SECRET_KEY_FILE):
                with open(SECRET_KEY_FILE, 'rb') as f:
                    SECRET_KEY = f.read()
                logger.info("[SECRET_KEY] Загружен из файла (синхронный fallback)")
            else:
                SECRET_KEY = os.urandom(32)
                logger.info("[SECRET_KEY] Сгенерирован новый ключ (синхронный fallback)")
            
            # Сохраняем в БД
            if SECRET_KEY:
                secret_key_b64 = base64.b64encode(SECRET_KEY).decode('utf-8')
                try:
                    cursor.execute(
                        "INSERT OR REPLACE INTO settings (key, value, description) VALUES (?, ?, ?)",
                        ('web_admin_secret_key', secret_key_b64, 'Секретный ключ для сессий веб-админки (base64)')
                    )
                    conn.commit()
                except Exception:
                    pass
        conn.close()
    except Exception:
        # Последний fallback - генерируем новый
        SECRET_KEY = os.urandom(32)
        logger.warning("[SECRET_KEY] Использован последний fallback - сгенерирован новый ключ")

# --- Инициализация Quart ---
app = Quart(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
app.config['DATABASE_PATH'] = DATABASE_PATH
# Увеличиваем лимит размера тела запроса для массовой отправки новостей (до 50MB)
app.config['MAX_CONTENT_LENGTH'] = 60 * 1024 * 1024  # 60 MB (50 МБ файл рассылки + headers/запас)
# Настройка сессий для работы с несколькими workers
# В Quart сессии хранятся в подписанных cookies, поэтому фиксированный SECRET_KEY критичен
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)  # Сессии живут 7 дней
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = False  # True если используете HTTPS
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# --- Простейшая сессия-авторизация (без сторонних пакетов) ---
async def _unauthorized_response():
    try:
        is_api_path = '/api/' in request.path
        wants_json = (request.accept_mimetypes and request.accept_mimetypes.accept_json and \
                      request.accept_mimetypes['application/json'] >= request.accept_mimetypes['text/html'])
        is_xhr = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        if is_api_path or wants_json or is_xhr:
            return jsonify({'error': 'unauthorized', 'login_url': url_for('admin.login', _external=False)}), 401
    except Exception:
        pass
    return redirect(url_for('admin.login'))

def login_required(func):
    @wraps(func)
    async def _wrapped(*args, **kwargs):
        if not session.get('admin_user_id'):
            resp = await _unauthorized_response()
            return resp
        return await func(*args, **kwargs)
    return _wrapped

def admin_only(func):
    """Только для администратора. Модератор получает 403."""
    @wraps(func)
    async def _wrapped(*args, **kwargs):
        if session.get('admin_role') != 'admin':
            return '', 403
        return await func(*args, **kwargs)
    return _wrapped

class AdminUser:
    def __init__(self, user_id: str, role: str = 'admin'):
        self.id = user_id
        self.role = role

class _CurrentUser:
    @property
    def is_authenticated(self) -> bool:
        return bool(session.get('admin_user_id'))

    @property
    def id(self) -> str:
        # Может вернуть пустую строку для гостей — главное, не Undefined,
        # иначе |tojson в шаблонах падает с TypeError.
        return str(session.get('admin_user_id') or '')

    @property
    def role(self) -> str:
        return session.get('admin_role', 'admin')

    @property
    def is_admin(self) -> bool:
        return session.get('admin_role') == 'admin'

    @property
    def is_moderator(self) -> bool:
        return session.get('admin_role') == 'moderator'

current_user = _CurrentUser()

async def login_user(user: AdminUser):
    session['admin_user_id'] = str(user.id)
    session['admin_role'] = user.role

async def logout_user():
    session.pop('admin_user_id', None)
    session.pop('admin_role', None)

# --- Фильтры Jinja: безопасное имя для отображения/заголовков ---
def _safe_name(value: str, max_len: int = 50) -> str:
    try:
        s = value or ""
        # убрать непечатаемые символы
        s = "".join(ch for ch in s if ch.isprintable())
        # нормализовать пробелы
        s = re.sub(r"\s+", " ", s).strip()
        # оставить безопасные ASCII символы
        s = re.sub(r"[^A-Za-z0-9 _\.-]", "_", s)
        # убрать ведущие точки/слеши
        s = s.lstrip(".\\/")
        # схлопнуть подряд идущие _ и .
        s = re.sub(r"[_.]{2,}", "_", s)
        s = s[:max_len] if len(s) > max_len else s
        return s or "user"
    except Exception:
        return "user"

app.jinja_env.filters['safe_name'] = _safe_name

# Глобальные обработчики ошибок: для API всегда возвращаем JSON
@app.errorhandler(HTTPException)
async def handle_http_exception(e: HTTPException):
    try:
        if '/api/' in request.path:
            payload = {
                'error': e.name,
                'message': e.description,
                'code': e.code
            }
            return jsonify(payload), e.code
    except Exception:
        pass
    return e

@app.errorhandler(Exception)
async def handle_unexpected_exception(e: Exception):
    logger.exception(f"Unexpected error: {e}")
    try:
        if '/api/' in request.path:
            return jsonify({'error': 'internal_error', 'message': str(e)}), 500
    except Exception:
        pass
    raise e

admin_bp = Blueprint('admin', __name__, url_prefix='/')
def _host_is_ip(host: str) -> bool:
    """Проверяет, что Host — IP-адрес (доступ по IP запрещён)."""
    if not host or ':' in host:
        host = host.split(':')[0] if host else ''
    return bool(re.match(r'^\d+\.\d+\.\d+\.\d+$', host))


@admin_bp.before_request
async def _admin_auth_guard():
    try:
        # Запрет доступа по IP — только по домену
        if _host_is_ip(request.host or ''):
            return '', 403

        # ── IP whitelist (settings.admin_ip_whitelist_enabled = '1') ───────
        # Любой запрос в admin_bp до cookie/auth должен пройти проверку.
        # Loopback всегда разрешён — иначе systemd/healthcheck/CLI ломаются.
        # Если случайно отрезали себе доступ — сбросьте через SQLite:
        #   sqlite3 router_bot.db "UPDATE settings SET value='0' \
        #     WHERE key='admin_ip_whitelist_enabled'"
        try:
            from web_admin.core.ip_whitelist import (
                get_whitelist_state, resolve_client_ip,
                is_ip_allowed, is_loopback,
            )
            wl_enabled, wl_networks, _ = await get_whitelist_state()
            if wl_enabled:
                client_ip = resolve_client_ip(request)
                if not is_loopback(client_ip) and not is_ip_allowed(client_ip, wl_networks):
                    logger.warning(
                        "[WHITELIST] blocked %s for %s (UA=%r)",
                        str(client_ip),
                        request.path,
                        request.headers.get('User-Agent', '')[:120],
                    )
                    # 404 — не палим существование секретного пути
                    return '', 404
        except Exception as _wl_e:
            # Любая ошибка whitelist-логики НЕ должна ронять админку.
            logger.warning("[WHITELIST] guard error: %s", _wl_e)

        ep = (request.endpoint or '')
        allowed = {
            'admin.login',
            'admin.secret_static',
            'admin.secret_favicon',
            'admin.admin_static',
            # PWA-эндпоинты — браузер запрашивает их без cookies при
            # установке/регистрации Service Worker'а; секретный префикс
            # уже защищает их от анонимного интернета.
            'admin.pwa_manifest',
            'admin.pwa_service_worker',
            'admin.pwa_offline',
            # push_config — может запрашиваться SW при pushsubscriptionchange
            # (когда подписка устарела), но и в этом случае защищён секретным
            # префиксом. Сам по себе отдаёт лишь public-ключ + флаг enabled.
            'admin.push_config',
        }
        if ep in allowed:
            return None
        if not session.get('admin_user_id'):
            return await _unauthorized_response()
        # Выход доступен всем авторизованным
        if ep == 'admin.logout':
            return None
        # Для модератора загружаем разрешённые разделы и проверяем доступ к маршруту
        if session.get('admin_role') == 'moderator':
            row = await async_query_db("SELECT value FROM settings WHERE key = 'moderator_sections'", (), one=True)
            sections = []
            if row and row.get('value'):
                try:
                    sections = json.loads(row['value'])
                    if not isinstance(sections, list):
                        sections = []
                except Exception:
                    sections = []
            g.moderator_visible_sections = set(sections)
            # Проверка доступа по пути: настройки и панели — только админ.
            # Аналитика теперь управляется разделом 'analytics' (см. ниже).
            path = (request.path or '').strip().rstrip('/')
            # Remnawave на user_details: трафик по нодам и IP-сессии — модератор с разделом users.
            _moderator_users_rw_api = (
                'users' in g.moderator_visible_sections
                and (
                    '/api/remnawave/user/' in path
                    or '/api/ip_lookup' in path
                )
            )
            if not _moderator_users_rw_api:
                _admin_only_substrings = ('/settings/', '/remnawave', '/panels', '/bulk-actions')
                if any(s in path for s in _admin_only_substrings) or path.endswith('/settings') or path.endswith('/remnawave'):
                    return '', 403
            # Разделы по префиксу пути
            _path_to_section = [
                ('/analytics', 'analytics'),
                ('/api/remnawave/user/', 'users'),
                ('/api/ip_lookup', 'users'),
                ('/users', 'users'), ('/payments', 'payments'), ('/referral', 'bonuses'), ('/referrals', 'bonuses'), ('/partners', 'bonuses'), ('/promo', 'bonuses'),
                ('/tariffs', 'tariffs'), ('/traffic-topup', 'tariffs'), ('/traffic_topup', 'tariffs'),
                ('/migrate', 'tools'), ('/tasks', 'tools'), ('/reports', 'tools'),
                ('/news', 'tools'), ('/inbound-templates', 'tools'), ('/services', 'tools'),
                ('/bulk-actions', 'tools'),
                ('/updates', 'updates'),
            ]
            req_section = None
            # path может быть /{admin_secret_path}/users — проверяем вхождение сегмента
            for prefix, sec in _path_to_section:
                if prefix in path:
                    req_section = sec
                    break
            if req_section is None:
                req_section = 'dashboard'  # корневая страница
            if req_section and req_section not in g.moderator_visible_sections:
                if g.moderator_visible_sections:
                    first_url = _moderator_first_allowed_url(list(g.moderator_visible_sections))
                    return redirect(first_url)
                return '', 403
        else:
            g.moderator_visible_sections = None  # admin — видит всё
    except Exception:
        return await _unauthorized_response()

def _moderator_can_see(section_id: str) -> bool:
    """Проверяет, может ли модератор видеть раздел. Админ всегда True."""
    sections = getattr(g, 'moderator_visible_sections', None)
    if sections is None:
        return True
    return section_id in sections

@admin_bp.app_context_processor
def inject_moderator_context():
    from web_admin.routes.pwa import PWA_CACHE_VERSION
    dedup_raw = app_conf.get('sub_link_dedup_enabled', '0')
    return {
        'moderator_can_see': _moderator_can_see,
        'pwa_cache_version': PWA_CACHE_VERSION,
        'host_balancer_enabled': dedup_raw in ('1', 'true', 'yes', 'on'),
    }


# --- Санитизация HTML под требования Telegram ---
_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "tg-spoiler", "blockquote"}

def sanitize_news_html(html: str) -> str:
    if not html:
        return html
    text = html
    # Нормализуем переносы (убираем <br>)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>\s*", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "", text, flags=re.IGNORECASE)
    # Списки -> тире
    text = re.sub(r"</?ul[^>]*>", "", text, flags=re.IGNORECASE)
    def _li_repl(m):
        inner = m.group(1).strip()
        return f"— {inner}\n"
    text = re.sub(r"<li[^>]*>([\s\S]*?)</li>", _li_repl, text, flags=re.IGNORECASE)
    # Заголовки -> жирный + перевод строки
    for h in [1,2,3,4,5,6]:
        text = re.sub(fr"<h{h}[^>]*>([\s\S]*?)</h{h}>", r"<b>\1</b>\n\n", text, flags=re.IGNORECASE)
    # Сохраняем <blockquote> как есть, чтобы Telegram отрисовал цитирование
    # Удаляем все теги, кроме разрешенных; для <a> оставляем только href
    def _tag_replacer(m):
        tag = m.group(1).lower()
        closing = m.group(0).startswith("</")
        if tag not in _ALLOWED_TAGS:
            return ""
        if tag == "a":
            if closing:
                return "</a>"
            # Извлекаем href
            href_match = re.search(r"href\s*=\s*\"([^\"]+)\"|href\s*=\s*'([^']+)'", m.group(0), flags=re.IGNORECASE)
            href = href_match.group(1) if href_match and href_match.group(1) else (href_match.group(2) if href_match else None)
            if not href:
                return ""
            return f"<a href=\"{href}\">"
        # Для остальных разрешенных — чистим атрибуты
        return f"</{tag}>" if closing else f"<{tag}>"

    text = re.sub(r"</?([a-zA-Z0-9\-]+)(?:\s+[^>]*)?>", _tag_replacer, text)
    
    # Балансируем инлайн‑теги внутри блоков <blockquote> и <tg-spoiler>
    def _balance_inline_inside(block_tag: str, s: str) -> str:
        pattern = re.compile(fr"<{block_tag}>([\s\S]*?)</{block_tag}>", re.IGNORECASE)
        def repl(m):
            inner = m.group(1)
            # Считаем дисбаланс для <b> и <i>
            for t in ("b", "i"):
                opens = len(re.findall(fr"<{t}>", inner, flags=re.IGNORECASE))
                closes = len(re.findall(fr"</{t}>", inner, flags=re.IGNORECASE))
                if opens > closes:
                    inner = inner + (f"</{t}>" * (opens - closes))
            return f"<{block_tag}>" + inner + f"</{block_tag}>"
        return pattern.sub(repl, s)

    text = _balance_inline_inside('blockquote', text)
    text = _balance_inline_inside('tg-spoiler', text)
    # Балансируем незакрытые теги (простая корректировка)
    for tag in list(_ALLOWED_TAGS):
        open_cnt = len(re.findall(fr"<{tag}>", text))
        close_cnt = len(re.findall(fr"</{tag}>", text))
        if open_cnt > close_cnt:
            text += "</" + tag + ">" * (open_cnt - close_cnt)
    # Убираем лишние множественные переводы строк (больше двух подряд → два)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Убираем хвостовые пустые строки
    text = re.sub(r"(\n\s*)+$", "", text)
    return text.strip()

def _moderator_first_allowed_url(sections: list) -> str:
    """Возвращает URL первого разрешённого раздела для модератора."""
    _section_to_url = [
        ('dashboard', 'admin.dashboard'),
        ('users', 'admin.users_list'),
        ('payments', 'admin.payments'),
        ('analytics', 'admin.analytics'),
        ('bonuses', 'admin.referral_stats'),
        ('tariffs', 'admin.tariffs_list'),
        ('tools', 'admin.tasks_list'),
        ('updates', 'admin.updates'),
    ]
    for sid, endpoint in _section_to_url:
        if sid in sections:
            return url_for(endpoint)
    return url_for('admin.dashboard')

@admin_bp.route('/login', methods=['GET', 'POST'])
async def login():
    if current_user.is_authenticated:
        if getattr(current_user, 'role', None) == 'moderator':
            row = await async_query_db("SELECT value FROM settings WHERE key = 'moderator_sections'", (), one=True)
            sections = []
            if row and row.get('value'):
                try:
                    sections = json.loads(row['value'])
                    if not isinstance(sections, list):
                        sections = []
                except Exception:
                    sections = []
            return redirect(_moderator_first_allowed_url(sections))
        return redirect(url_for('admin.dashboard'))
    if request.method == 'POST':
        form = await request.form
        password_attempt = form.get('password')
        admin_row = await async_query_db("SELECT value FROM settings WHERE key = 'admin_web_password'", (), one=True)
        moderator_row = await async_query_db("SELECT value FROM settings WHERE key = 'moderator_web_password'", (), one=True)
        if admin_row and password_attempt == admin_row['value']:
            await login_user(AdminUser(user_id='1', role='admin'))
            session.permanent = True
            await flash('Вы вошли как администратор.', 'success')
            return redirect(url_for('admin.dashboard'))
        if moderator_row and moderator_row.get('value') and password_attempt == moderator_row['value']:
            await login_user(AdminUser(user_id='mod', role='moderator'))
            session.permanent = True
            await flash('Вы вошли как модератор.', 'success')
            sections_row = await async_query_db("SELECT value FROM settings WHERE key = 'moderator_sections'", (), one=True)
            sections = []
            if sections_row and sections_row.get('value'):
                try:
                    sections = json.loads(sections_row['value'])
                    if not isinstance(sections, list):
                        sections = []
                except Exception:
                    sections = []
            return redirect(_moderator_first_allowed_url(sections))
        await flash('Неверный пароль.', 'danger')
    return await render_template('login.html')

@admin_bp.route('/logout')
@login_required
async def logout():
    await logout_user()
    await flash('Вы вышли из системы.', 'info')
    return redirect(url_for('admin.login'))

def _format_bytes_human(num_bytes: int) -> str:
    """Человекочитаемый объём (Б/КБ/МБ/ГБ/…) для суточного трафика."""
    n = float(num_bytes or 0)
    if n <= 0:
        return '0 Б'
    units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ', 'ПБ']
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    digits = 2 if i >= 3 else (1 if i >= 2 else 0)
    return f'{n:.{digits}f} {units[i]}'


@admin_bp.route('/')
@login_required
async def dashboard():
    
    stats = {}
    # Проверяем, существует ли колонка is_blocked
    columns = await async_query_db("PRAGMA table_info(users)", ())
    column_names = [col['name'] for col in columns]
    has_is_blocked = 'is_blocked' in column_names
    
    if has_is_blocked:
        stats['total_users'] = (await async_query_db("SELECT COUNT(*) FROM users WHERE COALESCE(is_blocked, 0) = 0", (), one=True))['COUNT(*)']
        stats['blocked_users'] = (await async_query_db("SELECT COUNT(*) FROM users WHERE COALESCE(is_blocked, 0) = 1", (), one=True))['COUNT(*)']
        stats['active_subs'] = await db_helpers.count_active_subscription_users()
        stats['trial_users'] = (await async_query_db("SELECT COUNT(*) FROM users WHERE is_trial_used = 1 AND COALESCE(is_blocked, 0) = 0", (), one=True))['COUNT(*)']
        stats['empty_uuid_users'] = (await async_query_db("SELECT COUNT(*) FROM users WHERE (xui_client_uuid IS NULL OR xui_client_uuid = '') AND COALESCE(is_blocked, 0) = 0", (), one=True))['COUNT(*)']
        try:
            stats['users_today'] = (await async_query_db("SELECT COUNT(*) FROM users WHERE COALESCE(is_blocked,0)=0 AND date(created_at) = date('now')", (), one=True))['COUNT(*)']
            stats['users_week'] = (await async_query_db("SELECT COUNT(*) FROM users WHERE COALESCE(is_blocked,0)=0 AND datetime(created_at) >= datetime('now','-7 days')", (), one=True))['COUNT(*)']
        except Exception:
            stats['users_today'] = 0
            stats['users_week'] = 0
    else:
        stats['total_users'] = (await async_query_db("SELECT COUNT(*) FROM users", (), one=True))['COUNT(*)']
        stats['blocked_users'] = 0
        stats['active_subs'] = await db_helpers.count_active_subscription_users(exclude_blocked=False)
        stats['trial_users'] = (await async_query_db("SELECT COUNT(*) FROM users WHERE is_trial_used = 1", (), one=True))['COUNT(*)']
        stats['empty_uuid_users'] = (await async_query_db("SELECT COUNT(*) FROM users WHERE xui_client_uuid IS NULL OR xui_client_uuid = ''", (), one=True))['COUNT(*)']
        try:
            stats['users_today'] = (await async_query_db("SELECT COUNT(*) FROM users WHERE date(created_at) = date('now')", (), one=True))['COUNT(*)']
            stats['users_week'] = (await async_query_db("SELECT COUNT(*) FROM users WHERE datetime(created_at) >= datetime('now','-7 days')", (), one=True))['COUNT(*)']
        except Exception:
            stats['users_today'] = 0
            stats['users_week'] = 0
    
    stats['successful_payments'] = (await async_query_db("SELECT COUNT(*) FROM payments WHERE status = 'succeeded'", (), one=True))['COUNT(*)']
    total_amount_row = await async_query_db("SELECT SUM(amount) FROM payments WHERE status = 'succeeded'", (), one=True)
    # За сегодня (UTC) — количество и сумма
    try:
        today_count_row = await async_query_db("SELECT COUNT(*) FROM payments WHERE status = 'succeeded' AND created_at >= date('now')", (), one=True)
        today_sum_row = await async_query_db("SELECT SUM(amount) FROM payments WHERE status = 'succeeded' AND created_at >= date('now')", (), one=True)
        stats['payments_today_count'] = today_count_row['COUNT(*)'] if today_count_row else 0
        stats['payments_today_sum'] = today_sum_row['SUM(amount)'] if today_sum_row and today_sum_row['SUM(amount)'] else 0
    except Exception:
        stats['payments_today_count'] = 0
        stats['payments_today_sum'] = 0
    stats['total_amount'] = total_amount_row['SUM(amount)'] if total_amount_row and total_amount_row['SUM(amount)'] else 0
    # Разбивка платежей по валютам
    try:
        rows = await async_query_db("SELECT currency, COUNT(*), SUM(amount) FROM payments WHERE status = 'succeeded' GROUP BY currency", ())
        payments_by_currency = []
        for r in rows:
            currency = r['currency']
            count = r['COUNT(*)']
            total = r['SUM(amount)']
            payments_by_currency.append({
                'currency': currency or 'N/A',
                'count': int(count or 0),
                'total': float(total or 0)
            })
        stats['payments_by_currency'] = payments_by_currency
    except Exception:
        stats['payments_by_currency'] = []
    # Промокоды: считаем использованные как те, у которых used_count > 0 (fallback: is_active=0)
    try:
        stats['promo_activated'] = (await async_query_db(
            "SELECT COUNT(*) FROM promo_codes WHERE COALESCE(used_count, 0) > 0", (), one=True
        ))['COUNT(*)']
    except Exception:
        # На старой схеме used_count может отсутствовать
        stats['promo_activated'] = (await async_query_db(
            "SELECT COUNT(*) FROM promo_codes WHERE is_active = 0", (), one=True
        ))['COUNT(*)']
    stats['promo_total'] = (await async_query_db("SELECT COUNT(*) FROM promo_codes", (), one=True))['COUNT(*)']

    # Топ-10 по трафику за сутки (users.total_bytes — суточный расход из синка
    # Remnawave, как в разделе «Клиенты» → daily_consumption). online_at — онлайн.
    top_daily_traffic = []
    try:
        if 'total_bytes' in column_names:
            has_online_col = 'online_at' in column_names
            online_sel = ', online_at' if has_online_col else ''
            blocked_w = "AND COALESCE(is_blocked, 0) = 0" if has_is_blocked else ""
            dt_rows = await async_query_db(
                f"""SELECT telegram_id, username, real_username,
                           COALESCE(total_bytes, 0) AS daily_bytes{online_sel}
                    FROM users
                    WHERE COALESCE(total_bytes, 0) > 0 {blocked_w}
                    ORDER BY daily_bytes DESC
                    LIMIT 10""",
                ()
            )
            from web_admin.routes.users import _format_last_online
            for r in (dt_rows or []):
                r = dict(r)
                name = (
                    r.get('username')
                    or (r.get('real_username') or '').lstrip('@')
                    or str(r.get('telegram_id'))
                )
                st = (
                    _format_last_online(r.get('online_at'))
                    if has_online_col
                    else {'online_label': '—', 'online_is_live': False}
                )
                top_daily_traffic.append({
                    'telegram_id': int(r['telegram_id']),
                    'name': name,
                    'daily_human': _format_bytes_human(int(r.get('daily_bytes') or 0)),
                    'online_is_live': bool(st.get('online_is_live')),
                    'online_label': st.get('online_label') or '—',
                })
    except Exception:
        top_daily_traffic = []

    # Системная статистика сервера (psutil.cpu_percent блокирует 300мс — выносим в поток)
    system = await asyncio.to_thread(get_system_stats)
    
    # Проверяем, включен ли Remnawave
    await app_conf.load_settings()
    remnawave_enabled = app_conf.get('remnawave_enabled', '0') == '1'
    # Настроен ли Remnawave (есть Base URL и API Token). Если нет — на главной
    # показываем баннер с призывом заполнить данные в разделе «Remnawave · Настройки».
    remnawave_configured = bool(
        (app_conf.get('remnawave_base_url') or '').strip()
        and (app_conf.get('remnawave_api_token') or '').strip()
    )

    return await render_template(
        'dashboard.html', stats=stats, system=system,
        remnawave_enabled=remnawave_enabled, top_daily_traffic=top_daily_traffic,
        remnawave_configured=remnawave_configured,
    )

@admin_bp.route('/users/analytics')
@login_required
async def users_analytics():
    """API endpoint для получения данных для графиков аналитики пользователей"""
    try:
        period = request.args.get('period', '30days')  # today, yesterday, 7days, 30days, 60days, year(legacy), 90days(legacy), all

        # Определяем период и группировку.
        # Все границы считаем в UTC — SQLite datetime('now', ...) по умолчанию возвращает UTC,
        # а created_at пишется как datetime.now(timezone.utc).isoformat().
        date_filter_upper = None  # верхняя граница для периодов-диапазонов (например, "вчера")
        if period == 'today':
            date_filter = "datetime('now', 'start of day')"
            group_by = "date(created_at)"
        elif period == 'yesterday':
            date_filter       = "datetime('now', 'start of day', '-1 day')"
            date_filter_upper = "datetime('now', 'start of day')"
            group_by = "date(created_at)"
        elif period == '7days':
            date_filter = "datetime('now', '-7 days')"
            group_by = "date(created_at)"
        elif period == '30days':
            date_filter = "datetime('now', '-30 days')"
            group_by = "date(created_at)"
        elif period == '60days':
            date_filter = "datetime('now', '-60 days')"
            group_by = "date(created_at)"
        elif period == '90days':  # legacy
            date_filter = "datetime('now', '-90 days')"
            group_by = "date(created_at)"
        elif period == 'year':  # legacy
            date_filter = "datetime('now', '-1 year')"
            group_by = "strftime('%Y-%m', created_at)"
        elif period == 'all':
            date_filter = None  # без фильтра
            group_by = "strftime('%Y-%m', created_at)"
        else:
            date_filter = "datetime('now', '-30 days')"
            group_by = "date(created_at)"

        # SQL-фрагмент для применения фильтра периода (или без него для 'all').
        # Для диапазона (yesterday) — добавляем верхнюю границу через AND.
        if date_filter:
            period_where = f"AND created_at >= {date_filter}"
            if date_filter_upper:
                period_where += f" AND created_at < {date_filter_upper}"
        else:
            period_where = ""
        
        # Проверяем наличие колонки is_blocked
        columns = await async_query_db("PRAGMA table_info(users)", ())
        column_names = [col['name'] for col in columns]
        has_is_blocked = 'is_blocked' in column_names
        
        # Фильтр для незаблокированных пользователей
        blocked_filter = "AND COALESCE(is_blocked, 0) = 0" if has_is_blocked else ""
        
        # Верхняя граница (если есть, например для 'yesterday')
        upper_clause = f"AND created_at < {date_filter_upper}" if date_filter_upper else ""

        # Данные по дням/месяцам (регистрации) — разбивка по источнику
        query_tg = f"""
            SELECT {group_by} as period, COUNT(*) as count
            FROM users
            WHERE created_at >= {date_filter} {upper_clause} {blocked_filter}
              AND (registration_type = 'telegram' OR registration_type IS NULL OR registration_type = '')
            GROUP BY {group_by} ORDER BY period ASC
        """
        query_site = f"""
            SELECT {group_by} as period, COUNT(*) as count
            FROM users
            WHERE created_at >= {date_filter} {upper_clause} {blocked_filter}
              AND registration_type = 'site'
            GROUP BY {group_by} ORDER BY period ASC
        """
        rows_tg   = await async_query_db(query_tg, ())
        rows_site = await async_query_db(query_site, ())

        # Собираем все периоды (union)
        all_periods = sorted(set(
            [dict(r)['period'] for r in rows_tg] +
            [dict(r)['period'] for r in rows_site]
        ))
        tg_map   = {dict(r)['period']: dict(r)['count'] for r in rows_tg}
        site_map = {dict(r)['period']: dict(r)['count'] for r in rows_site}

        chart_data = {
            'labels': all_periods,
            'datasets': [
                {
                    'label': 'Telegram',
                    'data': [tg_map.get(p, 0) for p in all_periods],
                    'borderColor': 'rgb(42, 171, 238)',
                    'backgroundColor': 'rgba(42, 171, 238, 0.15)',
                    'tension': 0.4,
                    'fill': True
                },
                {
                    'label': 'Сайт',
                    'data': [site_map.get(p, 0) for p in all_periods],
                    'borderColor': 'rgb(139, 92, 246)',
                    'backgroundColor': 'rgba(139, 92, 246, 0.15)',
                    'tension': 0.4,
                    'fill': True
                }
            ]
        }
        
        # Статистика по статусам подписок (только пользователи, зарегистрированные в выбранный период)
        subscription_query = f"""
            SELECT
                CASE
                    WHEN datetime(subscription_end_date) > datetime('now', 'utc') THEN 'Активные'
                    WHEN subscription_end_date IS NULL THEN 'Без подписки'
                    ELSE 'Истекшие'
                END as status,
                COUNT(*) as count
            FROM users
            WHERE 1=1 {blocked_filter} {period_where}
            GROUP BY status
        """
        subscription_rows = await async_query_db(subscription_query, ())
        
        subscription_labels = []
        subscription_data = []
        subscription_colors = []
        
        for row in subscription_rows:
            r_dict = dict(row)
            status = r_dict['status']
            count = r_dict['count'] or 0
            subscription_labels.append(status)
            subscription_data.append(count)
            # Цвета для разных статусов
            if status == 'Активные':
                subscription_colors.append('rgb(25, 135, 84)')  # Зеленый
            elif status == 'Истекшие':
                subscription_colors.append('rgb(220, 53, 69)')  # Красный
            else:
                subscription_colors.append('rgb(108, 117, 125)')  # Серый
        
        subscription_stats = {
            'labels': subscription_labels,
            'datasets': [{
                'data': subscription_data,
                'backgroundColor': subscription_colors,
                'borderWidth': 2,
                'borderColor': '#fff'
            }]
        }
        
        # Статистика по использованию пробного периода
        trial_query = f"""
            SELECT 
                CASE 
                    WHEN is_trial_used = 1 THEN 'Использовали пробный'
                    ELSE 'Не использовали пробный'
                END as status,
                COUNT(*) as count
            FROM users 
            WHERE 1=1 {blocked_filter}
            GROUP BY status
        """
        trial_rows = await async_query_db(trial_query, ())
        
        trial_labels = []
        trial_data = []
        trial_colors = []
        
        for row in trial_rows:
            r_dict = dict(row)
            status = r_dict['status']
            count = r_dict['count'] or 0
            trial_labels.append(status)
            trial_data.append(count)
            # Цвета для пробного периода
            if status == 'Использовали пробный':
                trial_colors.append('rgb(13, 110, 253)')  # Синий
            else:
                trial_colors.append('rgb(108, 117, 125)')  # Серый
        
        trial_stats = {
            'labels': trial_labels,
            'datasets': [{
                'data': trial_data,
                'backgroundColor': trial_colors,
                'borderWidth': 2,
                'borderColor': '#fff'
            }]
        }
        
        # Статистика по источнику регистрации за выбранный период
        reg_type_query = f"""
            SELECT
                CASE
                    WHEN registration_type = 'site' THEN 'Сайт'
                    ELSE 'Telegram'
                END as src,
                COUNT(*) as count
            FROM users
            WHERE 1=1 {blocked_filter} {period_where}
            GROUP BY src
        """
        reg_type_rows = await async_query_db(reg_type_query, ())
        reg_labels, reg_data, reg_colors = [], [], []
        for row in reg_type_rows:
            r = dict(row)
            src = r['src']
            reg_labels.append(src)
            reg_data.append(r['count'] or 0)
            reg_colors.append('rgb(42, 171, 238)' if src == 'Telegram' else 'rgb(139, 92, 246)')

        registration_source_stats = {
            'labels': reg_labels,
            'datasets': [{
                'data': reg_data,
                'backgroundColor': reg_colors,
                'borderWidth': 2,
                'borderColor': '#fff'
            }]
        }

        return jsonify({
            'success': True,
            'chart_data': chart_data,
            'subscription_stats': subscription_stats,
            'trial_stats': trial_stats,
            'registration_source_stats': registration_source_stats
        })
        
    except Exception as e:
        logger.error(f"Ошибка получения аналитики пользователей: {e}\n{traceback.format_exc()}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

def get_system_stats():
    """Возвращает системные метрики сервера: CPU, память, диск.
    CPU: оценка по loadavg(1m) на ядро (в %).
    Память/диск: занято, всего, проценты.
    """
    result = {
        'cpu_percent': None,
        'load1': None,
        'cores': os.cpu_count() or 1,
        'mem_used_gb': None,
        'mem_total_gb': None,
        'mem_percent': None,
        'disk_used_gb': None,
        'disk_total_gb': None,
        'disk_percent': None,
    }
    # CPU: приоритет loadavg (Linux/Unix) — подпись в UI «оценка по loadavg/ядро»
    # loadavg даёт стабильную среднюю нагрузку за 1 мин; psutil — только fallback (Windows)
    try:
        import psutil  # type: ignore
        try:
            cores_ps = psutil.cpu_count(logical=True)
            if cores_ps and cores_ps > 0:
                result['cores'] = cores_ps
        except Exception:
            pass
    except Exception:
        pass

    # На Windows у `os` метод getloadavg() ОТСУТСТВУЕТ (это Unix-only) — ловим
    # AttributeError. На Linux/macOS функция есть, но при недоступности данных
    # бросает OSError. Покрываем оба случая.
    try:
        load1, load5, load15 = os.getloadavg()
        result['load1'] = round(load1, 2)
        if result['cores'] and result['cores'] > 0:
            cpu_from_load = (load1 / result['cores']) * 100
            cpu_from_load = max(0.0, min(cpu_from_load, 100.0))
            result['cpu_percent'] = round(cpu_from_load, 1)
    except (AttributeError, OSError):
        # loadavg недоступен (Windows) — fallback на psutil
        try:
            import psutil  # type: ignore
            cpu_p = psutil.cpu_percent(interval=0.3)
            if cpu_p is not None:
                result['cpu_percent'] = round(max(0.0, min(float(cpu_p), 100.0)), 1)
        except Exception:
            pass
    # Память: из /proc/meminfo (Linux). На Windows/macOS этого файла нет —
    # после Exception падаем в psutil-fallback ниже.
    mem_filled = False
    try:
        meminfo = {}
        with open('/proc/meminfo') as f:
            for line in f:
                if ':' in line:
                    k, v = line.split(':', 1)
                    meminfo[k.strip()] = v.strip()
        if 'MemTotal' in meminfo and 'MemAvailable' in meminfo:
            def kb_to_gb(kb_str):
                # "xxxx kB"
                val_kb = float(kb_str.split()[0])
                return val_kb / (1024**2)
            total_gb = kb_to_gb(meminfo['MemTotal'])
            avail_gb = kb_to_gb(meminfo['MemAvailable'])
            used_gb = max(total_gb - avail_gb, 0)
            percent = round((used_gb / total_gb) * 100, 1) if total_gb > 0 else None
            result.update({
                'mem_used_gb': round(used_gb, 2),
                'mem_total_gb': round(total_gb, 2),
                'mem_percent': percent,
            })
            mem_filled = True
    except Exception:
        pass
    if not mem_filled:
        # Fallback: psutil.virtual_memory() — работает на Windows/macOS/Linux
        try:
            import psutil  # type: ignore
            vm = psutil.virtual_memory()
            total_gb = vm.total / (1024 ** 3)
            used_gb  = (vm.total - vm.available) / (1024 ** 3)
            result.update({
                'mem_used_gb':  round(used_gb, 2),
                'mem_total_gb': round(total_gb, 2),
                'mem_percent':  round(vm.percent, 1),
            })
        except Exception:
            pass
    # Диск: корневой раздел
    try:
        du = shutil.disk_usage('/')
        total_gb = du.total / (1024**3)
        used_gb = du.used / (1024**3)
        percent = round((used_gb / total_gb) * 100, 1) if total_gb > 0 else None
        result.update({
            'disk_used_gb': round(used_gb, 2),
            'disk_total_gb': round(total_gb, 2),
            'disk_percent': percent,
        })
    except Exception:
        pass
    return result

@admin_bp.route('/api/system_stats')
@login_required
async def api_system_stats():
    try:
        data = await asyncio.to_thread(get_system_stats)
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"Error in api_system_stats: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/api/system_stats/stream')
@login_required
async def api_system_stats_stream():
    """SSE-стрим статистики сервера — сервер толкает данные каждые 2 сек без polling."""
    async def event_generator():
        while True:
            try:
                data = await asyncio.to_thread(get_system_stats)
                payload = json.dumps(data, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            except asyncio.CancelledError:
                break
            except Exception as e:
                yield f"data: {_json.dumps({'error': str(e)})}\n\n"
            await asyncio.sleep(2)

    return Response(
        event_generator(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',   # отключает буферизацию nginx
        }
    )

@admin_bp.route('/users__placeholder__moved')
@login_required
def users_list_moved_placeholder():
        return redirect(url_for('admin.users_list'))

@admin_bp.route('/users__placeholder__moved/<int:telegram_id>', methods=['GET', 'POST'])
@login_required
def user_details_moved_placeholder(telegram_id):
        return redirect(url_for('admin.user_details', telegram_id=telegram_id))
    

@admin_bp.route('/settings__placeholder__moved', methods=['GET', 'POST'])
@login_required
def settings_moved_placeholder():
    return redirect(url_for('admin.settings_general'))

@admin_bp.route('/settings/general__placeholder__moved', methods=['GET', 'POST'])
@login_required
def settings_general_moved_placeholder():
        return redirect(url_for('admin.settings_general'))

@admin_bp.route('/settings/texts__placeholder__moved', methods=['GET', 'POST'])
@login_required
def settings_texts_moved_placeholder():
        return redirect(url_for('admin.settings_texts'))

# --- Ajax-обновление одного текста через модальное окно ---
@admin_bp.route('/settings/texts/update__placeholder__moved', methods=['POST'])
@login_required
def settings_texts_update_moved_placeholder():
    return jsonify({'ok': False, 'moved': True}), 404


@admin_bp.route('/settings/servers__placeholder__moved', methods=['GET', 'POST'])
@login_required
def settings_servers_moved_placeholder():
    return redirect(url_for('admin.remnawave_dashboard'))

@admin_bp.route('/settings/servers/edit__placeholder__moved/<int:server_id>', methods=['GET', 'POST'])
@login_required
def edit_server_moved_placeholder(server_id):
        return redirect(url_for('admin.remnawave_dashboard'))


@admin_bp.route('/settings/servers/add__placeholder__moved', methods=['GET', 'POST'])
@login_required
def add_server_moved_placeholder():
        return redirect(url_for('admin.remnawave_dashboard'))

@admin_bp.route('/settings/servers/delete__placeholder__moved/<int:server_id>', methods=['POST'])
@login_required
def delete_server_moved_placeholder(server_id):
        return redirect(url_for('admin.remnawave_dashboard'))
        
@admin_bp.route('/promo__placeholder__moved')
@login_required
def promo_list_moved_placeholder():
    return redirect(url_for('admin.promo_list'))

@admin_bp.route('/promo/create__placeholder__moved', methods=['POST'])
@login_required
def promo_create_moved_placeholder():
    return redirect(url_for('admin.promo_create'))

@admin_bp.route('/promo/export__placeholder__moved')
@login_required
def promo_export_moved_placeholder():
    return redirect(url_for('admin.promo_export'))

@admin_bp.route('/users/<int:telegram_id>/renew__placeholder__moved', methods=['POST'])
@login_required
def renew_subscription_moved_placeholder(telegram_id):
    return redirect(url_for('admin.renew_subscription', telegram_id=telegram_id))

@admin_bp.route('/send_news__placeholder__moved', methods=['POST'])
@login_required
def send_news_moved_placeholder():
    return redirect(url_for('admin.send_news'))

@admin_bp.route('/settings/backup__placeholder__moved', methods=['GET', 'POST'])
@login_required
def settings_backup_moved_placeholder():
        return redirect(url_for('admin.settings_backup'))

@admin_bp.route('/manual_backup__placeholder__moved', methods=['POST'])
@login_required
def manual_backup_moved_placeholder():
    return redirect(url_for('admin.settings_backup'))

def _auto_backup_devices_db_path() -> str | None:
    """Путь к devices.db для автобэкапа. None, если модуль недоступен."""
    try:
        from devices.config import DEVICES_DB_PATH
        return os.path.abspath(DEVICES_DB_PATH)
    except Exception:
        return None


async def do_auto_backup():
    try:
        # Автобэкап: фоновая задача
        # Получаем настройки
        row = await async_query_db("SELECT * FROM backup_settings LIMIT 1", (), one=True)
        if not row or not row['enabled']:
            return

        admin_id = (row.get('admin_telegram_id') or '').strip()
        interval_hours = row.get('interval_hours', 3) or 3
        last_backup = row.get('last_backup')
        s3_enabled = int(row.get('s3_enabled') or 0)

        # S3 имеет приоритет: если он включён, в Telegram бэкап не уходит
        # (см. manual_backup для пояснения).
        if s3_enabled:
            admin_id = ''

        # Нужен хотя бы один канал доставки
        if not admin_id and not s3_enabled:
            return

        # Проверяем, пора ли делать бэкап по интервалу
        moscow_tz = pytz.timezone('Europe/Moscow')
        now = datetime.now(moscow_tz)

        # Если есть last_backup, проверяем интервал
        if last_backup:
            try:
                last_dt = datetime.strptime(last_backup[:19], '%Y-%m-%d %H:%M:%S')
                # Приводим last_dt к московскому времени для корректного сравнения
                if last_dt.tzinfo is None:
                    last_dt = moscow_tz.localize(last_dt)
                else:
                    last_dt = last_dt.astimezone(moscow_tz)

                # Проверяем, прошло ли достаточно времени
                time_diff = now - last_dt
                if time_diff.total_seconds() < interval_hours * 3600:
                    return  # Еще не прошло достаточно времени
            except Exception as e:
                logger.debug(f"[BACKUP] Ошибка парсинга last_backup: {e}")
                # Если не удалось распарсить, делаем бэкап

        # Делаем бэкап
        db_path = DATABASE_PATH

        # Telegram-канал: проверяем токен только если он реально нужен
        bot_token = None
        if admin_id:
            bot_token_row = await async_query_db(
                "SELECT value FROM settings WHERE key = 'bot_token'", (), one=True
            )
            bot_token = bot_token_row['value'] if bot_token_row else None
            if not bot_token and not s3_enabled:
                # Ни TG, ни S3 — выходим
                logger.warning("[BACKUP] bot_token пуст, S3 выключен — авто-бэкап отменён")
                return

        # Создаем временный файл для ZIP
        temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
        temp_zip_path = temp_zip.name
        temp_zip.close()

        # Ищем .env рядом с БД, либо в корне проекта
        db_dir = os.path.dirname(os.path.abspath(db_path))
        env_candidates = [
            os.path.join(db_dir, '.env'),
            os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'),
        ]
        env_path = next((p for p in env_candidates if os.path.isfile(p)), None)

        # devices.db — только если S3 включён (см. ручной бэкап).
        extra_dbs: list[str] = []
        if s3_enabled:
            dev_db = _auto_backup_devices_db_path()
            if dev_db and os.path.isfile(dev_db):
                extra_dbs.append(dev_db)

        def create_backup_zip():
            build_backup_zip(
                db_path,
                temp_zip_path,
                env_path=env_path,
                extra_db_paths=extra_dbs,
            )

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, create_backup_zip)

        timestamp = now.strftime('%Y-%m-%d_%H-%M-%S')
        zip_filename = f'backup_auto_{timestamp}.zip'

        included = ['БД']
        if extra_dbs:
            included.append('devices.db')
        if env_path:
            included.append('.env')

        results: list[str] = []
        errors: list[str] = []

        # ── 1. Telegram ──────────────────────────────────────────────────────
        if admin_id and bot_token:
            try:
                proxy_url = (app_conf.get('telegram_proxy_url') or '').strip() or None
                bot = make_aiogram_bot(bot_token, proxy_url)
                try:
                    await bot.send_document(
                        int(admin_id),
                        FSInputFile(temp_zip_path, filename=zip_filename),
                        caption=(
                            f'Автоматический бэкап ({", ".join(included)})\n'
                            f'Дата: {now.strftime("%Y-%m-%d %H:%M:%S")} МСК\n'
                            f'Интервал: каждые {interval_hours} ч.'
                        )
                    )
                    results.append('Telegram')
                finally:
                    try:
                        await bot.session.close()
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"[BACKUP] Telegram send failed: {e}", exc_info=True)
                errors.append(f'Telegram: {e}')

        # ── 2. S3 ────────────────────────────────────────────────────────────
        if s3_enabled:
            try:
                s3_key = await upload_file_async(
                    temp_zip_path,
                    endpoint_url=(row.get('s3_endpoint') or '').strip() or None,
                    region_name=(row.get('s3_region') or '').strip() or None,
                    bucket=(row.get('s3_bucket') or '').strip(),
                    access_key=(row.get('s3_access_key') or '').strip(),
                    secret_key=(row.get('s3_secret_key') or '').strip(),
                    prefix=(row.get('s3_prefix') or '').strip(),
                    filename=zip_filename,
                )
                results.append(f'S3 ({s3_key})')
            except S3NotConfigured as e:
                logger.error(f"[BACKUP] S3 misconfigured: {e}")
                errors.append(f'S3: {e}')
            except S3UploadError as e:
                logger.error(f"[BACKUP] S3 upload failed: {e}")
                errors.append(f'S3: {e}')
            except Exception as e:
                logger.error(f"[BACKUP] S3 unknown error: {e}", exc_info=True)
                errors.append(f'S3: {e}')

        # Если хотя бы один канал отработал — апдейтим last_backup, чтобы
        # не дёргать второй раз через 10 минут. Если ВСЁ упало — оставляем
        # старое значение, тогда планировщик через 10 минут попробует снова.
        if results:
            await async_execute_db(
                "UPDATE backup_settings SET last_backup=? WHERE id=?",
                (now.strftime('%Y-%m-%d %H:%M:%S'), row['id'])
            )
            if errors:
                logger.warning(
                    f"[BACKUP] Авто-бэкап частично отправлен: {results}; ошибки: {errors}"
                )
            else:
                logger.info(
                    f"[BACKUP] Авто-бэкап успешно отправлен: {results} (интервал: {interval_hours} ч.)"
                )
        else:
            logger.error(f"[BACKUP] Авто-бэкап не отправлен. Ошибки: {errors}")

        try:
            os.unlink(temp_zip_path)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[BACKUP] Ошибка в do_auto_backup: {e}", exc_info=True)

@admin_bp.route('/news_templates__placeholder__moved')
@login_required
def news_templates_moved_placeholder():
    return redirect(url_for('admin.news_templates_list'))

@admin_bp.route('/news_templates/add__placeholder__moved', methods=['GET', 'POST'])
@login_required
def news_template_add_moved_placeholder():
            return redirect(url_for('admin.news_template_add'))

@admin_bp.route('/news_templates/edit__placeholder__moved/<int:template_id>', methods=['GET', 'POST'])
@login_required
def news_template_edit_moved_placeholder(template_id):
            return redirect(url_for('admin.news_template_edit', template_id=template_id))

@admin_bp.route('/news_templates/delete__placeholder__moved/<int:template_id>', methods=['POST'])
@login_required
def news_template_delete_moved_placeholder(template_id):
    return redirect(url_for('admin.news_template_delete', template_id=template_id))

from web_admin.routes.api import api_bp
from web_admin.routes.payments import attach_payment_routes
attach_payment_routes(admin_bp)
from web_admin.routes.users import attach_user_routes
attach_user_routes(admin_bp, async_query_db, async_execute_db)
from web_admin.routes.tariffs import attach_tariff_routes
attach_tariff_routes(admin_bp, async_query_db, async_execute_db)
from web_admin.routes.settings import attach_settings_routes
attach_settings_routes(admin_bp, async_query_db, async_execute_db)
from web_admin.routes.news import attach_news_routes
attach_news_routes(admin_bp, async_query_db, async_execute_db)
from web_admin.routes.routers_fleet import attach_routers_fleet_routes
attach_routers_fleet_routes(admin_bp, async_query_db, async_execute_db)
from web_admin.routes.catalog_shop import attach_catalog_shop_routes
attach_catalog_shop_routes(admin_bp, async_query_db, async_execute_db)
from web_admin.routes.devices_stock import attach_devices_stock_routes
attach_devices_stock_routes(admin_bp, async_query_db, async_execute_db)
from web_admin.routes.orders_shop import attach_orders_shop_routes
attach_orders_shop_routes(admin_bp, async_query_db, async_execute_db)
from web_admin.routes.tasks import attach_tasks_routes
attach_tasks_routes(admin_bp, async_query_db, async_execute_db)
from web_admin.routes.misc import attach_misc_routes
attach_misc_routes(admin_bp, async_query_db, async_execute_db)
from web_admin.routes.promo import attach_promo_routes
attach_promo_routes(admin_bp, async_query_db, async_execute_db)
from web_admin.routes.promotion import attach_promotion_routes
attach_promotion_routes(admin_bp, async_query_db, async_execute_db)
from web_admin.routes.partners import attach_partners_routes
attach_partners_routes(admin_bp, async_query_db, async_execute_db)
from web_admin.routes.reports import attach_reports_routes
attach_reports_routes(admin_bp, async_query_db, async_execute_db)
from web_admin.routes.remnawave import attach_remnawave_routes
attach_remnawave_routes(admin_bp)
from web_admin.routes.pwa import attach_pwa_routes
attach_pwa_routes(admin_bp)
from web_admin.routes.analytics import attach_analytics_routes
attach_analytics_routes(admin_bp)
from web_admin.routes.push import attach_push_routes
attach_push_routes(admin_bp)
from web_admin.routes.bulk_ops import attach_bulk_ops_routes
attach_bulk_ops_routes(admin_bp)
        
@admin_bp.route('/users__placeholder__moved/<int:telegram_id>/delete', methods=['POST'])
@login_required
def delete_user_moved_placeholder(telegram_id):
    return redirect(url_for('admin.delete_user', telegram_id=telegram_id))

@admin_bp.route('/users__placeholder__moved/<int:telegram_id>/block', methods=['POST'])
@login_required
def block_user_moved_placeholder(telegram_id):
    return redirect(url_for('admin.block_user', telegram_id=telegram_id))

@admin_bp.route('/users__placeholder__moved/<int:telegram_id>/unblock', methods=['POST'])
@login_required
def unblock_user_moved_placeholder(telegram_id):
    return redirect(url_for('admin.unblock_user', telegram_id=telegram_id))

@admin_bp.route('/payments__placeholder__moved')
@login_required
def payments_moved_placeholder():
    return redirect(url_for('admin.payments'))

@admin_bp.route('/payments/clear_pending__placeholder__moved', methods=['POST'])
@login_required
def clear_pending_payments_moved_placeholder():
    return redirect(url_for('admin.clear_pending_payments'))

@admin_bp.route('/api/all_user_ids__placeholder__moved')
@login_required
def api_all_user_ids_moved_placeholder():
    return redirect(url_for('admin.api_all_user_ids'))



@admin_bp.route('/tariffs__placeholder__moved')
@login_required
def tariffs_list_moved_placeholder():
    return redirect(url_for('admin.tariffs_list'))

@admin_bp.route('/tariffs/add__placeholder__moved', methods=['GET', 'POST'])
@login_required
def tariff_add_moved_placeholder():
    return redirect(url_for('admin.tariff_add'))

@admin_bp.route('/tariffs/edit__placeholder__moved/<int:tariff_id>', methods=['GET', 'POST'])
@login_required
def tariff_edit_moved_placeholder(tariff_id):
    return redirect(url_for('admin.tariff_edit', tariff_id=tariff_id))

@admin_bp.route('/tariffs/delete__placeholder__moved/<int:tariff_id>', methods=['POST'])
@login_required
def tariff_delete_moved_placeholder(tariff_id):
    return redirect(url_for('admin.tariff_delete', tariff_id=tariff_id))

@admin_bp.route('/tariffs/toggle__placeholder__moved/<int:tariff_id>', methods=['POST'])
@login_required
def tariff_toggle_moved_placeholder(tariff_id):
    return redirect(url_for('admin.tariff_toggle', tariff_id=tariff_id))

@admin_bp.route('/updates__placeholder__moved')
@login_required
def updates_moved_placeholder():
        return redirect(url_for('admin.updates'))

@admin_bp.route('/download_local_zip__placeholder__moved')
@login_required
def download_local_zip_moved_placeholder():
    return redirect(url_for('admin.download_local_zip'))

@admin_bp.route('/restart_all__placeholder__moved', methods=['POST'])
@login_required
def restart_all_moved_placeholder():
    return redirect(url_for('admin.restart_all'))

@admin_bp.route('/restart_services__placeholder__moved', methods=['POST'])
@login_required
def restart_services_moved_placeholder():
    return redirect(url_for('admin.restart_services'))

@admin_bp.route('/referrals__placeholder__moved')
@login_required
def referrals_moved_placeholder():
    return redirect(url_for('admin.referrals'))

@admin_bp.route('/users__placeholder__moved/<int:telegram_id>/edit_limit_ip', methods=['POST'])
@login_required
def edit_user_limit_ip_moved_placeholder(telegram_id):
    return redirect(url_for('admin.edit_user_limit_ip', telegram_id=telegram_id))

@admin_bp.route('/blocked_users__placeholder__moved')
@login_required
def blocked_users_list_moved_placeholder():
    return redirect(url_for('admin.blocked_users_list'))

@admin_bp.route('/tasks__placeholder__moved')
@login_required
def tasks_list_moved_placeholder():
    return redirect(url_for('admin.tasks_list'))

@admin_bp.route('/tasks/<task_id>__placeholder__moved')
@login_required
def task_details_moved_placeholder(task_id):
    return redirect(url_for('admin.task_details', task_id=task_id))

@admin_bp.route('/settings/subscription__placeholder__moved', methods=['GET', 'POST'])
@login_required
def settings_subscription_moved_placeholder():
        return redirect(url_for('admin.settings_subscription'))

def get_version():
    """Возвращает версию приложения из файла version.txt"""
    try:
        project_root = os.path.dirname(os.path.dirname(__file__))
        # Используем синхронное чтение только для небольшого файла версии
        # Это не блокирует event loop критично, но можно было бы сделать async
        with open(os.path.join(project_root, 'version.txt'), encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return 'unknown'

@app.context_processor
def inject_version():
    """Добавляет версию приложения во все шаблоны"""
    return dict(app_version=get_version())

@app.context_processor
def inject_now():
    """Добавляет текущую дату во все шаблоны"""
    return dict(now=datetime.now())

@app.context_processor
def inject_admin_secret_path():
    """Добавляет секретный префикс во все шаблоны"""
    return dict(admin_secret_path=app.config.get('ADMIN_SECRET_PATH', ''))

@app.context_processor
def inject_project_name():
    """Добавляет project_name во все шаблоны (для шапки/логотипа и др.)."""
    name = app.config.get('PROJECT_NAME', 'Панель')
    return dict(project_name=name)

@app.context_processor
def inject_current_user():
    """Делает current_user доступным в шаблонах (замена flask-login)."""
    return dict(current_user=current_user)

# URL JSON-фида с новостями для админки. Можно переопределить в `settings.news_feed_url`.
# По умолчанию — наш cdn-источник; если в БД пусто, используем дефолт.
_DEFAULT_NEWS_FEED_URL = 'https://update.3xstore.ru/sendpRPoiwKCadDLxRM/send.json'

@app.context_processor
def inject_news_feed_url():
    """Прокидывает в шаблоны URL JSON с новостями.

    Колокольчик в admin-панели рендерится только если URL непустой
    (в `base.html` это уже под `{% if current_user.is_admin %}`).
    """
    try:
        url = (app_conf.get('news_feed_url') or '').strip()
    except Exception:
        url = ''
    return dict(news_feed_url=(url or _DEFAULT_NEWS_FEED_URL))

# --- Jinja фильтры ---
@app.template_filter('msk_datetime')
def msk_datetime_filter(value):
    """Форматирует дату/время в МСК: YYYY.MM.DD HH:MM по MSK."""
    try:
        dt_obj = None
        if value is None:
            return ''
        if isinstance(value, str):
            try:
                dt_obj = datetime.fromisoformat(value)
            except Exception:
                try:
                    dt_obj = datetime.strptime(value[:19], '%Y-%m-%d %H:%M:%S')
                except Exception:
                    return value
        elif isinstance(value, datetime):
            dt_obj = value
        else:
            return str(value)

        # Если наивное время — считаем его UTC
        if dt_obj.tzinfo is None:
            dt_obj = dt_obj.replace(tzinfo=timezone.utc)
        msk = pytz.timezone('Europe/Moscow')
        dt_msk = dt_obj.astimezone(msk)
        return dt_msk.strftime('%Y.%m.%d %H:%M') + ' по MSK'
    except Exception:
        return str(value)

# --- Статика под секретным префиксом ---
@app.route('/<path:secret>/static/<path:filename>')
async def secret_static(secret, filename):
    # Поддержка доступа к статике через секретный префикс
    return await send_from_directory(os.path.join(os.path.dirname(__file__), 'static'), filename)


@app.route('/<path:secret>/news_media/<path:filename>')
async def news_media_file(secret, filename):
    """Раздача preview-файлов медиа-аттачей рассылок (картинки/гифки/видео).
    Секретный префикс уже защищает доступ; дополнительная авторизация не нужна.
    Папка: <project_root>/media/news/uploads/."""
    # Защита от path traversal: filename может содержать '/' но не '..'
    safe = (filename or '').replace('\\', '/').strip('/')
    if '..' in safe.split('/'):
        abort(404)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base_dir = os.path.join(project_root, 'media', 'news', 'uploads')
    abs_path = os.path.abspath(os.path.join(base_dir, safe))
    # Никогда не выходим за пределы base_dir
    if not abs_path.startswith(os.path.abspath(base_dir) + os.sep) and abs_path != os.path.abspath(base_dir):
        abort(404)
    if not os.path.isfile(abs_path):
        abort(404)
    rel = os.path.relpath(abs_path, base_dir)
    return await send_from_directory(base_dir, rel)

@app.route('/<path:secret>/favicon.ico')
async def secret_favicon(secret):
    # favicon по секретному префиксу
    static_dir = os.path.join(os.path.dirname(__file__), 'static')
    for fname, mtype in (('favicon.ico', 'image/vnd.microsoft.icon'), ('favicon.png', 'image/png'), ('favicon.svg', 'image/svg+xml')):
        fpath = os.path.join(static_dir, fname)
        if os.path.exists(fpath):
            return await send_from_directory(static_dir, fname, mimetype=mtype)
    abort(404)

# Глобальная переменная для планировщика (будет инициализирована в before_serving)
_scheduler: Optional[AsyncIOScheduler] = None

# --- Инициализация приложения (выполняется при запуске через hypercorn) ---
@app.before_serving
async def _initialize_app():
    """Инициализация приложения при запуске через hypercorn."""
    global _scheduler

    try:
        from devices.database import init_database as init_devices_db
        await init_devices_db()
    except Exception as _e:
        logger.warning(f"[DEVICES] Не удалось инициализировать БД устройств: {_e}")

    try:
        
        # Получаем секретный путь из БД
        row = await async_query_db("SELECT value FROM settings WHERE key = 'admin_secret_path'", (), one=True)
        admin_secret_path = row['value'] if row and row['value'] else 'admin123'
        
        # Устанавливаем url_prefix для blueprints
        admin_bp.url_prefix = f'/{admin_secret_path}'
        api_bp.url_prefix = f'/{admin_secret_path}/api'
        
        # Делаем доступным в шаблонах
        app.config['ADMIN_SECRET_PATH'] = admin_secret_path
        
        # Регистрируем blueprints (если еще не зарегистрированы)
        if 'api' not in app.blueprints:
            app.register_blueprint(api_bp)
        if 'admin' not in app.blueprints:
            app.register_blueprint(admin_bp)
        
        # Прочитаем project_name и сохраним в config
        try:
            row = await async_query_db("SELECT value FROM settings WHERE key = 'project_name'", (), one=True)
            app.config['PROJECT_NAME'] = row['value'].strip() if row and row.get('value') else 'Панель'
        except Exception:
            app.config['PROJECT_NAME'] = 'Панель'
        
        logger.info(f"[INIT] Веб-админка инициализирована. Секретный путь: /{admin_secret_path}")
        
        # Инициализация планировщика задач (выполняется здесь для всех способов запуска)
        print("[SCHEDULER] Инициализация планировщика задач...")
        logger.info("[SCHEDULER] Инициализация планировщика задач...")
        
        _scheduler = AsyncIOScheduler()
        
        _scheduler.add_job(do_auto_backup, 'interval', minutes=10)
        print("[SCHEDULER] ✅ Авто-бэкап: проверка каждые 10 мин")
        logger.info("[SCHEDULER] ✅ Авто-бэкап: проверка каждые 10 мин")

        # ── PWA push-уведомления: poll новых платежей и шлём пуши ─────────
        try:
            from web_admin.core.push_sender import poll_and_notify_payments

            async def _pwa_push_payments_tick():
                try:
                    await poll_and_notify_payments(lookback_minutes=60)
                except Exception as e:
                    logger.warning(f"[PWA-PUSH] Ошибка polling платежей: {e}")

            _scheduler.add_job(
                _pwa_push_payments_tick,
                'interval',
                seconds=20,
                id='pwa_push_payments',
                replace_existing=True,
                next_run_time=datetime.now() + timedelta(seconds=30),
            )
            print("[SCHEDULER] ✅ PWA push о платежах: проверка каждые 20с")
            logger.info("[SCHEDULER] ✅ PWA push о платежах: проверка каждые 20с")
        except Exception as e:
            logger.warning(f"[SCHEDULER] Не удалось запланировать PWA push polling: {e}")

        # ── Проверка доступности панели Remnawave (health-check) ──
        # 3 попытки раз в 10 мин: 3/3 — тихо; 2/3 — возможна потеря пакетов; ≤1/3 — панель недоступна.
        try:
            await app_conf.load_settings()
        except Exception:
            pass
        _rw_configured = bool(
            (app_conf.get('remnawave_base_url') or '').strip()
            and (app_conf.get('remnawave_api_token') or '').strip()
        )
        _rw_hc_enabled = True
        try:
            _hc_row = await async_query_db(
                "SELECT value FROM settings WHERE key = 'remnawave_health_check_enabled'", (), one=True
            )
            if _hc_row and _hc_row.get('value') is not None:
                _rw_hc_enabled = str(_hc_row['value']).strip() in ('1', 'true', 'yes', 'on')
        except Exception:
            _rw_hc_enabled = True

        if _rw_configured and _rw_hc_enabled:
            async def _remnawave_health_tick():
                try:
                    from remnawave_manager import remnawave_manager_instance
                    ok = 0
                    for _i in range(3):
                        if await remnawave_manager_instance.health_ping(timeout_seconds=6.0):
                            ok += 1
                        if _i < 2:
                            await asyncio.sleep(1.0)
                    if ok >= 3:
                        logger.debug("[RW-HEALTH] Панель Remnawave доступна (3/3)")
                        return
                    if ok == 2:
                        msg = ("⚠️ Remnawave: возможна потеря пакетов\n\n"
                               "Панель ответила 2 из 3 раз — соединение нестабильно. "
                               "Последите за состоянием панели и сети.")
                    else:
                        msg = ("🚨 Remnawave: панель недоступна / плохое соединение\n\n"
                               f"Панель ответила {ok} из 3 раз. Срочно проверьте доступность "
                               "панели Remnawave и сетевое соединение!")
                    # Получатели — администраторы (как в health-check 3X-UI)
                    admin_ids_list = []
                    try:
                        _air = await async_query_db("SELECT value FROM settings WHERE key = 'admin_ids'", (), one=True)
                        _ais = _air['value'].strip() if _air and _air.get('value') else ''
                        if _ais:
                            try:
                                admin_ids_list = json.loads(_ais)
                                if not isinstance(admin_ids_list, list):
                                    admin_ids_list = []
                            except json.JSONDecodeError:
                                admin_ids_list = [int(x.strip()) for x in _ais.split(',') if x.strip().isdigit()]
                        if not admin_ids_list:
                            _bk = await async_query_db("SELECT admin_telegram_id FROM backup_settings LIMIT 1", (), one=True)
                            _raw = (_bk.get('admin_telegram_id') or '').strip() if _bk else ''
                            if _raw.isdigit():
                                admin_ids_list = [int(_raw)]
                    except Exception as _e:
                        logger.warning(f"[RW-HEALTH] не удалось получить admin_ids: {_e}")
                    sent = 0
                    for _aid in admin_ids_list:
                        try:
                            await send_telegram_message(str(_aid), msg)
                            sent += 1
                        except Exception as _e:
                            logger.error(f"[RW-HEALTH] ошибка уведомления админа {_aid}: {_e}")
                    logger.warning(f"[RW-HEALTH] Панель ответила {ok}/3, уведомлено админов: {sent}")
                except Exception as e:
                    logger.warning(f"[RW-HEALTH] Ошибка проверки доступности Remnawave: {e}")

            _scheduler.add_job(
                _remnawave_health_tick,
                'interval',
                minutes=10,
                id='remnawave_health_check',
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now() + timedelta(seconds=30),
            )
            print("[SCHEDULER] ✅ Health-check Remnawave: проверка каждые 10 мин")
            logger.info("[SCHEDULER] ✅ Health-check Remnawave: проверка каждые 10 мин")
        else:
            logger.info("[SCHEDULER] Health-check Remnawave: пропуск (не настроен API или выключено)")

        print("[SCHEDULER] Запуск планировщика задач...")
        logger.info("[SCHEDULER] Запуск планировщика задач...")
        _scheduler.start()
        print("[SCHEDULER] ✅ Планировщик задач запущен")
        logger.info("[SCHEDULER] ✅ Планировщик задач запущен")
        
    except Exception as e:
        logger.error(f"[INIT] Ошибка инициализации приложения: {e}", exc_info=True)

# --- Запуск приложения ---
if __name__ == '__main__':
    async def __main_async():
        print("="*50)
        print("Запуск веб-админки...")

        # Инициализация по умолчанию перенесена и выполняется миграциями/синим экраном настроек. Убрана из run.py
        
        # Получаем порт из БД или используем значение по умолчанию
        # Используем асинхронные обёртки
        port_row = await async_query_db("SELECT value FROM settings WHERE key = 'web_admin_port'", (), one=True)
        port = int(port_row['value']) if port_row and port_row['value'].isdigit() else 8181

        # Получаем секретный путь из БД
        row = await async_query_db("SELECT value FROM settings WHERE key = 'admin_secret_path'", (), one=True)
        admin_secret_path = row['value'] if row and row['value'] else 'admin123'
        admin_bp.url_prefix = f'/{admin_secret_path}'
        # Регистрируем API под тем же секретным префиксом
        api_bp.url_prefix = f'/{admin_secret_path}/api'
        # Делаем доступным в шаблонах
        app.config['ADMIN_SECRET_PATH'] = admin_secret_path
        app.register_blueprint(api_bp)
        app.register_blueprint(admin_bp)

        # Прочитаем project_name и сохраним в config, чтобы не дергать БД из шаблонов
        try:
            row = await async_query_db("SELECT value FROM settings WHERE key = 'project_name'", (), one=True)
            app.config['PROJECT_NAME'] = row['value'].strip() if row and row.get('value') else 'Панель'
        except Exception:
            app.config['PROJECT_NAME'] = 'Панель'

        # Миграции, завязанные на БД
        try:
            # Добавим колонку comment в partner_accruals, если её нет
            cols = await async_query_db("PRAGMA table_info(partner_accruals)", ())
            col_names = [c['name'] for c in cols] if cols else []
            if 'comment' not in col_names:
                try:
                    await async_execute_db("ALTER TABLE partner_accruals ADD COLUMN comment TEXT", ())
                except Exception:
                    pass
            # Индексы для ускорения партнёрки
            try:
                await async_execute_db("CREATE INDEX IF NOT EXISTS idx_users_partner_ref_code ON users(partner_ref_code)", ())
            except Exception:
                pass
            try:
                await async_execute_db("CREATE INDEX IF NOT EXISTS idx_users_invited_by ON users(invited_by)", ())
            except Exception:
                pass
            try:
                await async_execute_db("CREATE INDEX IF NOT EXISTS idx_users_invited_by_method ON users(invited_by_method)", ())
            except Exception:
                pass
            try:
                await async_execute_db("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)", ())
            except Exception:
                pass
            # Создаем таблицу cleanup_tasks, если её нет
            try:
                await async_execute_db(
                    """
                    CREATE TABLE IF NOT EXISTS cleanup_tasks (
                        task_id TEXT PRIMARY KEY,
                        server_count INTEGER DEFAULT 0,
                        status TEXT DEFAULT 'running',
                        success_count INTEGER DEFAULT 0,
                        failed_count INTEGER DEFAULT 0,
                        server_results TEXT,
                        error_details TEXT,
                        created_at TEXT,
                        completed_at TEXT
                    )
                    """,
                    ()
                )
            except Exception:
                pass
            try:
                await async_execute_db("CREATE INDEX IF NOT EXISTS idx_payments_telegram_id ON payments(telegram_id)", ())
            except Exception:
                pass
            # Ensure table for per-user enabled servers
            try:
                await async_execute_db(
                    """
                    CREATE TABLE IF NOT EXISTS user_enabled_servers (
                        telegram_id INTEGER NOT NULL,
                        server_id INTEGER NOT NULL,
                        PRIMARY KEY (telegram_id, server_id)
                    )
                    """
                , ())
                try:
                    await async_execute_db("CREATE INDEX IF NOT EXISTS idx_ues_user ON user_enabled_servers(telegram_id)", ())
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"[BOOT] ensure user_enabled_servers failed: {e}")
        except Exception as e:
            logger.warning(f"[BOOT] migrate_user_vless_overrides failed: {e}")

        print(f"URL: http://127.0.0.1:{port}")
        print("Для остановки нажмите Ctrl+C")
        print("="*50)
        print("[INFO] Планировщик задач будет инициализирован в @app.before_serving")
        logger.info("[INFO] Планировщик задач будет инициализирован в @app.before_serving")

        # Запуск ASGI сервера (Hypercorn) для Quart
        __cfg = HypercornConfig()
        __cfg.bind = [f"127.0.0.1:{port}"]
        __cfg.keep_alive_timeout = 30  # Таймаут keep-alive соединений
        await hypercorn_serve(app, __cfg)

    asyncio.run(__main_async())