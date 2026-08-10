import asyncio
import os
import re
import base64
import hashlib
import json
import logging
import random
import time
from urllib.parse import quote, quote_plus, unquote, urlparse
from io import BytesIO
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional
import httpx
import qrcode
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

# Импортируем модули кэширования и защиты
from cache.cache_manager import (
    init_cache_manager,
    close_cache_manager,
    get_subscription_cache as get_cache,
    save_subscription_cache as save_cache,
    cleanup_expired_cache as cleanup_cache
)
from cache.redis_cache import is_available as redis_is_available
from security.rate_limiter import (
    init_rate_limiter,
    rate_limit_middleware
)
from security.ip_filter import (
    init_ip_filter,
    check_ip_middleware
)
from security.uuid_validator import is_valid_uuid, validate_uuid
from security.debounce import (
    init_debounce,
    check_debounce
)


async def _log_device_to_api(uuid: str, client_hwid: str, client_os: str, client_os_ver: str,
                              client_model: str, user_agent: str, client_ip: str, response_size: int):
    """Fire-and-forget: отправляет данные устройства в xuiweb для записи в subscription_access.db (при ответе из кэша)."""
    if not client_hwid and (not user_agent or is_browser(user_agent)):
        return
    try:
        headers = {
            'Content-Type': 'application/json',
            'X-Forwarded-HWID': client_hwid or '',
            'X-Forwarded-Device-OS': client_os or '',
            'X-Forwarded-Ver-OS': client_os_ver or '',
            'X-Forwarded-Device-Model': client_model or '',
            'X-Forwarded-User-Agent': user_agent or '',
            'X-Forwarded-For': client_ip or '',
        }
        body = json.dumps({'response_size': response_size}).encode('utf-8')
        async with httpx.AsyncClient(timeout=5.0) as c:
            await c.post(f"{API_BASE_URL}/api/sub/{uuid}/log-device", headers=headers, content=body)
    except Exception as e:
        logger.debug(f"Не удалось отправить log-device для {uuid}: {e}")


# Загружаем переменные окружения из .env
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Конфигурация из .env (загружаем сразу для использования в lifespan и middleware)
API_BASE_URL = os.getenv("API_BASE_URL", "https://example.com").rstrip('/')
API_TIMEOUT = float(os.getenv("API_TIMEOUT", "20"))  # Таймаут запроса к xuiweb (сек), xuiweb может отвечать до 15+ сек

# Подгружать ли настройки subpage из веб-админки бота при старте
# (через xuiweb endpoint /api/settings/info). Если false — работаем
# строго по этому .env, в сеть на старте не ходим.
API_CUSTOM_SETTINGS = os.getenv("API_CUSTOM_SETTINGS", "false").lower() in ("true", "1", "yes", "on")

# Как часто (в секундах) subpage-воркеры перечитывают конфиг из веб-админки
# (через xuiweb /api/settings/info) без рестарта. Работает только при
# API_CUSTOM_SETTINGS=true. 0 или отрицательное — отключить периодический опрос
# (тянем конфиг только один раз на старте). Значения <5с поднимаются до 5с,
# чтобы не долбить xuiweb слишком часто.
try:
    REMOTE_CFG_REFRESH_SEC = int(os.getenv("REMOTE_CFG_REFRESH_SEC", "30"))
except ValueError:
    REMOTE_CFG_REFRESH_SEC = 30
if 0 < REMOTE_CFG_REFRESH_SEC < 5:
    REMOTE_CFG_REFRESH_SEC = 5

PROJECT_NAME = os.getenv("PROJECT_NAME", "VPN")
SUPPORT_URL = os.getenv("SUPPORT_URL", "")
WEBSITE_URL = os.getenv("WEBSITE_URL", "")
ANNOUNCE_TEXT = os.getenv("ANNOUNCE_TEXT", "")

# Настройки Redis из .env
USE_REDIS = os.getenv("USE_REDIS", "false").lower() in ("true", "1", "yes", "on")
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None) or None
REDIS_CACHE_TTL = int(os.getenv("REDIS_CACHE_TTL", "300"))  # 5 минут по умолчанию

# Настройки защиты
ENABLE_SECURITY = os.getenv("ENABLE_SECURITY", "false").lower() in ("true", "1", "yes", "on")

# Принудительно использовать https для URL подписки (копирование, авто-добавление, QR).
# Полезно когда reverse proxy не передаёт X-Forwarded-Proto.
SUBSCRIPTION_FORCE_HTTPS = os.getenv("SUBSCRIPTION_FORCE_HTTPS", "false").lower() in ("true", "1", "yes", "on")

# Требовать HWID от VPN-клиентов. Если true — клиент без X-HWID получает заглушку.
# Браузеры не затрагиваются (они никогда не шлют HWID).
REQUIRE_HWID = os.getenv("REQUIRE_HWID", "false").lower() in ("true", "1", "yes", "on")
# Фильтр по [PC]/[MOBILE]/… во fragment: .env, либо переопределение из админки при API_CUSTOM_SETTINGS=true
PLATFORM_FILTER_ENABLED = os.getenv("PLATFORM_FILTER_ENABLED", "false").lower() in (
    "true", "1", "yes", "on"
)

# Прокси для запросов к check.happ.su (отправка подписки на TV).
# Если задан — /sendtv/<uid> идёт через прокси. Если пуст — напрямую.
# Формат: http://user:pass@host:port или socks5://user:pass@host:port
HAPP_TV_PROXY = (os.getenv("HAPP_TV_PROXY") or "").strip() or None

