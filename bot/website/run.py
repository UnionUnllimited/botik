"""
website — Публичный сайт с личным кабинетом.
"""
# ─── Стандартная библиотека ──────────────────────────────────────────────────
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import time
from collections import defaultdict, OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

# ─── Пути (нужны до импорта сторонних/локальных модулей) ─────────────────────
script_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(script_dir)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

# ─── Сторонние библиотеки ────────────────────────────────────────────────────
import aiosqlite
import httpx
import pytz
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ─── Загрузка .env (после sys.path, до локальных импортов) ───────────────────
# Сначала корень проекта, потом website/ (перекрывает корень)
load_dotenv(os.path.join(parent_dir, '.env'))
load_dotenv(os.path.join(script_dir, '.env'), override=True)

# ─── Локальные модули ────────────────────────────────────────────────────────
import db_helpers
from db_helpers import (
    get_web_user_by_email,
    create_web_user,
    save_web_auth_token,
    consume_web_auth_token,
    peek_web_auth_token,
    cleanup_web_auth_tokens,
    check_code_attempts,
    check_send_attempts,
    increment_code_attempt,
)
from email_sender import send_email, code_email_html, subscription_activated_html
from email_domain_policy import (
    SETTING_KEY as WEBSITE_CABINET_SETTING_KEY,
    config_from_setting_value,
    is_email_domain_allowed,
    EMAIL_DOMAIN_REJECT_MESSAGE,
)
from src.pay import (
    create_yookassa_payment_shared,
    create_platega_payment_shared,
    create_yoomoney_quickpay,
    create_wata_payment_shared,
    create_wata_payment_traffic_renewal,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# ─── БД ──────────────────────────────────────────────────────────────────────
DATABASE_NAME = os.path.join(parent_dir, 'vpn_bot.db')

async def db_get(query: str, params: tuple = ()) -> list:
    async with aiosqlite.connect(f'file:{DATABASE_NAME}?mode=ro', uri=True, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def db_one(query: str, params: tuple = ()):
    rows = await db_get(query, params)
    return rows[0] if rows else None

async def get_setting(key: str, default: str = '') -> str:
    row = await db_one("SELECT value FROM settings WHERE key = ?", (key,))
    return (row['value'] if row and row.get('value') else default) or default


async def get_website_cabinet_config() -> dict:
    raw = await get_setting(WEBSITE_CABINET_SETTING_KEY, '')
    return config_from_setting_value(raw)


async def email_registered_in_db(email: str) -> bool:
    row = await db_one(
        "SELECT 1 FROM users WHERE LOWER(email) = LOWER(?) LIMIT 1",
        (email,),
    )
    return bool(row)


# Удаление emoji-символов из строки (например, для кнопки Platega, рядом с которой уже есть SVG-иконка).
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE,
)


def strip_emoji(text: str) -> str:
    if not text:
        return text
    cleaned = _EMOJI_RE.sub('', text)
    return re.sub(r'\s+', ' ', cleaned).strip()


# ─── SMTP (из .env) ───────────────────────────────────────────────────────────
def smtp_settings() -> dict:
    return {
        'host':     os.getenv('SMTP_HOST', ''),
        'port':     int(os.getenv('SMTP_PORT', '465')),
        'user':     os.getenv('SMTP_USER', ''),
        'password': os.getenv('SMTP_PASSWORD', ''),
        'from':     os.getenv('SMTP_FROM', ''),
    }

# ─── Платёжные реквизиты для сайта ───────────────────────────────────────────
# Если в .env заданы WEBSITE_YOOKASSA_* / WEBSITE_PLATEGA_* / WEBSITE_YOOMONEY_* — используем их.
# Иначе fallback на общие настройки из БД (те же что у бота).

async def _get_yookassa_creds() -> tuple:
    shop_id = os.getenv('WEBSITE_YOOKASSA_SHOP_ID', '').strip()
    secret  = os.getenv('WEBSITE_YOOKASSA_SECRET_KEY', '').strip()
    if shop_id and secret:
        return shop_id, secret
    return await get_setting('yookassa_shop_id'), await get_setting('yookassa_secret_key')

async def _get_platega_creds() -> tuple:
    merchant_id = os.getenv('WEBSITE_PLATEGA_MERCHANT_ID', '').strip()
    api_secret  = os.getenv('WEBSITE_PLATEGA_API_SECRET', '').strip()
    if merchant_id and api_secret:
        return merchant_id, api_secret
    return await get_setting('platega_merchant_id'), await get_setting('platega_api_secret')

async def _get_yoomoney_creds() -> tuple:
    """Тройка YooMoney как в админке: account, token, notification_secret.

    Для каждого поля: WEBSITE_YOOMONEY_* в .env переопределяет значение из БД
    (``yoomoney_account``, ``yoomoney_token``, ``yoomoney_notification_secret``).

    Обязательны: account (получатель в quickpay-URL) и notification_secret
    (проверка подписи входящих webhook'ов). token используется только как
    API-fallback (operation-history) — без него платежи и webhook'и работают.
    """
    env_acc = os.getenv('WEBSITE_YOOMONEY_ACCOUNT', '').strip()
    env_tok = os.getenv('WEBSITE_YOOMONEY_TOKEN', '').strip()
    env_sec = os.getenv('WEBSITE_YOOMONEY_NOTIFICATION_SECRET', '').strip()
    db_acc = (await get_setting('yoomoney_account')) or ''
    db_tok = (await get_setting('yoomoney_token')) or ''
    db_sec = (await get_setting('yoomoney_notification_secret')) or ''
    return (env_acc or db_acc, env_tok or db_tok, env_sec or db_sec)

async def _get_wata_creds() -> tuple:
    """Wata: ``access_token`` (обязателен) и ``terminal_public_id`` (опционально).

    Сначала смотрим WEBSITE_WATA_*, затем общие WATA_* из ``.env`` (так же как
    в боте), затем БД-настройки ``wata_access_token`` / ``wata_terminal_public_id``.
    """
    env_tok = (
        os.getenv('WEBSITE_WATA_ACCESS_TOKEN', '').strip()
        or os.getenv('WATA_ACCESS_TOKEN', '').strip()
    )
    env_pid = (
        os.getenv('WEBSITE_WATA_TERMINAL_PUBLIC_ID', '').strip()
        or os.getenv('WATA_TERMINAL_PUBLIC_ID', '').strip()
    )
    tok = env_tok or (await get_setting('wata_access_token')) or ''
    pid = env_pid or (await get_setting('wata_terminal_public_id')) or ''
    return tok, pid

# ─────────────────────────────────────────────────────────────────────────────

async def send_code_email(email: str, code: str) -> bool:
    s = smtp_settings()
    project_name = await get_setting('project_name', 'VPN')
    return await send_email(
        to=email,
        subject=f"Код входа: {code} — {project_name}",
        html=code_email_html(code, project_name),
        smtp_from=s['from'],
    )

async def send_activation_email(email: str, sub_url: str, expiry: str) -> bool:
    s = smtp_settings()
    project_name = await get_setting('project_name', 'VPN')
    # Шифруем ссылку через happ.crypto только в режиме happcrypto
    happ_key = None
    if sub_url and _DEF_SUB_MODE == 'happcrypto':
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post("https://crypto.happ.su/api-v2.php", json={"url": sub_url})
                if resp.status_code == 200:
                    data = resp.json()
                    happ_key = data.get("encrypted_link") or data.get("url")
        except Exception:
            pass
    return await send_email(
        to=email,
        subject=f"✅ Подписка {project_name} активирована",
        html=subscription_activated_html(sub_url, expiry, project_name, happ_key=happ_key),
        smtp_from=s['from'],
    )


# ─── Сессии (простые, на основе signed cookie) ───────────────────────────────
_session_secret_env = os.getenv('WEBSITE_SESSION_SECRET', '')
SESSION_SECRET = _session_secret_env if _session_secret_env else secrets.token_hex(32)

def _sign(value: str) -> str:
    import hmac, hashlib
    sig = hmac.new(SESSION_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{sig}"

def _verify(signed: str) -> str | None:
    import hmac, hashlib
    if '.' not in signed:
        return None
    value, sig = signed.rsplit('.', 1)
    expected = hmac.new(SESSION_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()
    return value if hmac.compare_digest(sig, expected) else None

def get_session_email(request: Request) -> str | None:
    cookie = request.cookies.get('ws')
    return _verify(cookie) if cookie else None

def set_session(response: Response, email: str):
    response.set_cookie('ws', _sign(email), max_age=30*24*3600, httponly=True, samesite='lax')

def clear_session(response: Response):
    response.delete_cookie('ws')


# ─── Telegram Login Widget ────────────────────────────────────────────────────
_BOT_TOKEN    = ''   # заполняется при старте в lifespan из БД или .env
_BOT_USERNAME = os.getenv('BOT_USERNAME', '').strip()  # без @, напр. myvpnbot

# ─── Feature flags ────────────────────────────────────────────────────────────
# WEBSITE_DEVICES_ENABLED=1 — включить раздел "Мои устройства" в кабинете
_DEVICES_ENABLED = os.getenv('WEBSITE_DEVICES_ENABLED', '0').strip() == '1'

# Схема deep-link для кнопки «Открыть в приложении» в кабинете (только subscription-режим)
# По умолчанию happ://add/  Примеры: incy://add/  v2raytun://import/
_ADD_LINK = os.getenv('ADD_LINK', 'happ://add/')

# Google OAuth — включается только если заданы оба ключа
_GOOGLE_CLIENT_ID     = os.getenv('GOOGLE_CLIENT_ID', '').strip()
_GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET', '').strip()
_GOOGLE_ENABLED       = bool(_GOOGLE_CLIENT_ID and _GOOGLE_CLIENT_SECRET)

# Yandex OAuth — включается только если заданы оба ключа
_YANDEX_CLIENT_ID     = os.getenv('YANDEX_CLIENT_ID', '').strip()
_YANDEX_CLIENT_SECRET = os.getenv('YANDEX_CLIENT_SECRET', '').strip()
_YANDEX_ENABLED       = bool(_YANDEX_CLIENT_ID and _YANDEX_CLIENT_SECRET)

def verify_telegram_auth(data: dict) -> bool:
    """Проверяет подпись от Telegram Login Widget. Возвращает False при любой ошибке."""
    if not _BOT_TOKEN:
        return False
    received_hash = data.get('hash', '')
    if not received_hash:
        return False
    # auth_date не старше 1 часа
    try:
        if time.time() - int(data.get('auth_date', 0)) > 3600:
            return False
    except (ValueError, TypeError):
        return False
    # Строка для проверки: key=value\n... (отсортировано, без hash)
    check_fields = {k: v for k, v in data.items() if k != 'hash'}
    check_string = '\n'.join(f'{k}={v}' for k, v in sorted(check_fields.items()))
    secret_key = hashlib.sha256(_BOT_TOKEN.encode()).digest()
    computed = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, received_hash)


# ─── App ─────────────────────────────────────────────────────────────────────
def _build_tailwind():
    """Собирает Tailwind CSS при старте. Не блокирует запуск если не установлен."""
    import subprocess, sys
    try:
        # Ищем tailwindcss рядом с текущим python (в том же venv/bin)
        tw = os.path.join(os.path.dirname(sys.executable), 'tailwindcss')
        input_css  = os.path.join(script_dir, 'input.css')
        output_css = os.path.join(script_dir, 'static', 'style.css')
        if not os.path.exists(input_css):
            logging.warning('[TAILWIND] input.css не найден, пропускаем сборку')
            return
        result = subprocess.run(
            [tw, '-i', input_css, '-o', output_css, '--minify'],
            capture_output=True, text=True, timeout=60,
            cwd=script_dir  # tailwind должен найти tailwind.config.js в папке website/
        )
        if result.returncode == 0:
            css_size = os.path.getsize(output_css) if os.path.exists(output_css) else 0
            logging.info(f'[TAILWIND] style.css успешно собран ({css_size // 1024} KB)')
        else:
            logging.error(f'[TAILWIND] Ошибка сборки: {result.stderr[:300]}')
    except FileNotFoundError:
        logging.warning('[TAILWIND] tailwindcss не найден в venv — пропускаем (pip install pytailwindcss)')
    except Exception as e:
        logging.error(f'[TAILWIND] Неожиданная ошибка: {e}')

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _BOT_TOKEN
    _build_tailwind()
    await db_helpers.init_db()
    # Загружаем токен бота: сначала .env, потом БД
    _BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip() or (await get_setting('bot_token', ''))
    if _BOT_TOKEN:
        logging.info('[TG-AUTH] BOT_TOKEN загружен')
    else:
        logging.warning('[TG-AUTH] BOT_TOKEN не найден — вход через Telegram недоступен')
    yield

app = FastAPI(title="VPN Website", docs_url=None, redoc_url=None, lifespan=lifespan)

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    # API-роуты и JSON-запросы — отдаём JSON
    if request.url.path.startswith('/api/') or 'application/json' in request.headers.get('accept', ''):
        return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
    try:
        project_name = await get_setting('project_name', 'VPN')
    except Exception:
        project_name = 'VPN'
    try:
        bot_username = await get_setting('bot_username', '')
    except Exception:
        bot_username = ''
    try:
        support_link = await get_setting('support_link', '')
    except Exception:
        support_link = ''
    try:
        email = get_session_email(request) or ''
    except Exception:
        email = ''
    error_titles = {
        404: 'Страница не найдена',
        403: 'Доступ запрещён',
        500: 'Ошибка сервера',
    }
    error_messages = {
        404: 'Такой страницы не существует. Возможно, ссылка устарела или была удалена.',
        403: 'У вас нет доступа к этой странице.',
        500: 'Что-то пошло не так. Попробуйте позже.',
    }
    try:
        return templates.TemplateResponse(
            request=request,
            name='error.html',
            context={
                'project_name': project_name,
                'bot_username': bot_username,
                'email': email,
                'is_authenticated': bool(email),
                'support_link': support_link,
                'status_code': exc.status_code,
                'error_title': error_titles.get(exc.status_code, f'Ошибка {exc.status_code}'),
                'error_message': error_messages.get(exc.status_code, exc.detail or ''),
            },
            status_code=exc.status_code,
        )
    except Exception as e:
        logging.error(f'[error_handler] TemplateResponse failed: {e}')
        return HTMLResponse(
            f'<h1>{exc.status_code} {error_titles.get(exc.status_code, "Ошибка")}</h1>'
            f'<p>{error_messages.get(exc.status_code, exc.detail or "")}</p>',
            status_code=exc.status_code,
        )

# Убираем заголовок server: uvicorn
@app.middleware("http")
async def remove_server_header(request: Request, call_next):
    response = await call_next(request)
    if "server" in response.headers:
        del response.headers["server"]
    response.headers["x-content-type-options"] = "nosniff"
    response.headers["x-frame-options"] = "DENY"
    return response
app.mount("/static", StaticFiles(directory=os.path.join(script_dir, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(script_dir, "templates"))

# Глобальные переменные для всех шаблонов (из env)
_default_theme = os.getenv('DEFAULT_THEME', 'light').strip().lower()
if _default_theme not in ('light', 'dark'):
    _default_theme = 'light'

# Режим отдачи ключа подписки: 'happcrypto' (по умолчанию) или 'subscription'
_DEF_SUB_MODE = os.getenv('DEF_SUB_MODE', 'happcrypto').strip().lower()
if _DEF_SUB_MODE not in ('happcrypto', 'subscription'):
    _DEF_SUB_MODE = 'happcrypto'

# Пробный период для новых регистраций через сайт (0 = выключено)
_TRIAL_DAYS = int(os.getenv('TRIAL_DAYS', '0') or '0')
_BOT_INTERNAL_URL = os.getenv('BOT_INTERNAL_URL', 'http://127.0.0.1:8081').rstrip('/')
_MOSCOW_TZ = pytz.timezone('Europe/Moscow')


def format_iso_msk(iso_value: str | None, fmt: str = '%d.%m.%Y') -> str:
    """Парсит ISO UTC (или naive UTC) и форматирует в московском времени."""
    if not iso_value:
        return '—'
    raw = str(iso_value).strip()
    if not raw:
        return '—'
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_MOSCOW_TZ).strftime(fmt)
    except Exception:
        return raw[:10] if len(raw) >= 10 else raw


templates.env.globals.update({
    'privacy_url':    os.getenv('PRIVACY_URL', ''),
    'terms_url':      os.getenv('TERMS_URL', ''),
    'support_url':    os.getenv('SUPPORT_SITE', '') or os.getenv('SUPPORT_URL', ''),
    'default_theme':  _default_theme,
    'def_sub_mode':   _DEF_SUB_MODE,
})
templates.env.filters['format_msk'] = format_iso_msk


def tr(request: Request, name: str, status_code: int = 200, **kwargs) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name=name, context=kwargs, status_code=status_code
    )


async def get_user_from_session(email: str) -> dict | None:
    """Возвращает запись из users по email-сессии или tg:ID-сессии."""
    if not email:
        return None
    if email.startswith('tg:'):
        try:
            tg_id = int(email[3:])
            return await db_one("SELECT * FROM users WHERE telegram_id = ?", (tg_id,))
        except (ValueError, Exception):
            return None
    user = await get_web_user_by_email(email)
    if not user:
        # Пользователь бота с привязанным email — ищем по email в таблице users
        user = await db_one("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email,))
    return user


# ─── Маршруты ─────────────────────────────────────────────────────────────────

@app.get("/manifest.json")
async def manifest():
    project_name = await get_setting('project_name', 'VPN')
    return JSONResponse({
        "name": project_name,
        "short_name": project_name,
        "description": f"Личный кабинет {project_name}",
        "start_url": "/cabinet",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#ffffff",
        "theme_color": "#000000",
        "icons": [
            {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
            {"src": "/static/icon.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }, headers={"Cache-Control": "public, max-age=3600"})

@app.get("/sw.js")
async def service_worker():
    sw_code = """
const CACHE = 'vpn-v1';
const PRECACHE = ['/static/qrcode.min.js'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(clients.claim());
});
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request).then(res => {
      const clone = res.clone();
      caches.open(CACHE).then(c => c.put(e.request, clone));
      return res;
    })));
  }
});
"""
    return Response(sw_code.strip(), media_type="application/javascript",
                    headers={"Cache-Control": "no-cache, no-store"})

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    project_name = await get_setting('project_name', 'VPN')
    app_ios         = await get_setting('app_link_ios')
    app_ios_global  = await get_setting('app_link_ios_global', '')
    app_android     = await get_setting('app_link_android')
    app_android_apk = await get_setting('app_link_android_apk', '')
    bot_username = await get_setting('bot_username')
    support_link = await get_setting('support_link', '')
    email = get_session_email(request)
    resp = tr(request, "index.html",
        project_name=project_name,
        app_ios=app_ios,
        app_ios_global=app_ios_global,
        app_android=app_android,
        app_android_apk=app_android_apk,
        bot_username=bot_username,
        support_link=support_link,
        email=email,
    )
    # Сохраняем реферальный код из ?ref= в cookie на 30 дней
    # Поддерживаем два формата: ?ref=12345678 (telegram_id) и ?ref=par_ABCDEF (партнёрский код)
    ref = request.query_params.get('ref', '').strip()[:64]  # не более 64 символов
    if ref and not request.cookies.get('ref'):
        if (ref.isdigit() and len(ref) <= 20) or (ref.startswith('par_') and 4 < len(ref) <= 36):
            resp.set_cookie('ref', ref, max_age=30*24*3600, httponly=True, samesite='lax')
    return resp



@app.get("/setup", response_class=HTMLResponse)
async def setup(request: Request):
    project_name = await get_setting('project_name', 'VPN')
    app_ios         = await get_setting('app_link_ios')
    app_ios_global  = await get_setting('app_link_ios_global', '')
    app_android     = await get_setting('app_link_android')
    app_android_apk = await get_setting('app_link_android_apk', '')
    app_windows     = await get_setting('app_link_windows', '')
    support_link = await get_setting('support_link', '')
    email = get_session_email(request)
    return tr(request, "setup.html",
        project_name=project_name,
        app_ios=app_ios, app_ios_global=app_ios_global,
        app_android=app_android, app_android_apk=app_android_apk,
        app_windows=app_windows, support_link=support_link, email=email
    )


# ── Документы ────────────────────────────────────────────────────────────────



# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request):
    session_email = get_session_email(request)
    project_name = await get_setting('project_name', 'VPN')
    if session_email:
        return tr(request, "auth.html", project_name=project_name,
                  already_auth=True, bot_username=_BOT_USERNAME,
                  google_enabled=_GOOGLE_ENABLED, yandex_enabled=_YANDEX_ENABLED)
    captcha_site_key = os.getenv('YANDEX_CAPTCHA_SITE_KEY', '')
    return tr(request, "auth.html", project_name=project_name, sent=False,
              captcha_site_key=captcha_site_key, bot_username=_BOT_USERNAME,
              google_enabled=_GOOGLE_ENABLED, yandex_enabled=_YANDEX_ENABLED)


@app.post("/auth/send", response_class=HTMLResponse)
async def auth_send(request: Request):
    form = await request.form()
    email = (form.get('email') or '').strip().lower()
    project_name = await get_setting('project_name', 'VPN')
    captcha_site_key = os.getenv('YANDEX_CAPTCHA_SITE_KEY', '')
    captcha_secret   = os.getenv('YANDEX_CAPTCHA_SECRET_KEY', '')

    # ── Проверка Yandex SmartCaptcha (если ключи заданы) ──────────────────────
    if captcha_secret:
        token = (form.get('smart-token') or '').strip()
        client_ip = (
            request.headers.get('x-real-ip') or
            request.headers.get('x-forwarded-for', '').split(',')[0].strip() or
            (request.client.host if request.client else '')
        )
        captcha_ok = False
        captcha_error = "Пожалуйста, пройдите проверку на робота."

        if token:
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
                ) as hc:
                    r = await hc.get(
                        'https://smartcaptcha.yandexcloud.net/validate',
                        params={'secret': captcha_secret, 'token': token, 'ip': client_ip}
                    )
                    if r.status_code == 200:
                        captcha_ok = r.json().get('status') == 'ok'
                    else:
                        logging.warning(f"[CAPTCHA] Яндекс вернул {r.status_code}")
            except httpx.TimeoutException:
                logging.warning("[CAPTCHA] Таймаут запроса к Яндексу")
                captcha_error = "Сервис проверки временно недоступен. Попробуйте через минуту."
            except Exception as e:
                logging.warning(f"[CAPTCHA] Ошибка: {e}")
                captcha_error = "Сервис проверки временно недоступен. Попробуйте через минуту."
        # Fail-closed: при любой ошибке или отсутствии токена — блокируем
        if not captcha_ok:
            return tr(request, "auth.html", project_name=project_name, step='email',
                captcha_site_key=captcha_site_key, error=captcha_error,
                google_enabled=_GOOGLE_ENABLED, yandex_enabled=_YANDEX_ENABLED,
                bot_username=_BOT_USERNAME)
    # ──────────────────────────────────────────────────────────────────────────

    if not email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return tr(request, "auth.html", project_name=project_name, step='email',
            captcha_site_key=captcha_site_key, error="Введите корректный email",
            google_enabled=_GOOGLE_ENABLED, yandex_enabled=_YANDEX_ENABLED,
            bot_username=_BOT_USERNAME)

    admin_email = os.getenv('WEBSITE_ADMIN_EMAIL', '').strip().lower()
    if not (admin_email and email == admin_email):
        wc_cfg = await get_website_cabinet_config()
        if not is_email_domain_allowed(
            email, wc_cfg, user_already_exists=await email_registered_in_db(email),
        ):
            return tr(request, "auth.html", project_name=project_name, step='email',
                captcha_site_key=captcha_site_key, error=EMAIL_DOMAIN_REJECT_MESSAGE,
                google_enabled=_GOOGLE_ENABLED, yandex_enabled=_YANDEX_ENABLED,
                bot_username=_BOT_USERNAME)

    # Лимит отправок кода: не более 3 за 15 минут на один email
    if not await check_send_attempts(email, max_sends=3):
        return tr(request, "auth.html", project_name=project_name, step='email',
            captcha_site_key=captcha_site_key,
            error="Слишком много запросов кода для этого адреса. Подождите 15 минут.",
            google_enabled=_GOOGLE_ENABLED, yandex_enabled=_YANDEX_ENABLED,
            bot_username=_BOT_USERNAME)

    user = await get_web_user_by_email(email)

    # Проверка блокировки до отправки кода
    if user:
        _check = await db_one("SELECT is_blocked FROM users WHERE telegram_id = ?", (user['telegram_id'],))
        if _check and int(_check.get('is_blocked') or 0) == 1:
            return tr(request, "auth.html", project_name=project_name, step='email',
                captcha_site_key=captcha_site_key, error="Доступ ограничен. Обратитесь в поддержку.",
                google_enabled=_GOOGLE_ENABLED, yandex_enabled=_YANDEX_ENABLED,
                bot_username=_BOT_USERNAME)

    # Пользователь создаётся только после подтверждения кода (в verify-code)
    # Реф-cookie сохраняем в токен — передадим при верификации
    ref_cookie = request.cookies.get('ref', '').strip()[:64]

    # Секретный email: вход без кода
    if admin_email and email == admin_email:
        response = RedirectResponse("/cabinet", status_code=302)
        set_session(response, email)
        return response

    # Генерируем 6-значный код (15 минут), сохраняем ref_cookie для использования при верификации
    code = f"{secrets.randbelow(1000000):06d}"
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    await save_web_auth_token(email, code, expires_at, ref_cookie=ref_cookie)
    await cleanup_web_auth_tokens()

    async def _send():
        try:
            await asyncio.wait_for(send_code_email(email, code), timeout=20)
        except Exception as e:
            logging.error(f"[AUTH] Ошибка отправки кода: {e}")
    asyncio.create_task(_send())

    return tr(request, "auth.html", project_name=project_name, step='code', email=email)


@app.post("/auth/verify-code", response_class=HTMLResponse)
async def auth_verify_code(request: Request):
    form = await request.form()
    email = (form.get('email') or '').strip().lower()
    code  = (form.get('code') or '').strip()
    project_name = await get_setting('project_name', 'VPN')

    # Проверяем лимит попыток (5 за 15 минут)
    if not await check_code_attempts(email, max_attempts=5):
        return tr(request, "auth.html", project_name=project_name, step='code', email=email,
            error="Слишком много неверных попыток. Запросите новый код через 15 минут.")

    result = await consume_web_auth_token(code)
    if not result or result.get('email') != email:
        await increment_code_attempt(email)
        return tr(request, "auth.html", project_name=project_name, step='code', email=email,
            error="Неверный или истёкший код. Попробуйте ещё раз.")

    # Код верный — создаём пользователя если его ещё нет (защита от мусора в БД)
    user = await get_web_user_by_email(email)
    if not user:
        if await email_registered_in_db(email):
            user = await db_one("SELECT * FROM users WHERE LOWER(email) = LOWER(?) LIMIT 1", (email,))
        else:
            wc_cfg = await get_website_cabinet_config()
            if not is_email_domain_allowed(email, wc_cfg, user_already_exists=False):
                return tr(request, "auth.html", project_name=project_name, step='code', email=email,
                    error=EMAIL_DOMAIN_REJECT_MESSAGE)
    if not user:
        new_uid = await create_web_user(email)
        # Привязываем реферера из сохранённого ref_cookie
        ref_cookie = result.get('ref_cookie', '').strip()[:64]
        if ref_cookie:
            try:
                if ref_cookie.isdigit() and len(ref_cookie) <= 20:
                    ref_uid = int(ref_cookie)
                    if ref_uid != new_uid:
                        referrer = await db_helpers.get_user(ref_uid)
                        if referrer:
                            await db_helpers.set_invited_by_with_method(new_uid, ref_uid, 'referral')
                            logging.info(f"[REF] {new_uid} приглашён {ref_uid} method=referral")
                elif ref_cookie.startswith('par_') and 4 < len(ref_cookie) <= 36:
                    partner_code = ref_cookie[4:]
                    partner = await db_one(
                        "SELECT telegram_id FROM users WHERE partner_ref_code = ?", (partner_code,)
                    )
                    if partner and partner['telegram_id'] != new_uid:
                        await db_helpers.set_invited_by_with_method(new_uid, partner['telegram_id'], 'partner')
                        logging.info(f"[REF] {new_uid} → партнёр {partner['telegram_id']} ✓")
            except Exception as e:
                logging.warning(f"[REF] Ошибка привязки реферала: {e}", exc_info=True)
        # Пробный период: отправляем запрос боту по loopback
        _actual_trial_days = 0
        if _TRIAL_DAYS > 0:
            try:
                async with httpx.AsyncClient(timeout=10) as _hc:
                    _tr = await _hc.post(
                        f'{_BOT_INTERNAL_URL}/api/grant-trial',
                        json={'user_id': new_uid},
                    )
                if _tr.status_code == 200:
                    _actual_trial_days = _tr.json().get('trial_days', _TRIAL_DAYS)
                    logging.info(f"[TRIAL] Пробный период выдан user={new_uid}, дней={_actual_trial_days}")
                else:
                    logging.warning(f"[TRIAL] Бот вернул {_tr.status_code}: {_tr.text[:200]}")
            except Exception as _te:
                logging.warning(f"[TRIAL] Не удалось запросить триал у бота: {_te}")

    _show_welcome = _TRIAL_DAYS > 0 and not user and _actual_trial_days > 0
    cabinet_url = f"/cabinet?welcome=1&trial_days={_actual_trial_days}" if _show_welcome else "/cabinet"
    response = RedirectResponse(cabinet_url, status_code=302)
    set_session(response, email)
    return response


@app.get("/auth/magic/{token}", response_class=HTMLResponse)
async def auth_magic_link(token: str, request: Request):
    """
    Вход по magic-link из Telegram бота.

    НЕ потребляем токен (peek вместо consume) — оставляем ему жить 10 минут,
    чтобы пользователь мог нажать «Открыть в браузере» из in-app Telegram
    и продолжить сессию в Safari/Chrome без повторного входа. Внешний браузер
    получит /cabinet?m=<token>, увидит отсутствующую cookie и авторизуется
    заново через тот же токен (см. ветку с `?m=` в `cabinet`).
    """
    token_info = await peek_web_auth_token(token)
    if not token_info:
        return RedirectResponse("/auth")
    email = (token_info.get('email') or '').strip()
    if not email:
        return RedirectResponse("/auth")

    # Проверяем блокировку
    user = await get_web_user_by_email(email)
    if user:
        _check = await db_one("SELECT is_blocked FROM users WHERE telegram_id = ?", (user['telegram_id'],))
        if _check and int(_check.get('is_blocked') or 0) == 1:
            return RedirectResponse("/auth")

    # Редирект в кабинет с токеном в URL — Telegram передаст этот URL
    # в Safari/Chrome при «Открыть в браузере», и тот зайдёт по тому же токену.
    response = RedirectResponse(f"/cabinet?m={token}", status_code=302)
    set_session(response, email)
    return response


@app.post("/auth/telegram")
async def auth_telegram(request: Request):
    """Вход через Telegram Login Widget. Принимает JSON с данными пользователя от виджета."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({'ok': False, 'error': 'Неверный формат'}, status_code=400)

    if not verify_telegram_auth(data):
        # Обрезаем id чтобы не допустить log injection от клиента
        safe_id = str(data.get('id', ''))[:20]
        logging.warning(f"[TG-AUTH] Неверная подпись от id={safe_id}")
        return JSONResponse({'ok': False, 'error': 'Неверная подпись Telegram'}, status_code=403)

    try:
        tg_id = int(data['id'])
    except (KeyError, ValueError, TypeError):
        return JSONResponse({'ok': False, 'error': 'Неверный формат id'}, status_code=400)

    # Проверяем блокировку
    blocked = await db_one("SELECT is_blocked FROM users WHERE telegram_id = ?", (tg_id,))
    if blocked and int(blocked.get('is_blocked') or 0) == 1:
        return JSONResponse({'ok': False, 'error': 'Доступ заблокирован'}, status_code=403)

    # Проверяем: новый пользователь или уже существует
    existing = await db_one("SELECT telegram_id FROM users WHERE telegram_id = ?", (tg_id,))
    is_new_user = existing is None

    # Создаём пользователя если его ещё нет (новый пользователь через виджет)
    await db_helpers.ensure_tg_user_exists(
        tg_id,
        username=data.get('username', ''),
        first_name=data.get('first_name', ''),
    )

    # Применяем реферал/партнёра для новых пользователей
    if is_new_user:
        ref_cookie = request.cookies.get('ref', '').strip()[:64]
        if ref_cookie:
            try:
                if ref_cookie.isdigit() and len(ref_cookie) <= 20:
                    ref_uid = int(ref_cookie)
                    if ref_uid != tg_id:
                        referrer = await db_helpers.get_user(ref_uid)
                        if referrer:
                            await db_helpers.set_invited_by_with_method(tg_id, ref_uid, 'referral')
                            logging.info(f"[REF] tg-auth {tg_id} приглашён {ref_uid} method=referral")
                elif ref_cookie.startswith('par_') and 4 < len(ref_cookie) <= 36:
                    partner_code = ref_cookie[4:]
                    partner = await db_one(
                        "SELECT telegram_id FROM users WHERE partner_ref_code = ?", (partner_code,)
                    )
                    if partner and partner['telegram_id'] != tg_id:
                        await db_helpers.set_invited_by_with_method(tg_id, partner['telegram_id'], 'partner')
                        logging.info(f"[REF] tg-auth {tg_id} → партнёр {partner['telegram_id']} ✓")
            except Exception as e:
                logging.warning(f"[REF] tg-auth: ошибка привязки реферала: {e}")

    # Пробный период для новых пользователей
    _actual_trial_days = 0
    if is_new_user and _TRIAL_DAYS > 0:
        try:
            async with httpx.AsyncClient(timeout=10) as _hc:
                _tr = await _hc.post(
                    f'{_BOT_INTERNAL_URL}/api/grant-trial',
                    json={'user_id': tg_id},
                )
            if _tr.status_code == 200:
                _actual_trial_days = _tr.json().get('trial_days', _TRIAL_DAYS)
                logging.info(f"[TRIAL] Telegram: пробный период выдан user={tg_id}, дней={_actual_trial_days}")
            else:
                logging.warning(f"[TRIAL] Telegram: бот вернул {_tr.status_code}")
        except Exception as _te:
            logging.warning(f"[TRIAL] Telegram: не удалось запросить триал: {_te}")

    # Определяем идентификатор сессии:
    # если у пользователя привязан email — используем его, иначе tg:ID
    user_row = await db_one("SELECT email, username FROM users WHERE telegram_id = ?", (tg_id,))
    email_val = (user_row or {}).get('email', '') or ''
    session_id = email_val if (email_val and not email_val.startswith('tg:')) else f'tg:{tg_id}'

    _show_welcome = _TRIAL_DAYS > 0 and is_new_user and _actual_trial_days > 0
    redirect_url = f"/cabinet?welcome=1&trial_days={_actual_trial_days}" if _show_welcome else "/cabinet"
    response = JSONResponse({'ok': True, 'redirect': redirect_url})
    set_session(response, session_id)
    logging.info(f"[TG-AUTH] Успешный вход: telegram_id={tg_id} is_new={is_new_user}")
    return response


@app.get("/auth/logout")
async def auth_logout(request: Request):
    # HX-Redirect заставляет htmx делать полный редирект (не partial swap)
    # чтобы nav обновился и показал "Войти" вместо "Кабинет"
    if request.headers.get("hx-request"):
        response = Response(status_code=200)
        response.headers["HX-Redirect"] = "/auth"
    else:
        response = RedirectResponse("/auth", status_code=302)
    clear_session(response)
    return response


# ── Google OAuth ───────────────────────────────────────────────────────────────

@app.get("/auth/google")
async def auth_google(request: Request):
    """Редирект на страницу авторизации Google."""
    if not _GOOGLE_ENABLED:
        raise HTTPException(status_code=404)
    # CSRF: генерируем state, кладём в cookie на 5 минут
    state = secrets.token_urlsafe(16)
    # Определяем redirect_uri с учётом прокси (https за nginx)
    scheme = request.headers.get('x-forwarded-proto', request.url.scheme)
    host   = request.headers.get('x-forwarded-host', request.headers.get('host', request.url.netloc))
    redirect_uri = f"{scheme}://{host}/auth/google/callback"
    params = {
        'client_id':     _GOOGLE_CLIENT_ID,
        'redirect_uri':  redirect_uri,
        'response_type': 'code',
        'scope':         'openid email profile',
        'state':         state,
        'prompt':        'select_account',
    }
    from urllib.parse import urlencode
    google_url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(params)
    response = RedirectResponse(google_url, status_code=302)
    response.set_cookie('_gstate', state, max_age=300, httponly=True, samesite='lax')
    return response


@app.get("/auth/google/callback")
async def auth_google_callback(request: Request):
    """Обрабатывает ответ от Google, создаёт/находит пользователя и ставит сессию."""
    if not _GOOGLE_ENABLED:
        raise HTTPException(status_code=404)

    project_name = await get_setting('project_name', 'VPN')

    # Проверяем CSRF state
    state_cookie = request.cookies.get('_gstate', '')
    state_param  = request.query_params.get('state', '')
    if not state_cookie or not secrets.compare_digest(state_cookie, state_param):
        return tr(request, "auth.html", project_name=project_name,
                  error="Ошибка безопасности. Попробуйте войти снова.",
                  google_enabled=_GOOGLE_ENABLED, yandex_enabled=_YANDEX_ENABLED,
                  bot_username=_BOT_USERNAME, captcha_site_key='')

    # Проверяем наличие code
    code = request.query_params.get('code', '')
    if not code:
        error_desc = request.query_params.get('error_description', 'Авторизация отменена')
        return tr(request, "auth.html", project_name=project_name,
                  error=error_desc, google_enabled=_GOOGLE_ENABLED,
                  yandex_enabled=_YANDEX_ENABLED, bot_username=_BOT_USERNAME,
                  captcha_site_key='')

    # Восстанавливаем redirect_uri (должен точно совпадать с тем что отправляли)
    scheme = request.headers.get('x-forwarded-proto', request.url.scheme)
    host   = request.headers.get('x-forwarded-host', request.headers.get('host', request.url.netloc))
    redirect_uri = f"{scheme}://{host}/auth/google/callback"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Обмен code → tokens
            token_resp = await client.post(
                'https://oauth2.googleapis.com/token',
                data={
                    'code':          code,
                    'client_id':     _GOOGLE_CLIENT_ID,
                    'client_secret': _GOOGLE_CLIENT_SECRET,
                    'redirect_uri':  redirect_uri,
                    'grant_type':    'authorization_code',
                },
            )
            if token_resp.status_code != 200:
                raise ValueError(f"token endpoint {token_resp.status_code}: {token_resp.text[:200]}")
            token_data   = token_resp.json()
            access_token = token_data.get('access_token', '')
            if not access_token:
                raise ValueError("Нет access_token в ответе Google")

            # Получаем данные пользователя
            info_resp = await client.get(
                'https://www.googleapis.com/oauth2/v3/userinfo',
                headers={'Authorization': f'Bearer {access_token}'},
            )
            if info_resp.status_code != 200:
                raise ValueError(f"userinfo {info_resp.status_code}")
            info = info_resp.json()

        google_email = (info.get('email') or '').strip().lower()
        if not google_email:
            raise ValueError("Google не вернул email")

        wc_cfg = await get_website_cabinet_config()
        google_exists = await email_registered_in_db(google_email)
        if not is_email_domain_allowed(
            google_email, wc_cfg, user_already_exists=google_exists,
        ):
            return tr(request, "auth.html", project_name=project_name,
                      error=EMAIL_DOMAIN_REJECT_MESSAGE,
                      google_enabled=_GOOGLE_ENABLED, yandex_enabled=_YANDEX_ENABLED,
                      bot_username=_BOT_USERNAME, captcha_site_key='')

        # Ищем или создаём пользователя
        user = await get_web_user_by_email(google_email)
        if not user:
            user = await db_one(
                "SELECT * FROM users WHERE LOWER(email) = LOWER(?) LIMIT 1", (google_email,),
            )
        is_new = user is None
        if is_new:
            await create_web_user(google_email)
            user = await get_web_user_by_email(google_email)

        # Пробный период для новых пользователей
        _actual_trial_days = 0
        if is_new and _TRIAL_DAYS > 0 and user:
            try:
                async with httpx.AsyncClient(timeout=10) as _hc:
                    _tr = await _hc.post(
                        f'{_BOT_INTERNAL_URL}/api/grant-trial',
                        json={'user_id': user['telegram_id']},
                    )
                if _tr.status_code == 200:
                    _actual_trial_days = _tr.json().get('trial_days', _TRIAL_DAYS)
                    logging.info(f"[TRIAL] Google: пробный период выдан user={user['telegram_id']}, дней={_actual_trial_days}")
                else:
                    logging.warning(f"[TRIAL] Google: бот вернул {_tr.status_code}")
            except Exception as _te:
                logging.warning(f"[TRIAL] Google: не удалось запросить триал: {_te}")

        # Ставим сессию и редиректим в кабинет
        _show_welcome = _TRIAL_DAYS > 0 and is_new and _actual_trial_days > 0
        cabinet_url = f"/cabinet?welcome=1&trial_days={_actual_trial_days}" if _show_welcome else "/cabinet"
        response = RedirectResponse(cabinet_url, status_code=302)
        set_session(response, google_email)
        response.delete_cookie('_gstate')
        return response

    except Exception as e:
        logging.getLogger(__name__).error(f"Google OAuth error: {e}", exc_info=True)
        return tr(request, "auth.html", project_name=project_name,
                  error="Не удалось войти через Google. Попробуйте позже.",
                  google_enabled=_GOOGLE_ENABLED, yandex_enabled=_YANDEX_ENABLED,
                  bot_username=_BOT_USERNAME, captcha_site_key='')


# ── Yandex OAuth ───────────────────────────────────────────────────────────────

@app.get("/auth/yandex")
async def auth_yandex(request: Request):
    """Редирект на страницу авторизации Яндекс."""
    if not _YANDEX_ENABLED:
        raise HTTPException(status_code=404)
    state = secrets.token_urlsafe(16)
    scheme = request.headers.get('x-forwarded-proto', request.url.scheme)
    host   = request.headers.get('x-forwarded-host', request.headers.get('host', request.url.netloc))
    redirect_uri = f"{scheme}://{host}/auth/yandex/callback"
    from urllib.parse import urlencode
    yandex_url = 'https://oauth.yandex.ru/authorize?' + urlencode({
        'response_type': 'code',
        'client_id':     _YANDEX_CLIENT_ID,
        'redirect_uri':  redirect_uri,
        'state':         state,
        'force_confirm': 'no',
    })
    response = RedirectResponse(yandex_url, status_code=302)
    response.set_cookie('_ystate', state, max_age=300, httponly=True, samesite='lax')
    return response


@app.get("/auth/yandex/callback")
async def auth_yandex_callback(request: Request):
    """Обрабатывает ответ от Яндекс, создаёт/находит пользователя и ставит сессию."""
    if not _YANDEX_ENABLED:
        raise HTTPException(status_code=404)

    project_name = await get_setting('project_name', 'VPN')

    def _err(msg: str):
        return tr(request, "auth.html", project_name=project_name, error=msg,
                  google_enabled=_GOOGLE_ENABLED, yandex_enabled=_YANDEX_ENABLED,
                  bot_username=_BOT_USERNAME, captcha_site_key='')

    # Проверяем CSRF state
    state_cookie = request.cookies.get('_ystate', '')
    state_param  = request.query_params.get('state', '')
    if not state_cookie or not secrets.compare_digest(state_cookie, state_param):
        return _err("Ошибка безопасности. Попробуйте войти снова.")

    code = request.query_params.get('code', '')
    if not code:
        return _err(request.query_params.get('error_description', 'Авторизация отменена'))

    scheme = request.headers.get('x-forwarded-proto', request.url.scheme)
    host   = request.headers.get('x-forwarded-host', request.headers.get('host', request.url.netloc))
    redirect_uri = f"{scheme}://{host}/auth/yandex/callback"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Обмен code → token
            token_resp = await client.post(
                'https://oauth.yandex.ru/token',
                data={
                    'grant_type':   'authorization_code',
                    'code':         code,
                    'client_id':    _YANDEX_CLIENT_ID,
                    'client_secret': _YANDEX_CLIENT_SECRET,
                    'redirect_uri': redirect_uri,
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
            )
            if token_resp.status_code != 200:
                raise ValueError(f"token {token_resp.status_code}: {token_resp.text[:200]}")
            access_token = token_resp.json().get('access_token', '')
            if not access_token:
                raise ValueError("Нет access_token в ответе Яндекс")

            # Получаем данные пользователя
            info_resp = await client.get(
                'https://login.yandex.ru/info',
                params={'format': 'json'},
                headers={'Authorization': f'OAuth {access_token}'},
            )
            if info_resp.status_code != 200:
                raise ValueError(f"info {info_resp.status_code}")
            info = info_resp.json()

        # Яндекс может вернуть default_email или emails[]
        yandex_email = (
            info.get('default_email') or
            (info.get('emails') or [''])[0]
        ).strip().lower()
        if not yandex_email:
            raise ValueError("Яндекс не вернул email")

        wc_cfg = await get_website_cabinet_config()
        yandex_exists = await email_registered_in_db(yandex_email)
        if not is_email_domain_allowed(
            yandex_email, wc_cfg, user_already_exists=yandex_exists,
        ):
            return tr(request, "auth.html", project_name=project_name,
                      error=EMAIL_DOMAIN_REJECT_MESSAGE,
                      google_enabled=_GOOGLE_ENABLED, yandex_enabled=_YANDEX_ENABLED,
                      bot_username=_BOT_USERNAME, captcha_site_key='')

        user = await get_web_user_by_email(yandex_email)
        if not user:
            user = await db_one(
                "SELECT * FROM users WHERE LOWER(email) = LOWER(?) LIMIT 1", (yandex_email,),
            )
        is_new = user is None
        if is_new:
            await create_web_user(yandex_email)
            user = await get_web_user_by_email(yandex_email)

        # Пробный период для новых пользователей
        _actual_trial_days = 0
        if is_new and _TRIAL_DAYS > 0 and user:
            try:
                async with httpx.AsyncClient(timeout=10) as _hc:
                    _tr = await _hc.post(
                        f'{_BOT_INTERNAL_URL}/api/grant-trial',
                        json={'user_id': user['telegram_id']},
                    )
                if _tr.status_code == 200:
                    _actual_trial_days = _tr.json().get('trial_days', _TRIAL_DAYS)
                    logging.info(f"[TRIAL] Yandex: пробный период выдан user={user['telegram_id']}, дней={_actual_trial_days}")
                else:
                    logging.warning(f"[TRIAL] Yandex: бот вернул {_tr.status_code}")
            except Exception as _te:
                logging.warning(f"[TRIAL] Yandex: не удалось запросить триал: {_te}")

        _show_welcome = _TRIAL_DAYS > 0 and is_new and _actual_trial_days > 0
        cabinet_url = f"/cabinet?welcome=1&trial_days={_actual_trial_days}" if _show_welcome else "/cabinet"
        response = RedirectResponse(cabinet_url, status_code=302)
        set_session(response, yandex_email)
        response.delete_cookie('_ystate')
        return response

    except Exception as e:
        logging.getLogger(__name__).error(f"Yandex OAuth error: {e}", exc_info=True)
        return _err("Не удалось войти через Яндекс. Попробуйте позже.")


# ── Личный кабинет ────────────────────────────────────────────────────────────

@app.get("/cabinet", response_class=HTMLResponse)
async def cabinet(request: Request):
    email = get_session_email(request)

    # Поддержка авторизации по `?m=<token>` — нужно для перехода из
    # in-app Telegram-браузера в нативный Safari/Chrome через «Открыть в браузере».
    # Telegram передаёт текущий URL внешнему браузеру, и если у того нет cookie,
    # авторизуем его по живому magic-link токену (он не был потреблён, см.
    # auth_magic_link). После авторизации делаем чистый редирект без `?m=`,
    # чтобы убрать токен из адресной строки и истории.
    if not email:
        m_token = (request.query_params.get('m') or '').strip()
        if m_token:
            token_info = await peek_web_auth_token(m_token)
            token_email = (token_info or {}).get('email', '').strip() if token_info else ''
            if token_email:
                # Проверяем блокировку (как в auth_magic_link)
                blocked = False
                user = await get_web_user_by_email(token_email)
                if user:
                    _chk = await db_one(
                        "SELECT is_blocked FROM users WHERE telegram_id = ?",
                        (user['telegram_id'],)
                    )
                    if _chk and int(_chk.get('is_blocked') or 0) == 1:
                        blocked = True
                if not blocked:
                    response = RedirectResponse("/cabinet", status_code=302)
                    set_session(response, token_email)
                    return response

    if not email:
        return RedirectResponse("/auth")

    project_name = await get_setting('project_name', 'VPN')

    # Поддержка сессии без email: tg:TELEGRAM_ID — пользователь из бота без привязанной почты
    display_email = email
    if email.startswith('tg:'):
        try:
            tg_id = int(email[3:])
            user = await db_one("SELECT * FROM users WHERE telegram_id = ?", (tg_id,))
            # Показываем реальный email если он привязан в боте
            db_email = (user or {}).get('email', '') or ''
            if db_email and not db_email.startswith('tg:'):
                display_email = db_email
            else:
                # Ищем веб-запись по invited_by или xui_client_email — нет прямой связи,
                # поэтому обновляем сессию на email при следующем входе через виджет.
                display_email = ''
        except Exception:
            user = None
    else:
        user = await get_web_user_by_email(email)
        # Если веб-пользователь не найден по email — возможно это бот-пользователь с привязанным email
        if not user:
            user = await db_one("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email,))

    sub = None
    sub_url = None

    happ_url = None
    sub_key = None
    sub_info = {}   # детальная информация с xuiweb API

    if user:
        uid = user['telegram_id']
        sub = await db_one("SELECT * FROM users WHERE telegram_id = ?", (uid,))

        # Проверка блокировки
        if sub and int(sub.get('is_blocked') or 0) == 1:
            response = RedirectResponse("/auth/logout")
            return response

        sub_page_url  = await get_setting('sub_page_url', '')
        connect_url   = await get_setting('connect_page_url', sub_page_url)
        uuid = (user.get('xui_client_uuid') or user.get('remnawave_short_uuid') or '')

        if sub_page_url and uuid:
            sub_url = f"{sub_page_url}/sub/{uuid}"

            # Получаем детальную информацию о подписке с xuiweb /api/sub/{uuid}
            try:
                # Используем внутренний адрес xuiweb напрямую (из env, потом из настроек, потом дефолт)
                _xuiweb_base = (
                    os.getenv('XUIWEB_INTERNAL_URL', '').strip()
                    or (await get_setting('xuiweb_internal_url', '')).strip()
                    or 'http://127.0.0.1:8282'
                ).rstrip('/')
                async with httpx.AsyncClient(timeout=8, verify=False) as client:
                    api_url = f"{_xuiweb_base}/api/sub/{uuid}"
                    resp = await client.get(api_url, headers={
                        'Accept': 'text/html,application/xhtml+xml,*/*',
                        'Sec-Fetch-Dest': 'document',
                        'Sec-Fetch-Mode': 'navigate',
                    })
                    if resp.status_code == 200:
                        data = resp.json()
                        user_data = data.get('user') or {}
                        # Трафик
                        traffic_limit = int(user_data.get('trafficLimitBytes') or 0)
                        traffic_used  = int(user_data.get('trafficUsedBytes') or 0)
                        if traffic_limit > 0:
                            sub_info['traffic_limit_gb'] = round(traffic_limit / (1024**3), 2)
                            sub_info['traffic_used_gb']  = round(traffic_used  / (1024**3), 2)
                            sub_info['traffic_pct'] = min(100, round(traffic_used / traffic_limit * 100))
                        sub_info['days_left'] = user_data.get('daysLeft', '')
                        sub_info['is_active'] = user_data.get('isActive', False)
                        _expires_raw = user_data.get('expiresAt', '') or ''
                        sub_info['expires_at'] = _expires_raw[:10] if _expires_raw else ''
                        # Считаем часы когда осталось меньше суток
                        if sub_info['days_left'] == 0 and _expires_raw:
                            try:
                                _exp_dt = datetime.fromisoformat(_expires_raw.replace('Z', '+00:00'))
                                _now_dt = datetime.now(timezone.utc)
                                _hours = max(0, int((_exp_dt - _now_dt).total_seconds() // 3600))
                                sub_info['hours_left'] = _hours
                            except Exception:
                                pass
                        # Лимит устройств — из API или из user_data
                        limit_ip_api = user_data.get('limitIp') or user_data.get('limitIp')
                        if limit_ip_api is not None:
                            sub_info['limit_ip'] = int(limit_ip_api)
            except Exception:
                pass

            # Если API не вернул limit_ip — берём из БД
            if 'limit_ip' not in sub_info and sub:
                limit_ip_db = sub.get('limit_ip') or 0
                if limit_ip_db:
                    sub_info['limit_ip'] = int(limit_ip_db)

            # В режиме happcrypto — шифруем через happ.crypto API
            # В режиме subscription — отдаём ссылку как есть
            if _DEF_SUB_MODE == 'happcrypto':
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.post(
                            "https://crypto.happ.su/api-v2.php",
                            json={"url": sub_url}
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            happ_url = data.get("encrypted_link") or data.get("url")
                            sub_key = happ_url
                except Exception:
                    pass
            else:
                sub_key = sub_url

    ym_acc, ym_tok, ym_sec = await _get_yoomoney_creds()
    # token (ym_tok) — необязателен: используется только для API-fallback
    # (operation-history). Quickpay и webhook верифицируются через
    # account + notification_secret.
    wata_token, _wata_pid = await _get_wata_creds()

    # Платёжные методы: единый порядок из payment_methods_order
    from payment_methods import get_ordered_payment_methods, PAYMENT_METHOD_LABELS
    _pay_conf = {}
    for _pk in (
        'payment_methods_order',
        'show_payment_yookassa', 'yookassa_shop_id', 'yookassa_secret_key',
        'show_payment_platega', 'platega_merchant_id', 'platega_api_secret',
        'show_payment_yoomoney', 'yoomoney_account', 'yoomoney_notification_secret',
        'show_payment_wata', 'wata_access_token',
    ):
        _pay_conf[_pk] = await get_setting(_pk, '')

    site_methods = get_ordered_payment_methods('site', conf=_pay_conf)
    yookassa_on = 'yookassa' in site_methods
    platega_on = 'platega' in site_methods
    yoomoney_on = 'yoomoney' in site_methods
    wata_on = 'wata' in site_methods
    payment_methods_site = [{'id': m, 'label': PAYMENT_METHOD_LABELS.get(m, m)} for m in site_methods]

    # v2: метод оплаты выбирает плательщик на странице Platega → передаём пустой список
    platega_methods = []
    # Название кнопки Platega берётся из общей настройки бота (btn_payment_platega).
    # Emoji вырезаем — рядом с текстом уже отображается SVG-иконка Platega.
    platega_btn_text_raw = (await get_setting('btn_payment_platega', '🏦 Platega')) or '🏦 Platega'
    platega_btn_text = strip_emoji(platega_btn_text_raw) or 'Platega'
    # Название кнопки Wata — из той же админ-настройки бота (btn_payment_wata).
    # Emoji выкидываем — рядом уже SVG-иконка Wata.
    wata_btn_text_raw = (await get_setting('btn_payment_wata', '💳 Wata')) or '💳 Wata'
    wata_btn_text = strip_emoji(wata_btn_text_raw) or 'Wata'

    all_tariffs_raw = await db_get("SELECT * FROM tariffs WHERE is_active=1 ORDER BY days, COALESCE(limit_ip,0), price")

    # Фильтруем по доступным методам оплаты
    def tariff_allows(t, methods: list) -> list:
        pm = (t.get('payment_method') or 'all').lower()
        allowed = []
        if pm in ('all', None, ''):
            allowed = methods
        elif pm == 'both':
            allowed = methods  # 'both' исторически = yookassa + platega; yoomoney
                                # тоже подключим, если активен (legacy-тарифы должны
                                # уважать актуальный список методов сайта).
        elif pm == 'yookassa':
            allowed = ['yookassa'] if 'yookassa' in methods else []
        elif pm == 'platega':
            allowed = ['platega'] if 'platega' in methods else []
        elif pm == 'yoomoney':
            allowed = ['yoomoney'] if 'yoomoney' in methods else []
        elif pm == 'wata':
            allowed = ['wata'] if 'wata' in methods else []
        return allowed

    site_methods = get_ordered_payment_methods('site', conf=_pay_conf)

    # Объединяем тарифы с одинаковыми (days, limit_ip, price) из разных платёжных систем в один ряд
    def merge_tariffs(tariffs_list: list, methods: list) -> list:
        merged: dict = OrderedDict()
        for t in tariffs_list:
            t_methods = tariff_allows(t, methods)
            if not t_methods:
                continue
            key = (int(t.get('days') or 0), int(t.get('limit_ip') or 0), float(t.get('price') or 0))
            if key not in merged:
                t_copy = dict(t)
                t_copy['_merged_methods'] = set(t_methods)
                merged[key] = t_copy
            else:
                merged[key]['_merged_methods'].update(t_methods)
        result = []
        for t in merged.values():
            mm = t.pop('_merged_methods', set())
            # Если активны все 3 — ставим 'all', чтобы шаблон знал что показать всех.
            if {'yookassa', 'platega', 'yoomoney'}.issubset(mm):
                t['payment_method'] = 'all'
            elif {'yookassa', 'platega'} == mm:
                t['payment_method'] = 'all'  # обратная совместимость для legacy
            elif len(mm) == 1:
                t['payment_method'] = next(iter(mm))
            else:
                # Несколько, но не все — делаем 'all' и в шаблоне фильтруем
                # по флагу включения каждого метода.
                t['payment_method'] = 'all'
            t['_methods_set'] = list(mm)
            result.append(t)
        return result

    all_tariffs = merge_tariffs(all_tariffs_raw, site_methods)

    # ── Логика выбора лимита устройств ───────────────────────────────────────
    # • Новый юзер (без активной подписки) — может выбрать любой лимит.
    # • Подписка истекла — может выбрать любой лимит (фактически новая покупка).
    # • Подписка истекает в окне TARIFF_CHANGE_WINDOW_DAYS — может выбрать любой
    #   лимит для продления (в т.ч. с другим числом устройств).
    # • Подписка ещё долго (> окна) — защита: только тарифы своего лимита.
    TARIFF_CHANGE_WINDOW_DAYS = 7

    user_limit_ip = int((sub.get('limit_ip') or 0) if sub else 0)
    sub_end_date_str = (sub.get('subscription_end_date') or '') if sub else ''
    has_subscription = bool(sub_end_date_str)

    # Уникальные группы лимитов (для селектора)
    all_limits = sorted(set(int(t.get('limit_ip') or 0) for t in all_tariffs))

    # Сколько дней до окончания подписки (отрицательное = уже истекла)
    days_until_end = None
    if has_subscription:
        try:
            end_dt = datetime.fromisoformat(sub_end_date_str.replace('Z', '+00:00'))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            delta_seconds = (end_dt - datetime.now(timezone.utc)).total_seconds()
            days_until_end = delta_seconds / 86400.0
        except Exception:
            days_until_end = None

    # Первая покупка — нет подписки или нет зафиксированного лимита
    is_first_purchase = (not has_subscription) or (user_limit_ip <= 0)

    # «Окно продления» открыто, если подписка истекла либо до конца ≤ окна
    is_renewal_window_open = (
        (not is_first_purchase)
        and (days_until_end is None or days_until_end <= TARIFF_CHANGE_WINDOW_DAYS)
    )

    # Можно ли пользователю выбирать лимит (UI-флаг)
    can_change_limit = is_first_purchase or is_renewal_window_open

    # Хелпер: можно ли юзеру выбрать тариф с лимитом t_lim, имея текущий user_lim.
    # ЗАПРЕТ ПОНИЖЕНИЯ: при продлении в окне нельзя выбрать лимит МЕНЬШЕ текущего —
    # клиент уже зафиксировал устройства, понижение «забыло бы» часть из них.
    # `0` в схеме = безлимит (∞), считается «выше любого числа».
    def _can_take_lim(t_lim: int, user_lim: int) -> bool:
        if user_lim <= 0:           # Юзер на безлимите — только безлимит
            return t_lim <= 0
        if t_lim <= 0:              # Безлимитный тариф — всегда «выше» числа
            return True
        return t_lim >= user_lim    # Только равный или больший лимит

    if is_first_purchase:
        # Первая покупка — все тарифы доступны, дефолт = минимальный лимит
        tariffs = all_tariffs
        default_limit = min(all_limits, default=0)
    elif is_renewal_window_open:
        # Окно продления — все тарифы С РАВНЫМ ИЛИ БОЛЬШИМ лимитом, без понижения
        tariffs = [t for t in all_tariffs if _can_take_lim(int(t.get('limit_ip') or 0), user_limit_ip)]
        default_limit = user_limit_ip
    else:
        # Защита (далеко до окончания) — только тарифы своего лимита
        matching = [t for t in all_tariffs if (t.get('limit_ip') or 0) == user_limit_ip]
        if not matching and all_limits:
            nearest = min(all_limits, key=lambda x: abs(x - user_limit_ip))
            matching = [t for t in all_tariffs if (t.get('limit_ip') or 0) == nearest]
        tariffs = matching
        default_limit = user_limit_ip

    # Группируем ДОСТУПНЫЕ тарифы (с учётом запрета понижения) по limit_ip
    # для селектора в шаблоне.
    limit_groups = {}
    for t in tariffs:
        lim = int(t.get('limit_ip') or 0)
        if lim not in limit_groups:
            limit_groups[lim] = []
        limit_groups[lim].append(t)

    # Приложения
    app_ios         = await get_setting('app_link_ios', '')
    app_ios_global  = await get_setting('app_link_ios_global', '')
    app_android     = await get_setting('app_link_android', '')
    app_android_apk = await get_setting('app_link_android_apk', '')
    app_windows     = await get_setting('app_link_windows', '')
    app_mac         = await get_setting('app_link_mac', '')
    support_link = await get_setting('support_link', '')

    now_str = datetime.now(timezone.utc).isoformat()

    # Реферальная программа
    ref_user_id = user['telegram_id'] if user else None
    ref_count = 0
    ref_bonus_days = 7
    if ref_user_id:
        try:
            rows = await db_get("SELECT COUNT(*) as cnt FROM users WHERE invited_by = ?", (ref_user_id,))
            ref_count = rows[0]['cnt'] if rows else 0
        except Exception:
            pass
        try:
            ref_bonus_days = int(await get_setting('ref_bonus_on_payment_days', '7'))
        except Exception:
            ref_bonus_days = 7

    # URL сайта: сначала из настроек БД, потом из env, потом из заголовков запроса
    site_url = (await get_setting('website_url', '')).rstrip('/')
    if not site_url:
        site_url = os.getenv('SITE_URL', '').rstrip('/')
    if not site_url:
        scheme = request.headers.get('x-forwarded-proto', request.url.scheme)
        host = request.headers.get('x-forwarded-host', request.headers.get('host', request.url.netloc))
        site_url = f"{scheme}://{host}"
    ref_link = f"{site_url}/?ref={ref_user_id}" if ref_user_id else ''

    # Докупка трафика
    traffic_renewal_enabled = await get_setting('traffic_renewal_enabled', '0') == '1'
    traffic_topup_tariffs = []
    _is_active = sub_info.get('is_active', False) if sub_info else (
        bool(sub and sub.get('subscription_end_date') and sub.get('subscription_end_date', '') > now_str)
    )
    if traffic_renewal_enabled and _is_active:
        try:
            _raw_traffic = await db_get(
                "SELECT * FROM traffic_topup_tariffs WHERE is_active = 1 ORDER BY traffic_gb, price"
            )
            # Фильтруем и объединяем по (traffic_gb, price) так же как основные тарифы
            def merge_traffic_tariffs(tariffs_list: list, methods: list) -> list:
                merged: dict = OrderedDict()
                for t in tariffs_list:
                    t_methods = tariff_allows(t, methods)
                    if not t_methods:
                        continue
                    key = (float(t.get('traffic_gb') or 0), float(t.get('price') or 0))
                    if key not in merged:
                        t_copy = dict(t)
                        t_copy['_merged_methods'] = set(t_methods)
                        merged[key] = t_copy
                    else:
                        merged[key]['_merged_methods'].update(t_methods)
                result = []
                for t in merged.values():
                    mm = t.pop('_merged_methods', set())
                    if {'yookassa', 'platega', 'yoomoney'}.issubset(mm):
                        t['payment_method'] = 'all'
                    elif {'yookassa', 'platega'} == mm:
                        t['payment_method'] = 'all'
                    elif len(mm) == 1:
                        t['payment_method'] = next(iter(mm))
                    else:
                        t['payment_method'] = 'all'
                    t['_methods_set'] = list(mm)
                    result.append(t)
                return result

            traffic_topup_tariffs = merge_traffic_tariffs(_raw_traffic, site_methods)
        except Exception:
            traffic_topup_tariffs = []

    # Партнёрская программа
    partner_ref_code = (sub.get('partner_ref_code') or '') if sub else ''
    partner_balance  = float((sub.get('partner_balance_rub') or 0)) if sub else 0.0
    partner_percent  = 0
    partner_link     = ''
    partner_accruals = []
    show_partner     = False
    _show_partner_global = await get_setting('show_partner_program_button', '0') == '1'
    _show_partner_user   = (sub.get('show_partner_program_button') or '') == '1' if sub else False
    # Приоритет: глобальная → индивидуальная
    if _show_partner_global:
        show_partner = True
    elif _show_partner_user:
        show_partner = True
    # Данные загружаем только если раздел показывается и код есть
    if show_partner and partner_ref_code:
        partner_link = f"{site_url}/?ref=par_{partner_ref_code}"
        try:
            partner_percent = await db_helpers.get_partner_percent(ref_user_id)
        except Exception:
            partner_percent = int(await get_setting('partner_percent_rub', '10'))
        try:
            partner_accruals = await db_helpers.get_partner_accruals(ref_user_id, limit=5)
        except Exception:
            partner_accruals = []

    # ── Расширение лимита устройств (device_upgrade) ──────────────────────────
    # Логика и цены — в bot-процессе (app_conf). Сюда тянем готовые варианты
    # по внутреннему API бота. Показываем блок, только если фича включена,
    # есть активная подписка с числовым лимитом и доступны опции апгрейда.
    device_upgrade = None
    device_upgrade_status = None
    if user and await get_setting('device_upgrade_enabled', '0') == '1':
        try:
            async with httpx.AsyncClient(timeout=8) as _hc:
                _du = await _hc.post(
                    f'{_BOT_INTERNAL_URL}/api/device-upgrade/options',
                    json={'user_id': int(user['telegram_id'])},
                )
            if _du.status_code == 200:
                _du_data = _du.json()
                if _du_data.get('ok'):
                    # Сохраняем статус даже когда вариантов нет: сайт должен
                    # показать клиенту понятную причину, как это делает бот
                    # (например, too_few_days_left).
                    device_upgrade_status = _du_data
                    if _du_data.get('allowed') and _du_data.get('options'):
                        device_upgrade = _du_data
        except Exception as _due:
            logging.warning(f"[DEVICE-UPGRADE] options fetch failed: {_due}")

    _welcome_trial_days = int(request.query_params.get('trial_days') or 0)
    show_welcome = request.query_params.get('welcome') == '1' and _welcome_trial_days > 0

    return tr(request, "cabinet.html",
        project_name=project_name, email=display_email, is_authenticated=True,
        user=user, sub=sub, sub_url=sub_url,
        happ_url=happ_url, sub_key=sub_key,
        sub_info=sub_info,
        tariffs=tariffs,
        limit_groups=limit_groups,
        is_first_purchase=is_first_purchase,
        is_renewal_window_open=is_renewal_window_open,
        can_change_limit=can_change_limit,
        days_until_end=days_until_end,
        user_limit_ip=user_limit_ip,
        default_limit=default_limit,
        yookassa_on=yookassa_on,
        platega_on=platega_on,
        yoomoney_on=yoomoney_on,
        wata_on=wata_on,
        payment_methods_site=payment_methods_site,
        platega_methods=platega_methods,
        platega_btn_text=platega_btn_text,
        wata_btn_text=wata_btn_text,
        app_ios=app_ios, app_ios_global=app_ios_global,
        app_android=app_android, app_android_apk=app_android_apk,
        app_windows=app_windows, app_mac=app_mac,
        support_link=support_link,
        now=now_str,
        ref_link=ref_link,
        ref_count=ref_count,
        ref_bonus_days=ref_bonus_days,
        show_partner=show_partner,
        partner_ref_code=partner_ref_code,
        partner_link=partner_link,
        partner_balance=partner_balance,
        partner_percent=partner_percent,
        partner_accruals=partner_accruals,
        traffic_renewal_enabled=traffic_renewal_enabled,
        traffic_topup_tariffs=traffic_topup_tariffs,
        show_welcome=show_welcome,
        trial_days=_welcome_trial_days,
        devices_enabled=_DEVICES_ENABLED,
        device_upgrade=device_upgrade,
        device_upgrade_status=device_upgrade_status,
        add_link=_ADD_LINK,
    )


# ── Rate limiter для платежей ─────────────────────────────────────────────────
# In-memory: ключ → список timestamp-ов попыток
# Не требует БД, чистится автоматически при каждом обращении
_payment_attempts: dict = defaultdict(list)
_PAYMENT_LIMIT  = int(os.getenv('PAYMENT_RATE_LIMIT', '3'))    # максимум попыток
_PAYMENT_WINDOW = int(os.getenv('PAYMENT_RATE_WINDOW', '300'))  # окно в секундах (5 минут)

def _payment_rate_limit(uid: int) -> tuple[bool, int]:
    """
    Проверяет лимит платёжных запросов для пользователя.
    Возвращает (разрешено, секунд_до_сброса).
    Записывает попытку только если разрешено.
    """
    now = time.time()
    attempts = _payment_attempts[uid]
    # Удаляем устаревшие записи за пределами окна
    attempts[:] = [t for t in attempts if now - t < _PAYMENT_WINDOW]
    if len(attempts) >= _PAYMENT_LIMIT:
        retry_after = int(_PAYMENT_WINDOW - (now - attempts[0])) + 1
        return False, retry_after
    attempts.append(now)
    return True, 0


# ── Оплата ────────────────────────────────────────────────────────────────────

@app.post("/pay/traffic/{tariff_id}")
async def pay_traffic(request: Request, tariff_id: int):
    """Оплата докупки трафика."""
    email = get_session_email(request)
    if not email:
        return JSONResponse({'ok': False, 'error': 'Необходима авторизация'}, status_code=401)

    tariff = await db_one(
        "SELECT * FROM traffic_topup_tariffs WHERE id = ? AND is_active = 1", (tariff_id,)
    )
    if not tariff:
        return JSONResponse({'ok': False, 'error': 'Тариф не найден'}, status_code=404)

    user = await get_user_from_session(email)
    if not user:
        return JSONResponse({'ok': False, 'error': 'Пользователь не найден'}, status_code=404)

    uid = user['telegram_id']
    if int(user.get('is_blocked') or 0) == 1:
        return JSONResponse({'ok': False, 'error': 'Аккаунт заблокирован'}, status_code=403)

    # Берём валидный email из записи пользователя (сессия может быть tg:ID)
    user_email = (user.get('email') or '').strip()
    if not user_email or '@' not in user_email or user_email.startswith('tg:'):
        user_email = ''

    allowed, retry_after = _payment_rate_limit(uid)
    if not allowed:
        logging.warning(f"[PAY-LIMIT] traffic uid={uid} превышен лимит, retry={retry_after}s")
        return JSONResponse(
            {'ok': False, 'error': 'Превышен лимит запросов. Попробуйте позже.'},
            status_code=429,
        )

    amount = float(tariff['price'])
    traffic_gb = int(tariff.get('traffic_gb') or 0)

    scheme = request.headers.get('x-forwarded-proto', request.url.scheme)
    host = request.headers.get('host', request.url.netloc)
    return_url = f"{scheme}://{host}/cabinet?paid=1"

    form = await request.form()
    method = form.get('method', 'yookassa')

    # Метаданные с payment_type=traffic_renewal — main.py их обработает
    # tariff_id не передаём — create_*_payment_shared берёт его из tariff['id'] сам
    extra_meta = {
        "payment_type": "traffic_renewal",
        "traffic_to_add_gb": traffic_gb,
    }

    result = None
    if method == 'yookassa':
        shop_id, secret = await _get_yookassa_creds()
        if not shop_id or not secret:
            return JSONResponse({'ok': False, 'error': 'Оплата временно недоступна.'}, status_code=503)
        fake_tariff = {
            'id': tariff_id, 'days': 0, 'price': amount,
            'limit_ip': 0, 'currency': 'RUB', 'name': tariff.get('name', ''),
        }
        result = await create_yookassa_payment_shared(
            shop_id=shop_id, secret_key=secret,
            amount=amount, currency='RUB',
            tariff=fake_tariff, user_id=uid, return_url=return_url,
            registration_type='site', email=user_email,
            **extra_meta,
        )
    elif method == 'platega':
        merchant_id, api_secret = await _get_platega_creds()
        if not merchant_id or not api_secret:
            return JSONResponse({'ok': False, 'error': 'Оплата временно недоступна.'}, status_code=503)
        fake_tariff = {
            'id': tariff_id, 'days': 0, 'price': amount,
            'limit_ip': 0, 'currency': 'RUB', 'name': tariff.get('name', ''),
        }
        result = await create_platega_payment_shared(
            merchant_id=merchant_id, api_secret=api_secret,
            amount=amount, tariff=fake_tariff, user_id=uid, return_url=return_url,
            registration_type='site', email=user_email,
            **extra_meta,
        )
    elif method == 'yoomoney':
        account, ym_token, ym_notif = await _get_yoomoney_creds()
        # ym_token не обязателен (см. _get_yoomoney_creds): для создания платежа
        # и приёма webhook'а достаточно account + notification_secret.
        if not account or not ym_notif:
            return JSONResponse({'ok': False, 'error': 'Оплата временно недоступна.'}, status_code=503)
        ym_payment_id = f"YOOMONEY_TRAFFIC_RENEWAL_{int(time.time())}_{uid}"
        ym_meta = {
            "payment_type": "traffic_renewal",
            "telegram_user_id": uid,
            "user_id": uid,            # дублируем для совместимости с YooMoney-веткой webhook'а
            "price": amount,
            "amount": amount,          # webhook читает meta['amount']
            "currency": "RUB",
            "traffic_to_add_gb": traffic_gb,
            "tariff_id": tariff_id,
            "registration_type": "site",
            "payment_method": "YooMoney",
        }
        url = await create_yoomoney_quickpay(
            account=account,
            telegram_id=uid,
            payment_id=ym_payment_id,
            amount=amount,
            currency='RUB',
            target_text=f"Продление трафика (+{traffic_gb} GB)",
            metadata=ym_meta,
        )
        result = (ym_payment_id, url) if url else None
    elif method == 'wata':
        wata_token, wata_pid = await _get_wata_creds()
        if not wata_token:
            return JSONResponse({'ok': False, 'error': 'Оплата временно недоступна.'}, status_code=503)
        result = await create_wata_payment_traffic_renewal(
            wata_token,
            user_id=uid,
            price=amount,
            traffic_to_add_gb=traffic_gb,
            return_url=return_url,
            currency='RUB',
            tariff_id=tariff_id,
            terminal_public_id=wata_pid,
            registration_type='site',
            email=user_email,
        )
    else:
        return JSONResponse({'ok': False, 'error': 'Неизвестный метод оплаты'}, status_code=400)

    url = result[1] if result else None
    if not url:
        return JSONResponse({'ok': False, 'error': 'Ошибка создания платежа'}, status_code=500)

    return JSONResponse({'ok': True, 'redirect': url})


@app.post("/pay/device-upgrade")
async def pay_device_upgrade(request: Request):
    """Расширение лимита устройств. uid берётся из подписанной сессии,
    new_limit валидируется ботом (защита от понижения лимита)."""
    email = get_session_email(request)
    if not email:
        return JSONResponse({'ok': False, 'error': 'Необходима авторизация'}, status_code=401)
    if await get_setting('device_upgrade_enabled', '0') != '1':
        return JSONResponse({'ok': False, 'error': 'Функция недоступна'}, status_code=403)

    user = await get_user_from_session(email)
    if not user:
        return JSONResponse({'ok': False, 'error': 'Пользователь не найден'}, status_code=404)
    uid = user['telegram_id']
    if int(user.get('is_blocked') or 0) == 1:
        return JSONResponse({'ok': False, 'error': 'Аккаунт заблокирован'}, status_code=403)

    form = await request.form()
    method = (form.get('method') or 'yookassa').strip().lower()
    try:
        new_limit = int(form.get('new_limit') or 0)
    except (ValueError, TypeError):
        new_limit = 0
    if new_limit <= 0:
        return JSONResponse({'ok': False, 'error': 'Неверный лимит'}, status_code=400)

    allowed, retry_after = _payment_rate_limit(uid)
    if not allowed:
        logging.warning(f"[PAY-LIMIT] device-upgrade uid={uid} превышен лимит, retry={retry_after}s")
        return JSONResponse(
            {'ok': False, 'error': 'Превышен лимит запросов. Попробуйте позже.'},
            status_code=429,
        )

    user_email = (user.get('email') or '').strip()
    if not user_email or '@' not in user_email or user_email.startswith('tg:'):
        user_email = ''

    scheme = request.headers.get('x-forwarded-proto', request.url.scheme)
    host = request.headers.get('host', request.url.netloc)
    return_url = f"{scheme}://{host}/cabinet?paid=1"

    try:
        async with httpx.AsyncClient(timeout=15) as hc:
            r = await hc.post(
                f'{_BOT_INTERNAL_URL}/api/device-upgrade/create-payment',
                json={
                    'user_id': int(uid),
                    'new_limit': new_limit,
                    'method': method,
                    'return_url': return_url,
                    'email': user_email,
                },
            )
        data = r.json()
    except Exception as e:
        logging.error(f"[DEVICE-UPGRADE] create-payment failed: {e}")
        return JSONResponse({'ok': False, 'error': 'Сервис временно недоступен'}, status_code=502)

    if r.status_code == 200 and data.get('ok') and data.get('redirect'):
        return JSONResponse({'ok': True, 'redirect': data['redirect']})

    reason_texts = {
        'feature_disabled': 'Расширение лимита временно недоступно.',
        'no_active_subscription': 'Нет активной подписки.',
        'too_few_days_left': 'Слишком мало дней до конца подписки.',
        'over_max_limit': 'Превышен максимальный лимит.',
        'already_unlimited': 'У вас уже безлимит по устройствам.',
        'not_an_upgrade': 'Новый лимит должен быть больше текущего.',
        'no_options': 'Нет доступных вариантов.',
    }
    err = reason_texts.get(data.get('reason')) or data.get('error') or 'Не удалось создать платёж'
    return JSONResponse(
        {'ok': False, 'error': err},
        status_code=(r.status_code if r.status_code >= 400 else 400),
    )


@app.post("/pay/{tariff_id}")
async def pay(request: Request, tariff_id: int):
    email = get_session_email(request)
    if not email:
        return JSONResponse({'ok': False, 'error': 'Необходима авторизация'}, status_code=401)

    form = await request.form()
    method = form.get('method', 'yookassa')

    tariff = await db_one("SELECT * FROM tariffs WHERE id = ? AND is_active = 1", (tariff_id,))
    if not tariff:
        return JSONResponse({'ok': False, 'error': 'Тариф не найден'}, status_code=404)

    user = await get_user_from_session(email)
    if not user:
        return JSONResponse({'ok': False, 'error': 'Пользователь не найден'}, status_code=404)

    uid = user['telegram_id']

    # Берём валидный email из записи пользователя (сессия может быть tg:ID)
    user_email = (user.get('email') or '').strip()
    if not user_email or '@' not in user_email or user_email.startswith('tg:'):
        user_email = ''

    # ── Защита от подмены tariff_id ──────────────────────────────────────────
    # Логика синхронна с рендером тарифов в /cabinet:
    #   • Первая покупка → разрешаем любой лимит.
    #   • Подписка > 7 дней до окончания → только свой лимит (как раньше).
    #   • Подписка ≤ 7 дней или истекла → разрешаем равный или БОЛЬШИЙ лимит
    #     (понижение запрещено: клиент уже зафиксировал устройства).
    #   • ∞-безлимит (limit_ip = 0) считается «выше любого числа».
    TARIFF_CHANGE_WINDOW_DAYS = 7
    db_user = user
    if int(db_user.get('is_blocked') or 0) == 1:
        return JSONResponse({'ok': False, 'error': 'Аккаунт заблокирован'}, status_code=403)
    if db_user:
        existing_limit = int(db_user.get('limit_ip') or 0)
        sub_end_str = (db_user.get('subscription_end_date') or '').strip()
        has_sub = bool(sub_end_str)
        tariff_limit = int(tariff.get('limit_ip') or 0)

        # Сколько дней до окончания подписки (отрицательное = уже истекла)
        days_until_end = None
        if has_sub:
            try:
                end_dt = datetime.fromisoformat(sub_end_str.replace('Z', '+00:00'))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                days_until_end = (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
            except Exception:
                days_until_end = None

        is_first_purchase = (not has_sub) or (existing_limit <= 0)
        is_renewal_window_open = (
            (not is_first_purchase)
            and (days_until_end is None or days_until_end <= TARIFF_CHANGE_WINDOW_DAYS)
        )

        # Можно ли купить тариф `tariff_limit` при текущем `existing_limit`
        def _allowed_lim(t_lim: int, user_lim: int) -> bool:
            if user_lim <= 0:    return t_lim <= 0      # юзер на ∞ → только ∞
            if t_lim   <= 0:     return True            # ∞ всегда «выше» числа
            return t_lim >= user_lim                    # равный или больший

        allowed = False
        if is_first_purchase:
            allowed = True
        elif is_renewal_window_open:
            allowed = _allowed_lim(tariff_limit, existing_limit)
        else:
            allowed = (tariff_limit == existing_limit)

        if not allowed:
            logging.warning(
                f"[WEBSITE] Попытка купить недопустимый тариф: user={uid}, "
                f"existing_limit={existing_limit}, tariff_limit={tariff_limit}, "
                f"days_until_end={days_until_end}, window_open={is_renewal_window_open}"
            )
            # Текст ошибки — в зависимости от причины
            if not is_renewal_window_open and not is_first_purchase:
                err_text = 'Сменить лимит устройств можно только при продлении (за 7 дней до окончания подписки или после неё).'
            else:
                err_text = 'Нельзя выбрать тариф с лимитом ниже текущего. Понижение лимита недоступно.'
            return JSONResponse({'ok': False, 'error': err_text}, status_code=403)
    # ─────────────────────────────────────────────────────────────────────────

    allowed, retry_after = _payment_rate_limit(uid)
    if not allowed:
        logging.warning(f"[PAY-LIMIT] sub uid={uid} превышен лимит, retry={retry_after}s")
        return JSONResponse(
            {'ok': False, 'error': 'Превышен лимит запросов. Попробуйте позже.'},
            status_code=429,
        )

    amount = float(tariff['price'])

    scheme = request.headers.get('x-forwarded-proto', request.url.scheme)
    host = request.headers.get('host', request.url.netloc)
    return_url = f"{scheme}://{host}/cabinet?paid=1"

    result = None
    if method == 'yookassa':
        shop_id, secret = await _get_yookassa_creds()
        if not shop_id or not secret:
            return JSONResponse({'ok': False, 'error': 'Оплата временно недоступна.'}, status_code=503)
        result = await create_yookassa_payment_shared(
            shop_id=shop_id, secret_key=secret,
            amount=amount, currency=tariff.get('currency', 'RUB'),
            tariff=tariff, user_id=uid, return_url=return_url,
            registration_type='site', email=user_email,
        )
    elif method == 'platega':
        merchant_id, api_secret = await _get_platega_creds()
        if not merchant_id or not api_secret:
            return JSONResponse({'ok': False, 'error': 'Оплата временно недоступна.'}, status_code=503)
        result = await create_platega_payment_shared(
            merchant_id=merchant_id, api_secret=api_secret,
            amount=amount, tariff=tariff, user_id=uid, return_url=return_url,
            registration_type='site', email=user_email,
        )
    elif method == 'yoomoney':
        account, ym_token, ym_notif = await _get_yoomoney_creds()
        # ym_token не обязателен (см. _get_yoomoney_creds).
        if not account or not ym_notif:
            return JSONResponse({'ok': False, 'error': 'Оплата временно недоступна.'}, status_code=503)
        ym_payment_id = f"YOOMONEY_{int(time.time())}_{uid}_{tariff_id}"
        ym_meta = {
            "telegram_user_id": uid,
            "user_id": uid,                       # webhook читает meta['user_id']
            "subscription_days": int(tariff.get('days') or 0),
            "days": int(tariff.get('days') or 0),
            "limit_ip": int(tariff.get('limit_ip') or 0),
            "price": amount,
            "amount": amount,                     # webhook читает meta['amount']
            "currency": tariff.get('currency', 'RUB'),
            "tariff_id": tariff_id,
            "registration_type": "site",
            "payment_method": "YooMoney",
        }
        url = await create_yoomoney_quickpay(
            account=account,
            telegram_id=uid,
            payment_id=ym_payment_id,
            amount=amount,
            currency=tariff.get('currency', 'RUB'),
            target_text=f"Подписка {tariff.get('name', '')}".strip(),
            metadata=ym_meta,
        )
        result = (ym_payment_id, url) if url else None
    elif method == 'wata':
        wata_token, wata_pid = await _get_wata_creds()
        if not wata_token:
            return JSONResponse({'ok': False, 'error': 'Оплата временно недоступна.'}, status_code=503)
        result = await create_wata_payment_shared(
            wata_token,
            amount=amount,
            tariff=tariff,
            user_id=uid,
            return_url=return_url,
            registration_type='site',
            email=user_email,
            currency=tariff.get('currency', 'RUB'),
            terminal_public_id=wata_pid,
        )
    else:
        return JSONResponse({'ok': False, 'error': 'Неизвестный метод оплаты'}, status_code=400)

    url = result[1] if result else None
    if not url:
        return JSONResponse({'ok': False, 'error': 'Ошибка оплаты. Попробуйте позже.'}, status_code=500)

    return JSONResponse({'ok': True, 'redirect': url})


# -- Мои устройства -----------------------------------------------------------
@app.delete("/api/my-devices/{hwid}")
async def api_delete_device(hwid: str, request: Request):
    """Удаляет устройство пользователя по HWID. UUID берётся из сессии — пользователь может удалять только свои устройства."""
    if not _DEVICES_ENABLED:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    email = get_session_email(request)
    if not email:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

    if not hwid or len(hwid) < 4 or len(hwid) > 128:
        return JSONResponse({"ok": False, "error": "Invalid hwid"}, status_code=400)

    user = await get_user_from_session(email)
    if not user:
        return JSONResponse({"ok": False, "error": "User not found"}, status_code=404)

    uuid = (user.get("xui_client_uuid") or user.get("remnawave_short_uuid") or "").strip()
    if not uuid:
        return JSONResponse({"ok": False, "error": "No subscription"}, status_code=400)

    try:
        _xuiweb_base = (
            os.getenv("XUIWEB_INTERNAL_URL", "").strip()
            or (await get_setting("xuiweb_internal_url", "")).strip()
            or "http://127.0.0.1:8282"
        ).rstrip("/")
        async with httpx.AsyncClient(timeout=6, verify=False) as client:
            resp = await client.delete(f"{_xuiweb_base}/devices/{uuid}/{hwid}")
        if resp.status_code == 200:
            return JSONResponse({"ok": True})
        if resp.status_code == 404:
            return JSONResponse({"ok": False, "error": "Device not found"}, status_code=404)
        return JSONResponse({"ok": False, "error": f"xuiweb {resp.status_code}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/my-devices/{hwid}/name")
async def api_rename_device(hwid: str, request: Request):
    """Задаёт пользовательское имя устройству. UUID берётся из сессии — только свои устройства."""
    if not _DEVICES_ENABLED:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    email = get_session_email(request)
    if not email:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

    if not hwid or len(hwid) < 4 or len(hwid) > 128:
        return JSONResponse({"ok": False, "error": "Invalid hwid"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        body = {}
    custom_name = (body or {}).get("custom_name", "") if isinstance(body, dict) else ""

    user = await get_user_from_session(email)
    if not user:
        return JSONResponse({"ok": False, "error": "User not found"}, status_code=404)

    uuid = (user.get("xui_client_uuid") or user.get("remnawave_short_uuid") or "").strip()
    if not uuid:
        return JSONResponse({"ok": False, "error": "No subscription"}, status_code=400)

    try:
        _xuiweb_base = (
            os.getenv("XUIWEB_INTERNAL_URL", "").strip()
            or (await get_setting("xuiweb_internal_url", "")).strip()
            or "http://127.0.0.1:8282"
        ).rstrip("/")
        async with httpx.AsyncClient(timeout=6, verify=False) as client:
            resp = await client.post(
                f"{_xuiweb_base}/devices/{uuid}/{hwid}/name",
                json={"custom_name": custom_name},
            )
        if resp.status_code == 200:
            data = resp.json()
            return JSONResponse({"ok": True, "custom_name": data.get("custom_name", "")})
        if resp.status_code == 404:
            return JSONResponse({"ok": False, "error": "Device not found"}, status_code=404)
        return JSONResponse({"ok": False, "error": f"xuiweb {resp.status_code}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/my-devices")
async def api_my_devices(request: Request):
    """Проксирует запрос к xuiweb /devices/{uuid} (доступно только локально)."""
    if not _DEVICES_ENABLED:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    email = get_session_email(request)
    if not email:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

    user = await get_user_from_session(email)
    if not user:
        return JSONResponse({"ok": False, "error": "User not found"}, status_code=404)

    uuid = (user.get("xui_client_uuid") or user.get("remnawave_short_uuid") or "").strip()
    if not uuid:
        return JSONResponse({"ok": True, "devices": [], "count": 0})

    try:
        _xuiweb_base = (
            os.getenv("XUIWEB_INTERNAL_URL", "").strip()
            or (await get_setting("xuiweb_internal_url", "")).strip()
            or "http://127.0.0.1:8282"
        ).rstrip("/")
        async with httpx.AsyncClient(timeout=6, verify=False) as client:
            resp = await client.get(f"{_xuiweb_base}/devices/{uuid}")
        if resp.status_code == 200:
            data = resp.json()
            # Убираем ip_address — пользователю не нужен, не выводится на фронте
            devices = [
                {k: v for k, v in d.items() if k != 'ip_address'}
                for d in data.get("devices", [])
            ]
            return JSONResponse({"ok": True, "devices": devices, "count": data.get("count", 0)})
        return JSONResponse({"ok": False, "error": f"xuiweb {resp.status_code}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