# Белый список VPN-приложений по User-Agent (подстроки, через запятую).
# Если НЕ задан (пустой) — подписку получают все приложения.
# Если задан — подписку получают ТОЛЬКО приложения, чей UA содержит одну из подстрок.
# Браузеры не затрагиваются — они всегда получают веб-страницу.
# Пример: ALLOWED_APP_USER_AGENTS=happ,incy
_allowed_raw = os.getenv("ALLOWED_APP_USER_AGENTS", "").strip()
ALLOWED_APP_USER_AGENTS: list[str] = (
    [p.strip().lower() for p in _allowed_raw.split(",") if p.strip()]
    if _allowed_raw else []
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Инициализация при запуске и очистка при остановке приложения"""
    # Startup
    logger.info("Инициализация приложения...")

    # ── Подгружаем remote-конфиг из веб-админки (через xuiweb) ────────────
    # Делаем это ДО логирования whitelist'а/headers, чтобы лог отражал уже
    # финальное состояние (env + переопределения из админки).
    await _fetch_remote_subpage_config()

    # Периодический опрос конфига без рестарта (hot-reload). Запускаем только
    # если ходим за конфигом (API_CUSTOM_SETTINGS=true) и интервал > 0.
    app.state.remote_cfg_task = None
    if API_CUSTOM_SETTINGS and REMOTE_CFG_REFRESH_SEC > 0:
        app.state.remote_cfg_task = asyncio.create_task(_remote_cfg_refresh_loop())
    elif API_CUSTOM_SETTINGS:
        logger.info("[REMOTE_CFG] REMOTE_CFG_REFRESH_SEC<=0 — периодический опрос отключён, конфиг взят только на старте.")

    logger.info(
        f"[PLATFORM_FILTER] enabled={PLATFORM_FILTER_ENABLED} "
        f"(.env или админка при API_CUSTOM_SETTINGS=true)"
    )
    if ALLOWED_APP_USER_AGENTS:
        logger.info(f"[APP_ALLOWLIST] Подписка только для UA: {ALLOWED_APP_USER_AGENTS}")
    else:
        logger.info("[APP_ALLOWLIST] Не задан — подписку получают все приложения")

    # Логируем кастомные тексты заглушек, если заданы
    if STUB_TEXTS_HWID or STUB_TEXTS_UA or STUB_TEXTS_BLOCKED:
        logger.info(
            f"[STUBS] Кастомные тексты заглушек: "
            f"HWID={len(STUB_TEXTS_HWID)} вариантов, "
            f"UA={len(STUB_TEXTS_UA)} вариантов, "
            f"BLOCKED={len(STUB_TEXTS_BLOCKED)} вариантов"
        )

    # Логируем кастомные заголовки из .env
    if EXTRA_SUBSCRIPTION_HEADERS:
        logger.info(f"[RESP_HEADER] Кастомные заголовки подписки ({len(EXTRA_SUBSCRIPTION_HEADERS)} шт.): {EXTRA_SUBSCRIPTION_HEADERS}")
    else:
        logger.info("[RESP_HEADER] Кастомных заголовков не задано (RESP_HEADER_* в .env не найдено)")

    # Инициализация менеджера кэша (Redis)
    try:
        await init_cache_manager(
            use_redis=USE_REDIS,
            redis_host=REDIS_HOST,
            redis_port=REDIS_PORT,
            redis_db=REDIS_DB,
            redis_password=REDIS_PASSWORD,
            cache_ttl=REDIS_CACHE_TTL
        )
        logger.info(f"✅ Менеджер кэша инициализирован (TTL: {REDIS_CACHE_TTL}с, Redis: {'включен' if USE_REDIS else 'выключен'})")
    except Exception as e:
        logger.error(f"Ошибка инициализации менеджера кэша: {e}")
    
    # Инициализация защиты (rate limiting и IP фильтрация)
    if ENABLE_SECURITY:
        try:
            # Инициализация rate limiter
            await init_rate_limiter(
                redis_host=REDIS_HOST,
                redis_port=REDIS_PORT,
                redis_db=REDIS_DB,
                redis_password=REDIS_PASSWORD
            )
            logger.info("Rate limiter инициализирован")
            
            # Инициализация IP фильтра
            await init_ip_filter(
                redis_host=REDIS_HOST,
                redis_port=REDIS_PORT,
                redis_db=REDIS_DB,
                redis_password=REDIS_PASSWORD
            )
            logger.info("IP фильтр инициализирован")
            
            # Инициализация дебаунсинга
            await init_debounce(
                redis_host=REDIS_HOST,
                redis_port=REDIS_PORT,
                redis_db=REDIS_DB,
                redis_password=REDIS_PASSWORD
            )
            logger.info("Дебаунсинг инициализирован")
        except Exception as e:
            logger.error(f"Ошибка инициализации защиты: {e}")
    
    # Очистка устаревшего кэша
    try:
        await cleanup_cache()
        logger.info("Очистка устаревшего кэша выполнена")
    except Exception as e:
        logger.error(f"Ошибка очистки кэша: {e}")
    
    # HTTP-клиент с connection pooling для запросов к API (xuiweb) и внешним сервисам
    try:
        # 50 параллельных запросов к xuiweb при cache miss
        limits = httpx.Limits(
            max_keepalive_connections=60,
            max_connections=100,
            keepalive_expiry=30.0,
        )
        http_client = httpx.AsyncClient(
            timeout=API_TIMEOUT,
            follow_redirects=False,
            limits=limits,
        )
        app.state.http_client = http_client
        logger.info(f"✅ HTTP-клиент с connection pooling инициализирован (timeout={API_TIMEOUT}с, max_connections=100, keepalive=60)")
    except Exception as e:
        logger.error(f"Ошибка инициализации HTTP-клиента: {e}")
        app.state.http_client = None
    
    logger.info("Приложение запущено")
    yield
    
    # Shutdown
    logger.info("Приложение останавливается...")
    try:
        cfg_task = getattr(app.state, 'remote_cfg_task', None)
        if cfg_task is not None:
            cfg_task.cancel()
            try:
                await cfg_task
            except asyncio.CancelledError:
                pass
    except Exception as e:
        logger.error(f"Ошибка остановки цикла опроса конфига: {e}")
    try:
        if getattr(app.state, 'http_client', None):
            await app.state.http_client.aclose()
            logger.info("HTTP-клиент закрыт")
    except Exception as e:
        logger.error(f"Ошибка закрытия HTTP-клиента: {e}")
    try:
        await close_cache_manager()
    except Exception as e:
        logger.error(f"Ошибка закрытия менеджера кэша: {e}")


app = FastAPI(
    title="Subscription Page",
    description="Отдельный сервер для отображения подписок",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan
)

# Добавляем middleware для защиты
if ENABLE_SECURITY:
    # IP фильтрация (проверяется первой)
    app.middleware("http")(check_ip_middleware)
    # Rate limiting (проверяется второй)
    app.middleware("http")(rate_limit_middleware)

script_dir = os.path.dirname(os.path.abspath(__file__))

# Монтируем статические файлы
app.mount("/static", StaticFiles(directory=os.path.join(script_dir, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(script_dir, "templates"))

# Ссылки на приложения. Берутся из .env, а если переменная не задана —
# подставляется актуальная ссылка по умолчанию (как в env.example), чтобы
# UI не оказался с «мёртвыми» кнопками после обновления subpage.
# Если же пользователь явно прописал LINK_X= с пустым значением — далее
# в шаблоне сработает `or "#"` и кнопка станет no-op.
LINK_IOS = os.getenv("LINK_IOS", "https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973")
LINK_IOS_GLOBAL = os.getenv("LINK_IOS_GLOBAL", "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215")
LINK_ANDROID = os.getenv("LINK_ANDROID", "https://play.google.com/store/apps/details?id=com.happproxy")
LINK_ANDROID_APK = os.getenv("LINK_ANDROID_APK", "https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk")
LINK_WINDOWS = os.getenv("LINK_WINDOWS", "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe")
LINK_MAC = os.getenv("LINK_MAC", "https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973")
LINK_LINUX = os.getenv("LINK_LINUX", "https://github.com/Happ-proxy/happ-desktop/releases/")
LINK_INCY_IOS = os.getenv("LINK_INCY_IOS", "https://apps.apple.com/us/app/incy/id6756943388")
LINK_INCY_ANDROID = os.getenv("LINK_INCY_ANDROID", "https://play.google.com/store/apps/details?id=llc.itdev.incy")
LINK_INCY_ANDROID_APK = os.getenv("LINK_INCY_ANDROID_APK", "https://github.com/INCY-DEV/incy-platforms/releases/latest/download/Incy.apk")
LINK_INCY_WINDOWS = os.getenv("LINK_INCY_WINDOWS", "https://github.com/INCY-DEV/incy-platforms/releases/latest/download/incy-windows-setup.exe")
LINK_INCY_MAC = os.getenv("LINK_INCY_MAC", "https://github.com/INCY-DEV/incy-platforms")
LINK_INCY_LINUX = os.getenv("LINK_INCY_LINUX", "https://github.com/INCY-DEV/incy-platforms")

# Настройки интерфейса
ENABLE_COPY_KEYS = os.getenv("ENABLE_COPY_KEYS", "true").lower() in ("true", "1", "yes", "on")

# Интервал автоматического обновления подписки (в часах)
try:
    UPDATE_INTERVAL_HOURS = int(os.getenv("UPDATE_INTERVAL_HOURS", "6"))
    if UPDATE_INTERVAL_HOURS < 1:
        UPDATE_INTERVAL_HOURS = 6
except (ValueError, TypeError):
    UPDATE_INTERVAL_HOURS = 6

# Кастомные HTTP-заголовки ответа подписки.
# Любая переменная окружения с префиксом RESP_HEADER_ становится заголовком.
# Правило: имя после префикса → lowercase, подчёркивания → дефисы.
# ВАЖНО: имена переменных .env не могут содержать дефисы — пишите подчёркивания.
# Пример: RESP_HEADER_providerid=ABC      →  providerid: ABC
#         RESP_HEADER_hide_settings=1     →  hide-settings: 1
def _load_extra_headers() -> dict:
    prefix = "RESP_HEADER_"
    result = {}
    for key, value in os.environ.items():
        if key.startswith(prefix):
            raw = key[len(prefix):]
            header_name = raw.lower().replace('_', '-').strip('-')
            if header_name and value:
                result[header_name] = value
    return result

EXTRA_SUBSCRIPTION_HEADERS: dict = _load_extra_headers()


# ─────────────────────────────────────────────────────────────────────────
# Remote-конфиг: подгружаем настройки из веб-админки бота через xuiweb.
# ─────────────────────────────────────────────────────────────────────────
# Зачем: чтобы менять PROJECT_NAME/SUPPORT_URL/ссылки/RESP_HEADER_* и т.п.
# из веб-админки, не правя .env и не рестартуя контейнер subpage руками.
#
# Стратегия (как договорились с владельцем):
#   • запрос при старте (lifespan) + периодический фоновый опрос каждые
#     REMOTE_CFG_REFRESH_SEC секунд (hot-reload без рестарта). Каждый воркер
#     опрашивает независимо (pull-модель), поэтому multi-worker и деплой subpage
#     на отдельном сервере поддержаны без Redis. Переприменяем только при
#     реальном изменении конфига (сравниваем хэш);
#   • на каждое поле: API в приоритете, если значение пустое или API упал —
#     остаётся то, что уже взято из .env (фолбэк), кроме headers см. ниже;
#   • если в subpage_config задан непустой блок headers — в ответе подписки
#     используются ТОЛЬКО эти заголовки (RESP_HEADER_* из .env не мержатся).
#
# Заглушки (HWID/UA): админ задаёт список фраз, мы для каждого блок-сценария
# выбираем случайную и упаковываем в безопасный vless-URI с фрагментом.
# Если списки пустые — используется единая дефолтная фраза «Приложение не
# поддерживается» (как сейчас в коде).
# ─────────────────────────────────────────────────────────────────────────

# Списки кастомных текстов заглушек. Заполняются из remote-конфига, иначе пусто.
# Один источник истины с админкой (settings.subpage_config.stubs), но разная
# подача: subpage отдаёт ОДНУ случайную фразу (формат — base64-vless),
# xuiweb отдаёт ВЕСЬ список как массив ссылок (формат — JSON подписки).
STUB_TEXTS_HWID:    list[str] = []
STUB_TEXTS_UA:      list[str] = []
STUB_TEXTS_BLOCKED: list[str] = []

# Дефолтные фразы для каждого reason. Используются когда списки в админке пусты.
_STUB_DEFAULTS = {
    'hwid':    'Приложение не поддерживается',
    'ua':      'Приложение не поддерживается',
    'blocked': 'Вы были заблокированы',
}


def apply_subpage_placeholders(text: str, user: dict, uuid: str | None = None) -> str:
    """Подстановка [Email], [TelegramID], [UUID], [REGTYPE] в шаблоны ANNOUNCE_TEXT и заголовков."""
    if not text:
        return ''
    email = str(user.get('email') or '').strip()
    tg = user.get('telegramId')
    if tg is None:
        tg = user.get('telegram_id')
    tg_str = str(tg).strip() if tg is not None and str(tg).strip() else ''
    client_uuid = str(uuid or user.get('shortUuid') or '').strip()
    reg_type = str(user.get('regType') or '').strip()
    if not reg_type:
        raw_reg = user.get('registration_type')
        reg_type = 'SITE' if str(raw_reg or '').strip().lower() == 'site' else 'TG'
    return (
        text.replace('[Email]', email)
        .replace('[TelegramID]', tg_str)
        .replace('[UUID]', client_uuid)
        .replace('[REGTYPE]', reg_type)
    )


def _decode_subpage_text_escapes(text: str) -> str:
    """Текст из админки/JSON: ``\\n`` → перенос строки (для announce и кастомных заголовков)."""
    if not text:
        return ''
    return (
        text.replace('\\r\\n', '\n')
        .replace('\\n', '\n')
        .replace('\\r', '\n')
    )


def resolve_subpage_text(text: str, user: dict, uuid: str | None = None) -> str:
    """Плейсхолдеры + escape-последовательности для отображаемого текста."""
    return _decode_subpage_text_escapes(apply_subpage_placeholders(text, user, uuid))


def resolve_subpage_url(url: str, user: dict | None, uuid: str | None = None) -> str:
    """Плейсхолдеры ([UUID]/[TelegramID]/[Email]/[REGTYPE]) в SUPPORT_URL/WEBSITE_URL.

    Позволяет задать, например, WEBSITE_URL = ``https://.../sub/[UUID]/info`` —
    ссылка на info-страницу конкретного клиента.
    """
    if not url:
        return url
    return apply_subpage_placeholders(url, user or {}, uuid)


def _build_unsupported_link(text: str, default: str = 'Приложение не поддерживается') -> str:
    """Собирает безопасную vless-заглушку с заданным текстом во fragment.

    Telegram/VPN-приложения парсят фрагмент как «имя профиля» и показывают его
    пользователю. Внутри — фейковый IP/UUID, чтобы соединение никогда не
    встало.
    """
    safe = (text or default).strip() or default
    return (
        'vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1'
        '?security=none&type=tcp#' + quote(safe, safe='')
    )


def pick_unsupported_stub_b64(reason: str) -> str:
    """Возвращает base64-закодированную vless-заглушку для одной из ситуаций:
    `reason` ∈ {'hwid', 'ua', 'blocked'}. Если для reason задано несколько фраз —
    выбираем случайную (защита от детектирования по фиксированной строке).
    """
    pools = {
        'hwid':    STUB_TEXTS_HWID,
        'ua':      STUB_TEXTS_UA,
        'blocked': STUB_TEXTS_BLOCKED,
    }
    pool = pools.get(reason) or []
    default = _STUB_DEFAULTS.get(reason, 'Приложение не поддерживается')
    text = random.choice(pool) if pool else default
    link = _build_unsupported_link(text, default=default)
    return base64.b64encode(link.encode('utf-8')).decode('utf-8')


def _apply_remote_subpage_config(cfg: dict) -> None:
    """Перезаписывает наши module-level переменные значениями из cfg.

    Применяется только если соответствующее поле в cfg непустое — иначе
    сохраняется значение из .env. Это даёт админу возможность переопределять
    что нужно, оставляя в .env остальное как есть.
    """
    global PROJECT_NAME, SUPPORT_URL, WEBSITE_URL, ANNOUNCE_TEXT
    global ENABLE_COPY_KEYS, UPDATE_INTERVAL_HOURS, REQUIRE_HWID, PLATFORM_FILTER_ENABLED
    global ALLOWED_APP_USER_AGENTS
    global LINK_IOS, LINK_IOS_GLOBAL, LINK_ANDROID, LINK_ANDROID_APK
    global LINK_WINDOWS, LINK_MAC, LINK_LINUX
    global LINK_INCY_IOS, LINK_INCY_ANDROID, LINK_INCY_ANDROID_APK
    global LINK_INCY_WINDOWS, LINK_INCY_MAC, LINK_INCY_LINUX
    global EXTRA_SUBSCRIPTION_HEADERS, STUB_TEXTS_HWID, STUB_TEXTS_UA, STUB_TEXTS_BLOCKED

    main = (cfg or {}).get('main') or {}
    links = (cfg or {}).get('links') or {}
    headers = (cfg or {}).get('headers') or {}
    stubs = (cfg or {}).get('stubs') or {}

    def _str(v): return str(v).strip() if v not in (None, '') else ''
    def _override_str(name, current):
        v = _str(main.get(name))
        return v if v else current

    PROJECT_NAME   = _override_str('PROJECT_NAME',   PROJECT_NAME)
    SUPPORT_URL    = _override_str('SUPPORT_URL',    SUPPORT_URL)
    WEBSITE_URL    = _override_str('WEBSITE_URL',    WEBSITE_URL)
    ANNOUNCE_TEXT  = _override_str('ANNOUNCE_TEXT',  ANNOUNCE_TEXT)

    if 'ENABLE_COPY_KEYS' in main:
        ENABLE_COPY_KEYS = bool(main.get('ENABLE_COPY_KEYS'))
    if 'REQUIRE_HWID' in main:
        REQUIRE_HWID = bool(main.get('REQUIRE_HWID'))
    if 'PLATFORM_FILTER_ENABLED' in main:
        PLATFORM_FILTER_ENABLED = bool(main.get('PLATFORM_FILTER_ENABLED'))

    try:
        hours = int(main.get('UPDATE_INTERVAL_HOURS') or 0)
        if hours >= 1:
            UPDATE_INTERVAL_HOURS = hours
    except (ValueError, TypeError):
        pass

    # SUBSCRIPTION_THEME читается через os.getenv() при каждом запросе,
    # поэтому переопределяем через os.environ — иначе изменение не подхватится.
    theme = _str(main.get('SUBSCRIPTION_THEME'))
    if theme:
        os.environ['SUBSCRIPTION_THEME'] = theme

    ua_list = main.get('ALLOWED_APP_USER_AGENTS')
    if isinstance(ua_list, list) and ua_list:
        ALLOWED_APP_USER_AGENTS = [str(x).strip().lower() for x in ua_list if str(x).strip()]
    elif isinstance(ua_list, str) and ua_list.strip():
        ALLOWED_APP_USER_AGENTS = [p.strip().lower() for p in ua_list.split(',') if p.strip()]

    # Ссылки: переопределяем по одной, только непустые.
    if isinstance(links, dict):
        for var_name in (
            'LINK_IOS', 'LINK_IOS_GLOBAL', 'LINK_ANDROID', 'LINK_ANDROID_APK',
            'LINK_WINDOWS', 'LINK_MAC', 'LINK_LINUX',
            'LINK_INCY_IOS', 'LINK_INCY_ANDROID', 'LINK_INCY_ANDROID_APK',
            'LINK_INCY_WINDOWS', 'LINK_INCY_MAC', 'LINK_INCY_LINUX',
        ):
            v = _str(links.get(var_name))
            if v:
                globals()[var_name] = v

    # HTTP-заголовки ответа подписки: только из админки (без слияния с .env).
    if isinstance(headers, dict) and headers:
        only_api: dict = {}
        for raw_name, raw_value in headers.items():
            name = str(raw_name or '').strip().lower().replace('_', '-').strip('-')
            value = str(raw_value or '').strip()
            if name and value:
                only_api[name] = value
        EXTRA_SUBSCRIPTION_HEADERS = only_api

    # Заглушки. BLOCKED тоже забираем — здесь, в subpage, она используется в
    # /sub/{uuid} когда xuiweb отдал данные с userStatus=BLOCKED/DISABLED.
    # В отличие от xuiweb (массив ссылок в JSON), subpage берёт ОДНУ случайную
    # фразу и отдаёт её base64-vless-стрингой.
    h = stubs.get('HWID') if isinstance(stubs, dict) else None
    u = stubs.get('UA') if isinstance(stubs, dict) else None
    b = stubs.get('BLOCKED') if isinstance(stubs, dict) else None
    if isinstance(h, list):
        STUB_TEXTS_HWID = [str(x).strip() for x in h if str(x).strip()]
    if isinstance(u, list):
        STUB_TEXTS_UA = [str(x).strip() for x in u if str(x).strip()]
    if isinstance(b, list):
        STUB_TEXTS_BLOCKED = [str(x).strip() for x in b if str(x).strip()]


# Хэш последнего успешно применённого remote-конфига. Позволяет при
# периодическом опросе (см. _remote_cfg_refresh_loop) не переприменять и не
# логировать одно и то же по кругу — трогаем глобалы только когда админ реально
# что-то поменял в веб-админке.
_last_remote_cfg_hash: str | None = None


async def _fetch_remote_subpage_config(quiet: bool = False) -> None:
    """Тянет {API_BASE_URL}/api/settings/info и применяет к module-level переменным.

    Решение «ходить за конфигом или нет» принимаем здесь, в subpage,
    на основании флага API_CUSTOM_SETTINGS из .env. xuiweb всегда отдаёт
    текущий конфиг (если он сохранён в админке), без своих собственных
    тумблеров — это упрощает архитектуру и избавляет от двойного контроля.

    Тихий путь: любая ошибка/таймаут/404 — просто работаем по .env. Логируем
    INFO/WARNING, но падать на этом нельзя (subpage важнее всего, чтобы он
    отдавал подписки даже если бот лежит).

    quiet=True используется фоновым опросом: понижает уровень «рутинных»
    сообщений до DEBUG, чтобы не спамить логи каждые N секунд. Реальное
    изменение конфига логируется на INFO в любом случае.
    """
    global _last_remote_cfg_hash

    if not API_CUSTOM_SETTINGS:
        if quiet:
            logger.debug("[REMOTE_CFG] API_CUSTOM_SETTINGS=false — конфиг из веб-админки не запрашиваем, работаем по .env.")
        else:
            logger.info("[REMOTE_CFG] API_CUSTOM_SETTINGS=false — конфиг из веб-админки не запрашиваем, работаем по .env.")
        return

    url = f"{API_BASE_URL}/api/settings/info"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
        if resp.status_code == 404:
            (logger.debug if quiet else logger.info)(
                f"[REMOTE_CFG] {url} вернул 404 — endpoint недоступен или конфиг ещё не сохранён в админке. Используем .env."
            )
            return
        if resp.status_code != 200:
            logger.warning(f"[REMOTE_CFG] GET {url} вернул HTTP {resp.status_code}; используем .env.")
            return
        data = resp.json() or {}
        cfg = data.get('cfg') or data
        if not isinstance(cfg, dict):
            logger.warning("[REMOTE_CFG] cfg не dict — используем .env.")
            return

        # Пропускаем переприменение, если конфиг не менялся с прошлого раза.
        try:
            cfg_hash = hashlib.md5(
                json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest()
        except Exception:
            cfg_hash = None
        if cfg_hash is not None and cfg_hash == _last_remote_cfg_hash:
            logger.debug("[REMOTE_CFG] Конфиг не изменился — пропускаем переприменение.")
            return

        _apply_remote_subpage_config(cfg)
        _last_remote_cfg_hash = cfg_hash
        applied_summary = {
            'project': PROJECT_NAME,
            'theme_env': os.getenv('SUBSCRIPTION_THEME', ''),
            'allowed_uas': ALLOWED_APP_USER_AGENTS,
            'require_hwid': REQUIRE_HWID,
            'links_overridden': sum(1 for k in (cfg.get('links') or {}).values() if str(k).strip()),
            'headers_overridden': len(cfg.get('headers') or {}),
            'stubs_hwid': len(STUB_TEXTS_HWID),
            'stubs_ua': len(STUB_TEXTS_UA),
            'stubs_blocked': len(STUB_TEXTS_BLOCKED),
        }
        logger.info(f"[REMOTE_CFG] ✅ Применён remote-конфиг: {applied_summary}")
    except (httpx.RequestError, asyncio.TimeoutError) as e:
        logger.warning(f"[REMOTE_CFG] Не удалось получить конфиг ({type(e).__name__}: {e}); используем .env.")
    except Exception as e:
        logger.error(f"[REMOTE_CFG] Неожиданная ошибка: {e}", exc_info=True)


async def _remote_cfg_refresh_loop() -> None:
    """Фоновый цикл: каждые REMOTE_CFG_REFRESH_SEC секунд перечитывает конфиг из
    веб-админки (через xuiweb) и применяет изменения без рестарта.

    Работает в каждом воркере независимо (pull-модель), поэтому multi-worker и
    распределённый деплой (subpage на отдельном сервере) поддерживаются без
    Redis/pub-sub. Опрос — «тихий» (quiet=True): в лог попадает только реальное
    изменение конфига, а не каждая итерация.
    """
    logger.info(
        f"[REMOTE_CFG] Периодический опрос конфига включён: каждые {REMOTE_CFG_REFRESH_SEC}с."
    )
    while True:
        try:
            await asyncio.sleep(REMOTE_CFG_REFRESH_SEC)
            await _fetch_remote_subpage_config(quiet=True)
        except asyncio.CancelledError:
            logger.info("[REMOTE_CFG] Периодический опрос конфига остановлен.")
            raise
        except Exception as e:
            # Никогда не даём фоновому циклу упасть — просто ждём следующей итерации.
            logger.warning(f"[REMOTE_CFG] Ошибка в цикле опроса конфига: {type(e).__name__}: {e}")


def _get_forwarded_scheme_host(request: Request) -> tuple[str, str]:
    """Извлекает схему и хост с учётом X-Forwarded-Proto/X-Forwarded-Host/X-Forwarded-Ssl.
    За reverse proxy (nginx) request.url может содержать http вместо https."""
    scheme = request.url.scheme
    host = request.url.netloc
    forwarded_proto = request.headers.get("x-forwarded-proto", "").strip().lower()
    forwarded_ssl = request.headers.get("x-forwarded-ssl", "").strip().lower()
    forwarded_host = request.headers.get("x-forwarded-host", "").strip()
    if forwarded_proto in ("https", "on", "1"):
        scheme = "https"
    elif forwarded_proto == "http":
        scheme = "http"
    elif forwarded_ssl in ("on", "1", "yes", "true"):
        # Некоторые прокси (например старые) передают только X-Forwarded-Ssl
        scheme = "https"
    elif SUBSCRIPTION_FORCE_HTTPS:
        # Если прокси не передаёт заголовки, принудительно https (env SUBSCRIPTION_FORCE_HTTPS=true)
        scheme = "https"
    if forwarded_host:
        host = forwarded_host.split(",")[0].strip()
    return scheme, host


def _get_request_url(request: Request) -> str:
    """Возвращает полный URL запроса с учётом X-Forwarded-Proto/X-Forwarded-Host."""
    scheme, host = _get_forwarded_scheme_host(request)
    url = request.url
    return f"{scheme}://{host}{url.path}" + (f"?{url.query}" if url.query else "")


def is_browser(user_agent: str) -> bool:
    """Определяет, является ли запрос от браузера"""
    if not user_agent:
        return False
    ua_lower = user_agent.lower()

    # Яндекс.Браузер / приложение Яндекса / Алиса работают через WebView, и их UA
    # может содержать подстроку 'happ' (YaApp/YaSearchBrowser и т.п.). Из-за этого
    # страница ошибочно отдавалась как содержимое подписки, а не как HTML-шаблон.
    # Такой UA — всегда браузер.
    yandex_markers = ('yabrowser', 'ya-browser', 'yandex', 'yaapp', 'yasearch')
    if any(m in ua_lower for m in yandex_markers):
        return True

    browser_indicators = ['mozilla', 'chrome', 'safari', 'firefox', 'edge', 'opera', 'msie', 'yabrowser', 'ya-browser']
    app_indicators = [
        'clash',           # Clash, ClashX, Clash for Windows
        'v2ray',          # V2Ray, V2RayN, V2RayNG, V2RayX
        'v2raytun',       # V2RayTun
        'v2rayn',         # V2RayN (Windows)
        'v2rayng',        # V2RayNG (Android)
        'v2box',          # V2Box (iOS)
        'sing-box',       # sing-box
        'shadowrocket',   # ShadowRocket (iOS)
        'happ/',          # Happ — точный маркер клиента (со слэшем: "Happ/1.x"),
                          #        чтобы не ловить подстроку 'happ' в UA браузеров
        'incy',           # INCY
        'hiddify',        # Hiddify, Hiddify Next
        'shadowsocks',    # Shadowsocks
        'nekoray',        # Nekoray
        'nekobox',        # Nekobox
        'mihomo',         # mihomo (Clash fork)
        'clash-verge',    # Clash Verge
        'clash-meta'      # Clash Meta
    ]
    
    # Если есть индикаторы приложений, это не браузер
    if any(indicator in ua_lower for indicator in app_indicators):
        return False
    
    # Если есть индикаторы браузера, это браузер
    return any(indicator in ua_lower for indicator in browser_indicators)


def is_xray_json_client_ua(user_agent: str) -> bool:
    """Клиенты, которым xuiweb отдаёт xrayConfig (Happ / INCY / v2raytun)."""
    ua_lower = (user_agent or '').lower()
    return any(marker in ua_lower for marker in ('happ', 'incy', 'v2raytun'))


# ── Фильтрация ссылок по меткам [PC]/[MOBILE]/[ROUTER]/[TV] во fragment ─────────
# Классификация только по X-Device-OS; без заголовка — полный список (все теги в имени
# при включённом фильтре всё равно вырезаются из ответа для чистого имени).
_PLATFORM_TAG_RE = re.compile(r"\[(pc|mobile|router|tv)\]", re.IGNORECASE)


def classify_platform_from_x_device_os(client_os: str) -> Optional[str]:
    """Возвращает 'pc' | 'mobile' | 'router' | 'tv' или None (все сервера).

    Используем только значение из заголовка X-Device-OS (как в devices.db).
    Пустая строка или нераспознанное значение → None.
    """
    s = (client_os or "").strip().lower()
    if not s:
        return None
    # TV раньше Android (androidtv содержит "android")
    if "androidtv" in s or "android tv" in s or "tvos" in s:
        return "tv"
    if "ipad" in s:
        return "mobile"
    if "ios" in s:
        return "mobile"
    if "android" in s:
        return "mobile"
    if "windows" in s:
        return "pc"
    if "mac" in s or "darwin" in s:
        return "pc"
    if "linux" in s:
        return "pc"
    if any(r in s for r in ("openwrt", "keenetic", "asuswrt", "merlin", "padavan")):
        return "router"
    return None


def _strip_platform_tags(name: str) -> str:
    t = _PLATFORM_TAG_RE.sub("", name or "")
    return re.sub(r"\s{2,}", " ", t).strip()


def filter_subscription_links_by_platform(
    links: list[str],
    platform: Optional[str],
) -> list[str]:
    """Убирает ссылки с неподходящими метками; метки всегда вырезаются из fragment."""
    out: list[str] = []
    for raw in links or []:
        link = (raw or "").strip()
        if not link:
            continue
        if "#" not in link:
            out.append(link)
            continue
        body, frag_enc = link.split("#", 1)
        name = unquote(frag_enc) if frag_enc else ""
        tags = {m.group(1).lower() for m in _PLATFORM_TAG_RE.finditer(name)}
        clean_name = _strip_platform_tags(name)

        if platform is not None and tags and platform not in tags:
            continue

        if clean_name:
            out.append(f"{body}#{quote(clean_name)}")
        else:
            out.append(body)
    return out


# Разрешённые темы для пути /sub/{uuid}/{theme}
SUBSCRIPTION_PATH_THEMES = frozenset(('android', 'apple', 'router'))


@app.get("/sub/{uuid}/info", response_class=Response)
async def get_subscription_info(uuid: str, request: Request):
    """Страница сводки о подписке без ключей подключения."""
    if not is_valid_uuid(uuid):
        return templates.TemplateResponse(request=request, name="error.html", context={
            "project_name": PROJECT_NAME,
            "support_url": SUPPORT_URL,
            "error_message": "Неверный формат подписки",
        }, status_code=400)

    user_agent = request.headers.get('user-agent', '')
    if not is_browser(user_agent):
        return RedirectResponse(url=f"/sub/{uuid}", status_code=302)

    try:
        client = getattr(request.app.state, 'http_client', None)
        if not client:
            client = httpx.AsyncClient(timeout=10.0)
            close_client = True
        else:
            close_client = False
        try:
            resp = await client.get(f"{API_BASE_URL}/api/sub/{uuid}/info")
        finally:
            if close_client:
                await client.aclose()
    except Exception as e:
        logger.error(f"Info API error for {uuid}: {e}")
        return templates.TemplateResponse(request=request, name="error.html", context={
            "project_name": PROJECT_NAME,
            "support_url": SUPPORT_URL,
            "error_message": "Не удалось загрузить информацию. Попробуйте позже.",
        }, status_code=503)

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Info page disabled")
    if resp.status_code != 200:
        return templates.TemplateResponse(request=request, name="error.html", context={
            "project_name": PROJECT_NAME,
            "support_url": SUPPORT_URL,
            "error_message": "Подписка не найдена или временно недоступна.",
        }, status_code=resp.status_code if resp.status_code >= 400 else 503)

    try:
        info_data = resp.json()
    except Exception:
        info_data = {}

    if not info_data.get('isFound'):
        return templates.TemplateResponse(request=request, name="error.html", context={
            "project_name": PROJECT_NAME,
            "support_url": SUPPORT_URL,
            "error_message": "Подписка не найдена",
        }, status_code=404)

    if info_data.get('userStatus') == 'BLOCKED':
        return templates.TemplateResponse(request=request, name="blocked.html", context={
            "project_name": info_data.get('meta', {}).get('projectName') or PROJECT_NAME,
            "support_url": info_data.get('meta', {}).get('supportUrl') or SUPPORT_URL,
            "support_link": info_data.get('meta', {}).get('supportUrl') or SUPPORT_URL,
            "website_url": resolve_subpage_url(WEBSITE_URL, info_data.get('user') or {}, uuid),
        })

    meta = info_data.get('meta') or {}
    scheme, host = _get_forwarded_scheme_host(request)
    ctx = {
        "project_name": meta.get('projectName') or PROJECT_NAME,
        "support_url": meta.get('supportUrl') or SUPPORT_URL or "#",
        "info": info_data,
        "connect_url": meta.get('connectUrl') or f"{scheme}://{host}/sub/{uuid}",
    }
    return templates.TemplateResponse(request=request, name="info.html", context=ctx)


@app.get("/sub/{uuid}/{theme}", response_class=Response)
async def get_subscription_themed(uuid: str, theme: str, request: Request):
    """Страница подписки по явному пути: /sub/uuid/android, /sub/uuid/router, и т.д."""
    theme_lower = theme.lower()
    if theme_lower not in SUBSCRIPTION_PATH_THEMES:
        raise HTTPException(status_code=404, detail="Unknown theme")
    request.state._subscription_theme_override = theme_lower
    return await get_subscription(uuid, request)


@app.get("/sub/{uuid}", response_class=Response)
async def get_subscription(uuid: str, request: Request):
    """Основной endpoint для получения подписки"""
    # Валидация UUID для защиты от брутфорса
    if not is_valid_uuid(uuid):
        logger.warning(f"Невалидный UUID получен: {uuid}")
        user_agent = request.headers.get('user-agent', '')
        if is_browser(user_agent):
            return templates.TemplateResponse(request=request, name="error.html", context={
                "project_name": PROJECT_NAME,
                "support_url": SUPPORT_URL,
                "error_message": "Неверный формат подписки"
            }, status_code=400)
        else:
            # Для приложений (Happ) всегда возвращаем HTTP 200, чтобы не показывать "ошибку сети"
            # Возвращаем пустую подписку с сообщением об обновлении
            error_link = "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1?security=none&type=tcp#Обновите_ещё_раз"
            error_base64 = base64.b64encode(error_link.encode('utf-8')).decode('utf-8')
            return Response(content=error_base64, media_type='text/plain', status_code=200)
    
    user_agent = request.headers.get('user-agent', '')
    
    # Извлекаем данные устройства от клиента для передачи в бот
    client_hwid = request.headers.get('x-hwid', '') or request.headers.get('X-HWID', '')
    client_os = request.headers.get('x-device-os', '') or request.headers.get('X-Device-OS', '')
    client_os_ver = request.headers.get('x-ver-os', '') or request.headers.get('X-Ver-OS', '')
    client_model = request.headers.get('x-device-model', '') or request.headers.get('X-Device-Model', '')
    client_ip = request.client.host if request.client else ''
    
    # Определяем тип клиента для логирования (браузер проверяем первым, иначе
    # UA Яндекса/Алисы с подстрокой 'happ' ошибочно подписывался как Happ)
    is_browser_client = is_browser(user_agent)
    is_happ_client = (not is_browser_client) and ('happ/' in user_agent.lower() if user_agent else False)
    client_type = "Happ" if is_happ_client else ("Браузер" if is_browser_client else "Приложение")
    
    logger.info(f"📱 Запрос от {client_type} для {uuid}: User-Agent={user_agent[:100]}")
    if client_hwid:
        logger.debug(f"   Устройство: HWID={client_hwid[:20]}..., OS={client_os}, Ver={client_os_ver}, Model={client_model}")

    # ── Проверка обязательности HWID ──────────────────────────────────────────
    # Браузеры не проверяем — они открывают веб-страницу подписки, HWID не шлют.
    if REQUIRE_HWID and not is_browser(user_agent) and not client_hwid:
        logger.info(f"🚫 [{client_type}] HWID не передан для {uuid}, UA={user_agent[:80]} — отдаём заглушку (REQUIRE_HWID=true)")
        return Response(content=pick_unsupported_stub_b64('hwid'), media_type="text/plain", status_code=200)

    # ── Белый список приложений (ALLOWED_APP_USER_AGENTS) ─────────────────────
    # Если список задан, пропускаем только приложения из него.
    # Браузеры не затрагиваются — они идут по своей ветке HTML.
    if ALLOWED_APP_USER_AGENTS and not is_browser(user_agent):
        ua_lower = (user_agent or "").lower()
        if not any(pattern in ua_lower for pattern in ALLOWED_APP_USER_AGENTS):
            logger.info(
                f"🚫 [{client_type}] UA не в белом списке ALLOWED_APP_USER_AGENTS "
                f"для {uuid}, UA={user_agent[:80]} — отдаём заглушку"
            )
            return Response(content=pick_unsupported_stub_b64('ua'), media_type="text/plain", status_code=200)
    
    # Сначала проверяем свежий кэш (Redis или SQLite, TTL < REDIS_CACHE_TTL)
    # ВАЖНО: Если Redis доступен и TTL истек, get_cache вернет None, чтобы сделать запрос к API
    # SQLite fallback используется только если Redis недоступен
    sub_data = await get_cache(uuid, use_stale=False)
    cache_source = "новый кэш (Redis/SQLite)" if sub_data else None
    
    # Проверяем дебаунсинг - если запрос на тот же UUID пришел недавно, используем кэш
    should_fetch_from_api = True
    
    if sub_data:
        # Свежий кэш есть - используем его, не делаем запрос к API
        if is_xray_json_client_ua(user_agent) and not sub_data.get('xrayConfig'):
            logger.info(
                f'🔄 [{client_type}] Кэш для {uuid} без xrayConfig — повторный запрос к API '
                f'(кэш мог быть записан из браузера)'
            )
            sub_data = None
            should_fetch_from_api = True
        else:
            links_count = len(sub_data.get('links', []))
            is_found = sub_data.get('isFound', False)
            cache_timestamp = sub_data.get('_cache_timestamp', 'N/A')
            cache_age = "свежий" if sub_data.get('_cache_fresh', False) else "устаревший"
            logger.info(f"✅ [{client_type}] Используем {cache_age} кэш для {uuid} из {cache_source} (isFound: {is_found}, links: {links_count}, timestamp: {cache_timestamp})")
            should_fetch_from_api = False
    elif await check_debounce(uuid):
        # Дебаунсинг активен - запрос был недавно (в течение 2 секунд)
        # ВАЖНО: Проверяем сначала свежий кэш (может быть обновлен после предыдущего запроса к API)
        logger.debug(f"⏱️ [{client_type}] Дебаунсинг активен для {uuid}, проверяем кэш (сначала свежий, потом устаревший)")
        
        # Сначала пробуем свежий кэш
        sub_data = await get_cache(uuid, use_stale=False)
        cache_source_debounce = "Redis (свежий)" if sub_data else None

        # Если свежего кэша нет, пробуем устаревший
        if not sub_data:
            logger.debug(f"Свежего кэша нет для {uuid}, пробуем устаревший")
            sub_data = await get_cache(uuid, use_stale=True)
            cache_source_debounce = "Redis (устаревший)" if sub_data else None
        
        if sub_data:
            if is_xray_json_client_ua(user_agent) and not sub_data.get('xrayConfig'):
                logger.info(
                    f'🔄 [{client_type}] Дебаунс-кэш для {uuid} без xrayConfig — запрос к API'
                )
                sub_data = None
                should_fetch_from_api = True
            else:
                links_count = len(sub_data.get('links', []))
                is_found = sub_data.get('isFound', False)
                cache_timestamp = sub_data.get('_cache_timestamp', 'N/A')
                cache_age = "свежий" if sub_data.get('_cache_fresh', False) else "устаревший"
                logger.info(
                    f"⏱️ [{client_type}] Дебаунсинг активен для {uuid}, используем {cache_age} кэш "
                    f"из {cache_source_debounce} (isFound: {is_found}, links: {links_count}, "
                    f"timestamp: {cache_timestamp})"
                )
                should_fetch_from_api = False
        else:
            # Кэша нет — не делаем запрос к API (дебаунс), отдаём заглушку
            if is_browser(user_agent):
                return templates.TemplateResponse(request=request, name="error.html", context={
                    "project_name": PROJECT_NAME,
                    "support_url": SUPPORT_URL,
                    "error_message": "Подписка временно недоступна. Попробуйте через несколько секунд."
                }, status_code=503)
            else:
                logger.warning(
                    f"Дебаунсинг активен для {uuid}, но кэша нет. "
                    f"Возвращаем пустую подписку для приложения."
                )
                empty_sub = base64.b64encode(b"").decode('utf-8')
                return Response(content=empty_sub, media_type='text/plain', status_code=200)
    
    # Если данных нет в кэше и дебаунсинг не активен, делаем запрос к API
    if should_fetch_from_api and not sub_data:
        logger.info(f"🔄 [{client_type}] Кэш не найден для {uuid}, делаем запрос к API")
        try:
            client = getattr(request.app.state, 'http_client', None)
            if not client:
                client = httpx.AsyncClient(timeout=API_TIMEOUT, follow_redirects=False)
                use_fallback_client = True
            else:
                use_fallback_client = False
            try:
                # Важно: используем /api/sub/{uuid}, а не /sub/{uuid}
                api_url = f"{API_BASE_URL}/api/sub/{uuid}"
                # Добавляем заголовки, чтобы API вернул JSON, а не текстовую подписку
                # Ключевые заголовки: Sec-Fetch-Dest: document и Sec-Fetch-Mode: navigate
                # Это заставляет Caddy вернуть JSON вместо Base64 подписки
                headers = {
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br',
                'User-Agent': (
                    user_agent if user_agent and not is_browser(user_agent) else
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                ),
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',  # Ключевой заголовок!
                'Sec-Fetch-Mode': 'navigate',   # Ключевой заголовок!
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0'
                }
                
                # Передаем данные устройства от клиента в бот для логирования
                # Эти заголовки будут использованы ботом для записи в subscription_access.db
                if client_hwid:
                    headers['X-Forwarded-HWID'] = client_hwid
                if client_os:
                    headers['X-Forwarded-Device-OS'] = client_os
                if client_os_ver:
                    headers['X-Forwarded-Ver-OS'] = client_os_ver
                if client_model:
                    headers['X-Forwarded-Device-Model'] = client_model
                if user_agent:
                    headers['X-Forwarded-User-Agent'] = user_agent  # Оригинальный User-Agent от клиента
                if client_ip:
                    headers['X-Forwarded-For'] = client_ip  # IP адрес клиента
                # Логируем детали запроса к API
                logger.info(f"📤 Запрос к API для {uuid}:")
                logger.info(f"   URL: {api_url}")
                logger.info(f"   Метод: GET")
                logger.info(f"   Заголовки: Accept={headers.get('Accept', 'N/A')[:50]}, User-Agent={headers.get('User-Agent', 'N/A')[:50]}, Sec-Fetch-Dest={headers.get('Sec-Fetch-Dest', 'N/A')}")
                response = await client.get(api_url, headers=headers, timeout=API_TIMEOUT)
                
                # Проверяем редиректы
                if response.status_code in (301, 302, 303, 307, 308):
                    redirect_url = response.headers.get('Location', '')
                    logger.warning(f"Получен редирект {response.status_code} на {redirect_url}")
                    # Если редирект на /sub/ вместо /api/sub/, это проблема
                    if '/sub/' in redirect_url and '/api/sub/' not in redirect_url:
                        logger.error(f"Редирект уводит с /api/sub/ на /sub/. Это причина проблемы!")
                        raise ValueError(f"API редиректит на /sub/ вместо /api/sub/. Проверьте конфигурацию прокси.")
                    # Следуем редиректу вручную
                    if redirect_url.startswith('http'):
                        api_url = redirect_url
                    else:
                        api_url = f"{API_BASE_URL}{redirect_url}"
                    logger.info(f"Следуем редиректу на: {api_url}")
                    response = await client.get(api_url, headers=headers, timeout=API_TIMEOUT)
                
                response.raise_for_status()
                
                # Логируем детали ответа от API
                logger.info(f"📥 Ответ от API для {uuid}:")
                logger.info(f"   Статус: {response.status_code}")
                logger.info(f"   Размер ответа: {len(response.content)} байт")
                
                # Проверяем Content-Type и Content-Encoding
                content_type = response.headers.get('content-type', '').lower()
                content_encoding = response.headers.get('content-encoding', '').lower()
                is_json_content_type = 'application/json' in content_type
                logger.info(f"   Content-Type: {content_type}")
                logger.info(f"   Content-Encoding: {content_encoding if content_encoding else 'нет'}")
                
                # httpx автоматически распаковывает gzip/deflate/brotli (при установленном brotli)
                # Ручная распаковка не нужна — приводит к "decoder failed" на уже распакованном контенте
                response_text = response.text.strip()
                
                # Проверяем, что ответ не пустой
                if not response_text:
                    logger.warning(f"Пустой ответ от API для {uuid}")
                    raise ValueError("API вернул пустой ответ")
                
                # Проверяем, начинается ли ответ с { или [ (признак JSON)
                looks_like_json = response_text.startswith('{') or response_text.startswith('[')
                
                logger.info(f"URL: {api_url}, Content-Type: {content_type}, Начинается с {{ или [: {looks_like_json}, Первые 50 символов: {response_text[:50]}")
                
                # Если ответ не JSON и не похож на JSON, это ошибка
                if not is_json_content_type and not looks_like_json:
                    logger.error(f"API вернул не-JSON ответ для {uuid}. URL: {api_url}, Content-Type: {content_type}, Первые 200 символов: {response_text[:200]}")
                    raise ValueError(f"API вернул не JSON ответ. URL: {api_url}, Content-Type: {content_type}")
                
                # Парсим JSON
                try:
                    if looks_like_json:
                        # Если содержимое похоже на JSON, парсим его независимо от Content-Type
                        sub_data = json.loads(response_text)
                        logger.info(f"✅ Успешно распарсили JSON для {uuid}")
                    else:
                        sub_data = response.json()
                    
                    # Логируем содержимое ответа от API
                    links_count = len(sub_data.get('links', []))
                    is_found = sub_data.get('isFound', False)
                    user_data = sub_data.get('user') or {}  # API может вернуть user: null
                    days_left = user_data.get('daysLeft', 'N/A')
                    is_active = user_data.get('isActive', False)
                    logger.info(f"📊 Данные от API для {uuid}:")
                    logger.info(f"   isFound: {is_found}")
                    logger.info(f"   isActive: {is_active}")
                    logger.info(f"   daysLeft: {days_left}")
                    logger.info(f"   Количество ссылок: {links_count}")
                    if links_count > 0:
                        # Показываем первые 3 ссылки (первые 100 символов каждой)
                        for i, link in enumerate(sub_data.get('links', [])[:3], 1):
                            logger.info(f"   Ссылка {i}: {link[:100]}...")
                        if links_count > 3:
                            logger.info(f"   ... и ещё {links_count - 3} ссылок")
                    logger.info(f"   Размер JSON: {len(response_text)} символов")
                    logger.info(f"   Первые 200 символов JSON: {response_text[:200]}")
                    
                except (ValueError, json.JSONDecodeError) as e:
                    logger.error(f"❌ Ошибка парсинга JSON от API для {uuid}: {e}")
                    logger.error(f"   URL: {api_url}")
                    logger.error(f"   Content-Type: {content_type}")
                    logger.error(f"   Размер ответа: {len(response_text)} символов")
                    logger.error(f"   Первые 500 символов ответа: {response_text[:500]}")
                    raise
                
                # Сохраняем данные в кэш (даже если isFound=False для защиты от брутфорса)
                if sub_data:
                    # Добавляем метку времени для отслеживания свежести кэша
                    sub_data['_cache_timestamp'] = time.time()
                    sub_data['_cache_fresh'] = True
                    
                    # Логируем информацию о данных перед сохранением
                    links_count = len(sub_data.get('links', []))
                    is_found = sub_data.get('isFound', False)
                    days_left = (sub_data.get('user') or {}).get('daysLeft', 'N/A')
                    logger.info(f"Сохранение в кэш для {uuid}: isFound={is_found}, links={links_count}, daysLeft={days_left}")
                    
                    try:
                        await save_cache(uuid, sub_data)
                        logger.info(f"✅ Данные подписки {uuid} успешно сохранены в кэш (isFound: {is_found}, links: {links_count})")
                    except Exception as e:
                        logger.error(f"❌ Ошибка сохранения в новый кэш для {uuid}: {e}", exc_info=True)
                    
            finally:
                if use_fallback_client and client:
                    await client.aclose()
                
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
            logger.warning(f"Не удалось подключиться к API для {uuid}: {e}. Пробуем Redis-кеш.")
            if not sub_data:
                sub_data = await get_cache(uuid, use_stale=False)
            if not sub_data:
                sub_data = await get_cache(uuid, use_stale=True)
                if sub_data:
                    logger.info(f"✅ [{client_type}] Используем устаревший Redis-кеш для {uuid} (API недоступно, таймаут)")
                    
        except httpx.HTTPStatusError as e:
            # HTTP ошибка (404, 500 и т.д.)
            if e.response.status_code == 404:
                # Пользователь не найден - кэшируем 404 на короткое время (1 минута)
                # чтобы не делать множественные запросы для несуществующих UUID
                logger.info(f"404 для {uuid}, кэшируем на 1 минуту")
                fake_404_data = {'isFound': False, 'user': {}, 'links': []}
                try:
                    await save_cache(uuid, fake_404_data)
                except Exception:
                    pass
                
                # Пробуем использовать кэш если есть (404 не требует устаревшего кэша)
                if not sub_data:
                    sub_data = await get_cache(uuid, use_stale=False)
            else:
                logger.warning(f"HTTP ошибка {e.response.status_code} для {uuid}. Пробуем Redis-кеш.")
                if not sub_data:
                    sub_data = await get_cache(uuid, use_stale=True)
                    if sub_data:
                        logger.info(f"✅ [{client_type}] Используем устаревший Redis-кеш для {uuid} (HTTP ошибка {e.response.status_code})")
                    
        except Exception as e:
            logger.error(f"Неожиданная ошибка при запросе API для {uuid}: {e}", exc_info=True)
            if not sub_data:
                sub_data = await get_cache(uuid, use_stale=True)
                if sub_data:
                    logger.info(f"✅ [{client_type}] Используем устаревший Redis-кеш для {uuid} (неожиданная ошибка API)")
        
        # Если данных все еще нет - возвращаем ошибку
            # ВАЖНО: для приложений (Happ, INCY) при таймауте лучше вернуть пустую подписку
        # чтобы приложение не показывало ошибку пользователю, а просто не обновляло подписку
        if not sub_data:
            if is_browser(user_agent):
                return templates.TemplateResponse(request=request, name="error.html", context={
                    "project_name": PROJECT_NAME,
                    "support_url": SUPPORT_URL,
                    "error_message": "Сервис временно недоступен. Попробуйте позже."
                })
            else:
                # Для приложений возвращаем пустую Base64 подписку вместо ошибки
                # Это позволит приложению корректно обработать ситуацию (не обновит подписку, но и не покажет ошибку)
                logger.warning(f"API недоступно и кэша нет для {uuid}, возвращаем пустую подписку для приложения")
                empty_sub = base64.b64encode(b"").decode('utf-8')
                return Response(content=empty_sub, media_type='text/plain')
    
    # Если данных нет ни из API, ни из кэша
    if not sub_data or not sub_data.get('isFound'):
        if is_browser(user_agent):
            return templates.TemplateResponse(request=request, name="error.html", context={
                "project_name": PROJECT_NAME,
                "support_url": SUPPORT_URL,
                "error_message": "Подписка не найдена"
            })
        else:
            # Для приложений (Happ) всегда возвращаем HTTP 200 с валидной подпиской (пустой)
            # Это предотвратит показ "ошибки сети" в приложении
            logger.info(f"Подписка не найдена для {uuid}, возвращаем пустую подписку для приложения")
            empty_sub = base64.b64encode(b"").decode('utf-8')
            return Response(content=empty_sub, media_type='text/plain', status_code=200)
    
    if PLATFORM_FILTER_ENABLED and sub_data:
        _plat = classify_platform_from_x_device_os(client_os)
        sub_data['links'] = filter_subscription_links_by_platform(sub_data.get('links', []), _plat)

    try:
        # Если отдали из кэша — логируем устройство в xuiweb (fire-and-forget)
        if not should_fetch_from_api and sub_data and (client_hwid or (user_agent and not is_browser(user_agent))):
            response_size = len(json.dumps(sub_data, ensure_ascii=False).encode('utf-8'))
            asyncio.create_task(_log_device_to_api(uuid, client_hwid, client_os, client_os_ver, client_model, user_agent, client_ip, response_size))
        
        # Проверяем статус блокировки (BLOCKED и DISABLED считаем заблокированным)
        user_status = (sub_data.get('user') or {}).get('userStatus')
        if user_status in ('BLOCKED', 'DISABLED'):
            if is_browser(user_agent):
                # Для браузера показываем страницу блокировки
                return templates.TemplateResponse(request=request, name="blocked.html", context={
                    "project_name": PROJECT_NAME,
                    "support_url": SUPPORT_URL,
                    "support_link": SUPPORT_URL,
                    "website_url": resolve_subpage_url(WEBSITE_URL, (sub_data.get('user') or {}), uuid)
                })
            else:
                # Для приложений возвращаем base64-vless-заглушку. Текст берём из
                # кастомного списка BLOCKED (общий с xuiweb), случайной фразой;
                # если список пуст — дефолт "Вы были заблокированы".
                blocked_base64 = pick_unsupported_stub_b64('blocked')
                profile_title_b64 = base64.b64encode(PROJECT_NAME.encode('utf-8')).decode('utf-8')
                headers = {'Profile-Title': f"base64:{profile_title_b64}"}
                return Response(content=blocked_base64, media_type='text/plain', headers=headers)

        # Истечение срока и лимит устройств: полный ответ (links / userStatus) собирает xuiweb.
        # Здесь только не даём отдать Xray JSON клиенту — иначе при «режиме JSON-подписки»
        # упрёмся в ветку ниже и покажем старый конфиг из кэша вместо заглушек в links.

        if is_browser(user_agent):
            # Для браузера рендерим HTML страницу
            links = sub_data.get('links', [])
            user = sub_data.get('user') or {}
            subscription_url = sub_data.get('subscriptionUrl', '')
            
            # Формируем универсальную ссылку (текущий URL) с учётом X-Forwarded-Proto (https за прокси)
            current_url = _get_request_url(request)
            
            # Формируем ссылки для авто-настройки (с правильной схемой https за reverse proxy)
            scheme, host = _get_forwarded_scheme_host(request)
            base_redirect_url = f"{scheme}://{host}/redirect_app"
            auto_add_link = f"{base_redirect_url}?target={quote_plus('happ://add/' + current_url)}"
            incy_auto_add_link = f"{base_redirect_url}?target={quote_plus('incy://add/' + current_url)}"
            
            # Получаем тему: из пути (/sub/uuid/android) или из переменной окружения
            subscription_theme = getattr(request.state, '_subscription_theme_override', None) or os.getenv("SUBSCRIPTION_THEME", "").lower()
            
            # Формируем зашифрованную ссылку Happ для темы happcrypt
            encrypted_happ_link = auto_add_link  # По умолчанию используем обычную ссылку
            happ_url_clean = ""  # Чистый ключ happ://crypt4/
            if subscription_theme == 'happcrypt':
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        api_url = "https://crypto.happ.su/api.php"
                        resp = await client.post(api_url, json={"url": current_url})
                        
                        if resp.status_code == 200:
                            response_data = resp.json()
                            happ_url = response_data.get("encrypted_link")
                            
                            if happ_url and happ_url.startswith("happ://crypt4/"):
                                happ_url_clean = happ_url  # Сохраняем чистый ключ
                                encrypted_happ_link = f"{base_redirect_url}?target={quote_plus(happ_url)}"
                            else:
                                logger.warning(f"Crypto API returned invalid data: {response_data}")
                        else:
                            logger.warning(f"Crypto API failed with status {resp.status_code}: {resp.text}")
                except Exception as e:
                    logger.error(f"Failed to call crypto API: {e}")
            
            # Генерируем QR-код для подписки
            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(current_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format='PNG')
            qr_code_data = f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}"
            
            # Формируем sub_details для совместимости с шаблоном
            expires_at = user.get('expiresAt', '')
            days_left = user.get('daysLeft', 0)
            
            # Вычисляем total_days (общий срок подписки)
            created_at = user.get('createdAt', '')
            total_days = 30  # Значение по умолчанию
            
            # Форматируем время окончания
            end_date_msk_str = '—'
            time_left_str = '—'
            if expires_at:
                try:
                    dt = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                    # Конвертируем в московское время (MSK)
                    msk_tz = ZoneInfo("Europe/Moscow")
                    dt_msk = dt.astimezone(msk_tz)
                    end_date_msk_str = dt_msk.strftime('%d.%m.%Y')
                    # Вычисляем оставшееся время (используем UTC для точности)
                    now_utc = datetime.now(timezone.utc)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    delta = dt - now_utc
                    if delta.total_seconds() > 0:
                        if days_left > 0:
                            time_left_str = f"{days_left} дн."
                        else:
                            hours = int(delta.total_seconds() // 3600)
                            time_left_str = f"{hours} ч."
                    
                    # Вычисляем total_days если есть дата создания
                    if created_at:
                        try:
                            created_dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                            total_delta = dt - created_dt
                            total_days = max(1, int(total_delta.days))
                        except Exception:
                            pass
                except Exception:
                    pass
            
            # Формируем limit_ip_display из данных API; если полей нет — игнорируем
            limit_ip = user.get('limitIp') or user.get('limit_ip')
            limit_ip_display_raw = user.get('limitIpDisplay') or user.get('limit_ip_display')
            has_limit_ip_info = 'limitIp' in user or 'limit_ip' in user or 'limitIpDisplay' in user or 'limit_ip_display' in user
            if has_limit_ip_info:
                if limit_ip and limit_ip > 0:
                    limit_ip_display = str(limit_ip)
                else:
                    limit_ip_display = limit_ip_display_raw or 'Без лимита'
            else:
                limit_ip_display = None  # API не вернул — не показываем блок
            
            sub_details = {
                'username': user.get('username', '—'),
                'time_left_str': time_left_str,
                'end_date_msk_str': end_date_msk_str,
                'limit_ip_display': limit_ip_display,
                'has_limit_ip_info': has_limit_ip_info,
                'days_left': days_left,
                'total_days': total_days,
                'sub_status': 'Активна' if user.get('isActive', False) else 'Истекла',
            }
            
            # Получаем первую ссылку как raw_vless_link
            raw_vless_link = links[0] if links else None
            
            # Формируем список серверов из ключей
            servers_list = []
            for idx, link in enumerate(links, 1):
                # Извлекаем название сервера из fragment (после #)
                server_name = f"Сервер {idx}"
                if '#' in link:
                    server_name = link.split('#')[1]
                    # Декодируем URL-encoded символы
                    server_name = unquote(server_name)
                else:
                    # Если нет названия, пытаемся извлечь из домена
                    try:
                        parsed = urlparse(link)
                        if parsed.hostname:
                            server_name = parsed.hostname.split('.')[0].capitalize()
                    except Exception:
                        pass
                
                servers_list.append({
                    'name': server_name,
                    'key': link
                })
            
            # Получаем данные о трафике для прогресс-бара
            traffic_used_bytes = 0
            traffic_limit_bytes = 0
            try:
                traffic_used_bytes = int(user.get('trafficUsedBytes', 0) or 0)
                traffic_limit_bytes = int(user.get('trafficLimitBytes', 0) or 0)
            except (ValueError, TypeError):
                traffic_used_bytes = 0
                traffic_limit_bytes = 0
            
            # Формируем контекст для шаблона
            context = {
                "request": request,
                "project_name": PROJECT_NAME,
                "support_url": SUPPORT_URL or "#",
                "support_link": SUPPORT_URL or "#",
                "website_url": resolve_subpage_url(WEBSITE_URL, user, uuid),
                "user": user,
                "links": links,
                "link": current_url,  # Универсальная ссылка
                "subscription_url": subscription_url,
                "is_active": user.get('isActive', False),
                "days_left": days_left,
                "traffic_used": user.get('trafficUsed', '0'),
                "traffic_limit": user.get('trafficLimit', 'Unlimited'),
                "traffic_used_bytes": traffic_used_bytes,
                "traffic_limit_bytes": traffic_limit_bytes,
                "expires_at": expires_at,
                "username": user.get('username', ''),
                "sub_details": sub_details,
                "raw_vless_link": raw_vless_link,
                "qr_code_data": qr_code_data,
                "auto_add_link": auto_add_link,
                "incy_auto_add_link": incy_auto_add_link,
                # Зашифрованная ссылка Happ для темы happcrypt
                "encrypted_happ_link": encrypted_happ_link,
                "happ_url_clean": happ_url_clean,
                # Ссылки на приложения
                "link_ios": LINK_IOS or "#",
                "link_ios_global": LINK_IOS_GLOBAL or "#",
                "link_android": LINK_ANDROID or "#",
                "link_android_apk": LINK_ANDROID_APK or "#",
                "link_incy_ios": LINK_INCY_IOS or "#",
                "link_incy_android": LINK_INCY_ANDROID or "#",
                "link_incy_android_apk": LINK_INCY_ANDROID_APK or "#",
                "link_incy_windows": LINK_INCY_WINDOWS or "#",
                "link_incy_mac": LINK_INCY_MAC or "#",
                "link_incy_linux": LINK_INCY_LINUX or "#",
                "link_windows": LINK_WINDOWS or "#",
                "link_mac": LINK_MAC or "#",
                "link_linux": LINK_LINUX or "#",
                # Список серверов с ключами
                "servers_list": servers_list,
                # Настройки интерфейса
                "enable_copy_keys": ENABLE_COPY_KEYS,
            }
            
            # Формируем Base64 ссылки подписки для отправки sendtv happ
            # Используем current_url (текущий URL страницы), как в основном проекте
            try:
                context['subscription_base64'] = base64.b64encode(current_url.encode('utf-8')).decode('utf-8')
            except Exception:
                context['subscription_base64'] = ''
            
            # Выбираем шаблон в зависимости от темы (путь /sub/uuid/theme или SUBSCRIPTION_THEME)
            theme_map = {
                'subscription': 'subscription.html',
                'happcrypt': 'subscription_happcrypt.html',
                'subscription_custom': 'subscription_custom.html',
                'android': 'android.html',
                'apple': 'apple.html',
                'router': 'router.html',
            }
            template_name = theme_map.get(subscription_theme, 'subscription.html')
            
            ctx = {k: v for k, v in context.items() if k != 'request'}
            return templates.TemplateResponse(request=request, name=template_name, context=ctx)
        else:
            # Для приложений возвращаем текстовую подписку
            links = sub_data.get('links', [])
            user = sub_data.get('user') or {}  # API может вернуть user: null

            # ── Ветка Xray JSON формата ────────────────────────────────────────
            # Готовый JSON конфиг формируется в xuiweb с подстановкой UUID/Email.
            # При EXPIRED / DEVICE_LIMIT в ответе должны быть только заглушки в links,
            # без рабочего JSON — иначе клиент с JSON-подпиской не покажет текст заглушки.
            xray_config = sub_data.get('xrayConfig')
            user_status_app = (user or {}).get('userStatus')
            if user_status_app in ('EXPIRED', 'DEVICE_LIMIT'):
                xray_config = None

            # Логируем данные из кэша/API перед формированием ответа
            logger.info(f"📋 Формирование ответа для приложения {uuid} (данные из {'кэша' if cache_source else 'API'}):")
            logger.info(f"   Количество ссылок из данных: {len(links)}")
            logger.info(f"   isFound: {sub_data.get('isFound', False)}")
            logger.info(f"   isActive: {user.get('isActive', False)}")
            logger.info(f"   daysLeft: {user.get('daysLeft', 'N/A')}")
            
            if not links:
                # Если нет ссылок, возвращаем заглушку
                logger.warning(f"⚠️ Нет ссылок в данных для {uuid}, добавляем заглушку")
                fallback_link = "vless://0000@127.0.0.1:443?security=tls#❌Подписка_неактивна_или_не_найдена"
                links = [fallback_link]
            
            # Определяем тип приложения по User-Agent
            ua_lower = user_agent.lower()
            is_happ = 'happ' in ua_lower
            is_incy = 'incy' in ua_lower
            is_v2raytun = 'v2raytun' in ua_lower
            
            # Формируем заголовок Subscription-Userinfo с временем окончания и трафиком
            user_info_header = None
            expires_at = user.get('expiresAt', '')
            
            # Получаем данные о трафике
            traffic_used_bytes = 0
            traffic_limit_bytes = 0
            try:
                traffic_used_bytes = int(user.get('trafficUsedBytes', 0) or 0)
                traffic_limit_bytes = int(user.get('trafficLimitBytes', 0) or 0)
            except (ValueError, TypeError):
                traffic_used_bytes = 0
                traffic_limit_bytes = 0
            
            if expires_at:
                try:
                    dt = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                    # Если дата без timezone, считаем что это UTC
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    expire_timestamp = int(dt.timestamp())
                    
                    # Формат: upload=<bytes>; download=<bytes>; total=<bytes>; expire=<timestamp>
                    # Обычно весь трафик считается как download, upload = 0
                    # Или можно разделить пополам: upload = download = traffic_used_bytes / 2
                    # Используем подход: весь трафик = download, upload = 0
                    upload_bytes = 0
                    download_bytes = traffic_used_bytes
                    total_bytes = traffic_limit_bytes if traffic_limit_bytes > 0 else 0
                    
                    user_info_header = f"upload={upload_bytes}; download={download_bytes}; total={total_bytes}; expire={expire_timestamp}"
                except Exception as e:
                    logger.debug(f"Ошибка формирования Subscription-Userinfo: {e}")
            
            # Тело подписки — только сами ссылки (мета передаётся в HTTP-заголовках)
            all_lines = list(links)
            
            subscription_text = '\n'.join(all_lines)
            # Формируем Base64 payload как bytes (как в xuiweb) - это важно для Happ
            # ВАЖНО: base64.b64encode возвращает bytes, но для Response нужно передать bytes напрямую
            final_payload_bytes = base64.b64encode(subscription_text.encode('utf-8'))
            # Для Response используем bytes напрямую (как в xuiweb)
            final_payload = final_payload_bytes
            
            # Логируем содержимое ответа клиенту
            logger.info(f"📤 Формирование ответа для клиента {uuid}:")
            logger.info(f"   User-Agent: {user_agent[:100]}")
            logger.info(f"   Тип клиента: {'Happ' if is_happ else 'INCY' if is_incy else 'V2RayTun' if is_v2raytun else 'Другое приложение'}")
            logger.info(f"   Всего ссылок: {len(links)}")
            logger.info(f"   Всего строк в подписке: {len(all_lines)}")
            logger.info(f"   Размер текста подписки: {len(subscription_text)} символов")
            logger.info(f"   Размер Base64 payload: {len(final_payload)} байт")
            if len(all_lines) > 0:
                logger.info(f"   Первые 3 строки подписки:")
                for i, line in enumerate(all_lines[:3], 1):
                    logger.info(f"      {i}: {line[:150]}")
            
            # Формируем HTTP заголовки ответа
            # Все параметры передаются через заголовки для всех клиентов (Happ, INCY и др.)
            # согласно официальной документации Happ — оба способа (заголовки и тело) поддерживаются

            def _safe_header(value: str) -> str:
                """HTTP-заголовки допускают только latin-1. URL-кодируем не-ASCII символы."""
                try:
                    value.encode('latin-1')
                    return value
                except (UnicodeEncodeError, UnicodeDecodeError):
                    from urllib.parse import quote
                    return quote(value, safe=':/?#[]@!$&\'()*+,;=%-._~')

            def _auto_header(value: str) -> str:
                """Для кастомных заголовков: ASCII без переносов — как есть;
                не-ASCII или многострочный текст — base64:<b64> (как announce)."""
                if '\n' in value or '\r' in value:
                    b64 = base64.b64encode(value.encode('utf-8')).decode('ascii')
                    return f"base64:{b64}"
                try:
                    value.encode('latin-1')
                    return value
                except (UnicodeEncodeError, UnicodeDecodeError):
                    b64 = base64.b64encode(value.encode('utf-8')).decode('ascii')
                    return f"base64:{b64}"

            project_name_short = PROJECT_NAME[:25]
            profile_title_b64 = base64.b64encode(project_name_short.encode('utf-8')).decode('utf-8')
            headers = {
                'profile-title': f"base64:{profile_title_b64}",
                'profile-update-interval': str(UPDATE_INTERVAL_HOURS),
                'update-always': 'true',
            }
            
            if user_info_header:
                headers['subscription-userinfo'] = _safe_header(user_info_header)
            
            if SUPPORT_URL:
                _support_url = resolve_subpage_url(SUPPORT_URL, user, uuid)
                headers['support-url'] = _safe_header(_support_url)
                headers['announce-url'] = _safe_header(_support_url)
            
            if WEBSITE_URL:
                headers['profile-web-page-url'] = _safe_header(resolve_subpage_url(WEBSITE_URL, user, uuid))
            
            if ANNOUNCE_TEXT:
                resolved_announce = resolve_subpage_text(ANNOUNCE_TEXT, user, uuid)
                if resolved_announce:
                    announce_b64 = base64.b64encode(resolved_announce.encode('utf-8')).decode('utf-8')
                    headers['announce'] = f"base64:{announce_b64}"

            # Применяем кастомные заголовки из .env (RESP_HEADER_*).
            # Применяются последними — могут перекрывать стандартные.
            if EXTRA_SUBSCRIPTION_HEADERS:
                for _h_name, _h_value in EXTRA_SUBSCRIPTION_HEADERS.items():
                    headers[_h_name] = _auto_header(resolve_subpage_text(_h_value, user, uuid))
                logger.info(f"[RESP_HEADER] Применены кастомные заголовки: {list(EXTRA_SUBSCRIPTION_HEADERS.keys())}")

            # ── Возврат Xray JSON (с теми же заголовками подписки) ───────────
            if xray_config:
                logger.info(f"📐 Xray JSON для {uuid}, заголовки: {list(headers.keys())}")
                return JSONResponse(content=xray_config, media_type='application/json', headers=headers)
            # ── Конец ветки Xray JSON ────────────────────────────────────────

            # Логируем заголовки ответа
            logger.info(f"   HTTP заголовки ответа: {list(headers.keys())}")
            logger.info(f"   Media-Type: text/plain")
            logger.info(f"   Размер payload (bytes): {len(final_payload)}")
            logger.info(f"✅ Ответ готов для отправки клиенту {uuid}")
            
            # Возвращаем ответ с bytes (как в xuiweb) - это предотвращает проблемы с Socket closed в Happ
            return Response(content=final_payload, media_type='text/plain', headers=headers)
            
    except Exception as e:
        logger.error(f"Error processing subscription {uuid}: {e}", exc_info=True)
        if is_browser(user_agent):
            return templates.TemplateResponse(request=request, name="error.html", context={
                "project_name": PROJECT_NAME,
                "support_url": SUPPORT_URL,
                "error_message": "Произошла ошибка при обработке подписки"
            })
        else:
            # Для приложений (Happ) всегда возвращаем HTTP 200, чтобы не показывать "ошибку сети"
            # Возвращаем пустую подписку - приложение просто не обновит подписку, но не покажет ошибку
            logger.error(f"Ошибка обработки подписки {uuid}, возвращаем пустую подписку для приложения")
            empty_sub = base64.b64encode(b"").decode('utf-8')
            return Response(content=empty_sub, media_type='text/plain', status_code=200)

@app.get("/redirect_app", response_class=HTMLResponse)
async def redirect_app(request: Request, target: str = None):
    """Редирект на приложение (Happ, INCY)"""
    if not target:
        return HTMLResponse("<h1>Ошибка: не указан URL.</h1>", status_code=400)
    
    # Извлекаем URL подписки из target (happ://add/URL, happ://crypt3/URL, happ://crypt5/URL)
    subscription_url = ""
    app_name = "приложении"  # По умолчанию
    try:
        # Декодируем URL-encoded строку
        decoded_target = unquote(target).lower()
        decoded_target_original = unquote(target)  # Сохраняем оригинальный регистр для извлечения URL
        
        # Определяем приложение и извлекаем URL подписки
        if "happ://" in decoded_target or "://crypt3/" in decoded_target or "://crypt4/" in decoded_target or "://crypt5/" in decoded_target:
            app_name = "Happ"
            if "://crypt3/" in decoded_target_original:
                subscription_url = decoded_target_original
            elif "://crypt4/" in decoded_target_original:
                subscription_url = decoded_target_original
            elif "://crypt5/" in decoded_target_original:
                subscription_url = decoded_target_original
            elif "://add/" in decoded_target_original:
                subscription_url = decoded_target_original.split("://add/", 1)[1]
            else:
                parts = decoded_target_original.split("://", 1)
                if len(parts) > 1 and "/" in parts[1]:
                    subscription_url = parts[1].split("/", 1)[1]
        elif "v2raytun://" in decoded_target or "://import/" in decoded_target:
            app_name = "V2RayTun"
            if "://import/" in decoded_target_original:
                subscription_url = decoded_target_original.split("://import/", 1)[1]
            else:
                parts = decoded_target_original.split("://", 1)
                if len(parts) > 1 and "/" in parts[1]:
                    subscription_url = parts[1].split("/", 1)[1]
        elif "incy://" in decoded_target:
            app_name = "INCY"
            if "://add/" in decoded_target_original:
                subscription_url = decoded_target_original.split("://add/", 1)[1]
            else:
                parts = decoded_target_original.split("://", 1)
                if len(parts) > 1 and "/" in parts[1]:
                    subscription_url = parts[1].split("/", 1)[1]
        else:
            # Пробуем другие варианты
            parts = decoded_target_original.split("://", 1)
            if len(parts) > 1:
                url_part = parts[1]
                # Ищем URL после первого слеша
                if "/" in url_part:
                    subscription_url = url_part.split("/", 1)[1]
    except Exception as e:
        logger.warning(f"Не удалось извлечь URL подписки из target: {e}")
        subscription_url = ""
    
    # Передаем target как есть (FastAPI уже декодирует query параметры)
    # Как в xuiweb - просто передаем target без дополнительного декодирования
    logger.info(f"Redirect app: target={target}, app_name={app_name}")
    
    context = {
        "request": request,
        "target": target,  # Передаем target как есть (FastAPI уже декодировал)
        "subscription_url": subscription_url,
        "app_name": app_name,
        "support_link": SUPPORT_URL or "#"
    }
    
    ctx = {k: v for k, v in context.items() if k != 'request'}
    return templates.TemplateResponse(request=request, name="redirect_app.html", context=ctx)


@app.post("/sendtv/{uid}")
async def send_tv(uid: str, request: Request):
    """
    Проксирование отправки подписки на TV по UID
    Аналогично реализации в основном проекте
    """
    try:
        payload = await request.json()
        base64_data = (payload or {}).get('data')
        
        # Валидация UID
        if not uid or len(uid) != 5 or not uid.isalnum():
            return JSONResponse({"ok": False, "error": "invalid_uid"}, status_code=400)
        
        # Валидация данных
        if not base64_data or not isinstance(base64_data, str):
            return JSONResponse({"ok": False, "error": "invalid_data"}, status_code=400)
        
        # Проксируем запрос на сервис Happ.
        # Если задан HAPP_TV_PROXY — идём через указанный прокси
        # (например, при блокировке check.happ.su на сервере).
        client_kwargs = {"timeout": 10.0}
        if HAPP_TV_PROXY:
            client_kwargs["proxy"] = HAPP_TV_PROXY
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(
                f"https://check.happ.su/sendtv/{uid}",
                json={"data": base64_data},
                headers={"Accept": "application/json"},
            )

        # Пробуем распарсить ответ Happ — иногда они отдают 200 с ошибкой в теле
        body_text = (resp.text or "")[:500]
        body_json = None
        try:
            body_json = resp.json()
        except Exception:
            pass

        logger.info(
            f"/sendtv/{uid} → check.happ.su status={resp.status_code} "
            f"json={body_json if body_json is not None else 'N/A'} text={body_text!r}"
        )

        # Считаем успехом только когда HTTP 2xx И в JSON-ответе нет признаков ошибки.
        # Известные паттерны ошибок Happ: {"ok": false, ...} / {"status": "error"} / {"error": "..."}
        is_http_ok = 200 <= resp.status_code < 300
        is_body_ok = True
        if isinstance(body_json, dict):
            if body_json.get("ok") is False:
                is_body_ok = False
            elif str(body_json.get("status", "")).lower() in ("error", "fail", "failed"):
                is_body_ok = False
            elif body_json.get("error"):
                is_body_ok = False

        if is_http_ok and is_body_ok:
            return JSONResponse({"ok": True, "remote": body_json})

        # Возвращаем максимум информации на фронт — пользователь увидит реальную причину
        return JSONResponse({
            "ok": False,
            "status": resp.status_code,
            "remote": body_json,
            "error": body_text or "remote_error",
        }, status_code=resp.status_code if not is_http_ok else 502)
            
    except httpx.TimeoutException:
        logger.error(f"/sendtv/{uid} failed: timeout")
        return JSONResponse({"ok": False, "error": "timeout"}, status_code=504)
    except Exception as e:
        logger.error(f"/sendtv/{uid} failed: {e}", exc_info=True)
        return JSONResponse({"ok": False, "error": "exception"}, status_code=500)


if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv("PORT", "3004"))
    host = os.getenv("HOST", "127.0.0.1")
    logger.info(f"Subscription page 3XUIStore {host}:{port}")
    logger.info(f"API Base URL: {API_BASE_URL}")
    uvicorn.run("run:app", host=host, port=port)


