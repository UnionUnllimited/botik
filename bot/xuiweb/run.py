import os
import sys

# Корень проекта и xuiweb в sys.path — до локальных импортов (dedup, devices, …)
script_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(script_dir)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import asyncio
import base64
from contextlib import asynccontextmanager
import json
import logging
import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiosqlite
import httpx
try:
    from remnawave import RemnawaveSDK
    from remnawave.exceptions import ApiError
    from remnawave.enums import ClientType as RemnawaveClientType
    REMNAWAVE_SDK_AVAILABLE = True
except ImportError:
    REMNAWAVE_SDK_AVAILABLE = False
import pytz
from fastapi import FastAPI, Request, Response, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from dedup.link_dedup import deduplicate_subscription_links
from dedup.link_host_stats import TextLinkHostStatsSettings, text_link_host_cache
from dedup.link_host_stats_loop import run_link_host_stats_loop

# Импорт для логирования доступа к подпискам
try:
    from devices.database import insert_access_record, get_hwid_list_for_uuid
    SUBSCRIPTION_LOG_AVAILABLE = True
except ImportError as e:
    SUBSCRIPTION_LOG_AVAILABLE = False
    logging.warning(f"Модуль devices не доступен, логирование устройств отключено: {e}")


# Справочник «код модели → маркетинговое имя» вынесен в отдельный пакет
# xuiweb/device_names — там же лежат devices.json (онлайн-кэш) и
# devices_overrides.json (ручные правки). Здесь импортируем только
# публичные функции, которые нужны run.py.
try:
    from xuiweb.device_names import (
        load_device_names,
        prettify_device_model,
        refresh_device_names_if_stale,
    )
except ImportError:
    # Запуск напрямую (`python xuiweb/run.py`): xuiweb может не быть на
    # sys.path, но script_dir уже добавлен — пробуем абсолютный импорт.
    from device_names import (  # type: ignore[no-redef]
        load_device_names,
        prettify_device_model,
        refresh_device_names_if_stale,
    )

try:
    from xuiweb.balancer import (
        apply_xray_balancer,
        is_xray_json_client,
        BalancerOptions,
        NodeStatsSettings,
        node_stats_cache,
        parse_routing_config,
    )
    from xuiweb.balancer.node_stats_loop import run_node_stats_loop
    XRAY_BALANCER_AVAILABLE = True
except ImportError:
    try:
        from balancer import (  # type: ignore[no-redef]
            apply_xray_balancer,
            is_xray_json_client,
            BalancerOptions,
            NodeStatsSettings,
            node_stats_cache,
            parse_routing_config,
        )
        from balancer.node_stats_loop import run_node_stats_loop  # type: ignore[no-redef]
        XRAY_BALANCER_AVAILABLE = True
    except ImportError as e:
        XRAY_BALANCER_AVAILABLE = False
        logging.warning(f"Модуль balancer недоступен: {e}")

        def is_xray_json_client(user_agent: str | None, allowed_markers=None) -> bool:
            if not user_agent:
                return False
            markers = allowed_markers if allowed_markers else ('happ', 'incy', 'v2raytun')
            if not markers:
                return False
            ua = user_agent.lower()
            return any(m in ua for m in markers)


async def check_hwid_allowed(uuid: str, hwid: str, limit: int) -> bool:
    """
    Проверяет, разрешён ли данный HWID для UUID с учётом лимита устройств.
    Возвращает True — разрешить, False — лимит исчерпан.

    Никакого отдельного кэша не нужно — subscription_access уже является
    источником истины и содержит все данные.

    Алгоритм:
    1. Если HWID уже есть в subscription_access → разрешаем (известное устройство).
    2. Если HWID новый → считаем уникальные HWID для UUID.
       Если count < limit → разрешаем (устройство будет залогировано после).
       Если count >= limit → блокируем.
    """
    try:
        from devices.database import get_db_connection
        async with get_db_connection(read_only=True) as db:
            # Шаг 1: HWID уже знакомый?
            cursor = await db.execute(
                "SELECT 1 FROM subscription_access WHERE uuid = ? AND hwid = ? LIMIT 1",
                (uuid, hwid)
            )
            if await cursor.fetchone():
                return True  # известное устройство — всегда пропускаем

            # Шаг 2: считаем уникальные HWID этого UUID
            cursor = await db.execute(
                """
                SELECT COUNT(DISTINCT hwid) FROM subscription_access
                WHERE uuid = ?
                  AND hwid IS NOT NULL AND hwid != '' AND hwid != '-'
                """,
                (uuid,)
            )
            row = await cursor.fetchone()
            count = row[0] if row else 0

            if count < limit:
                return True  # есть свободный слот — разрешаем новое устройство

            logging.info(
                f"[HWID] Лимит {limit} исчерпан для {uuid} "
                f"(занято {count}), hwid={hwid[:16]}... заблокирован"
            )
            return False
    except Exception as e:
        logging.warning(f"[HWID] Ошибка проверки hwid для {uuid}: {e}")
        return True  # при ошибке БД — не блокируем

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Инициализация HTTP-клиента с connection pooling для 1000+ одновременных соединений"""
    load_device_names()

    if SUBSCRIPTION_LOG_AVAILABLE:
        try:
            from devices.database import init_database as init_devices_db
            await init_devices_db()
        except Exception as e:
            logging.warning(f"Не удалось инициализировать БД устройств: {e}")

    limits = httpx.Limits(max_keepalive_connections=2000, max_connections=10000, keepalive_expiry=60.0)
    http_client = httpx.AsyncClient(timeout=15.0, verify=False, limits=limits)
    app.state.http_client = http_client
    logging.info("HTTP-клиент инициализирован (max_connections=10000, keepalive=2000)")

    # Подтягиваем справочник моделей в фоне — старт сервиса не блокируется.
    devices_refresh_task = asyncio.create_task(refresh_device_names_if_stale(http_client))

    node_stats_task = None
    if XRAY_BALANCER_AVAILABLE:
        async def _node_stats_settings_loader():
            return await _load_node_stats_settings()

        async def _node_stats_remnawave_loader():
            return (
                await get_db_setting_async('remnawave_base_url'),
                await get_db_setting_async('remnawave_api_token'),
            )

        node_stats_task = asyncio.create_task(
            run_node_stats_loop(
                get_settings=_node_stats_settings_loader,
                get_remnawave=_node_stats_remnawave_loader,
                http_client=http_client,
            )
        )
        logging.info('Фоновый опрос node_stats запущен')

    link_host_stats_task = None

    async def _link_host_stats_settings_loader():
        return await _load_text_link_host_stats_settings()

    async def _link_host_remnawave_loader():
        return (
            await get_db_setting_async('remnawave_base_url'),
            await get_db_setting_async('remnawave_api_token'),
        )

    link_host_stats_task = asyncio.create_task(
        run_link_host_stats_loop(
            get_settings=_link_host_stats_settings_loader,
            get_remnawave=_link_host_remnawave_loader,
            http_client=http_client,
        )
    )
    logging.info('Фоновый опрос link_host_stats запущен')

    try:
        yield
    finally:
        if link_host_stats_task is not None:
            link_host_stats_task.cancel()
            try:
                await link_host_stats_task
            except (asyncio.CancelledError, Exception):
                pass
        if node_stats_task is not None:
            node_stats_task.cancel()
            try:
                await node_stats_task
            except (asyncio.CancelledError, Exception):
                pass
        if not devices_refresh_task.done():
            devices_refresh_task.cancel()
            try:
                await devices_refresh_task
            except (asyncio.CancelledError, Exception):
                pass
        await http_client.aclose()
        logging.info("HTTP-клиент закрыт")


app = FastAPI(
    title="Subscription API",
    description="Асинхронный сервис для выдачи VPN подписок (Remnawave)",
    version="1.0.0", 
    docs_url=None, 
    redoc_url=None,
    lifespan=lifespan
)

# script_dir уже определен выше для sys.path
DATABASE_NAME = os.path.join(parent_dir, 'vpn_bot.db')

# Протоколы, разрешённые из Remnawave
# hy2:// — синоним hysteria2:// (используется в части клиентов / шаблонов).
REMNAWAVE_ALLOWED_PROTOCOLS = ('vless://', 'ss://', 'trojan://', 'hysteria2://', 'hy2://')

def _filter_remnawave_links(links: list) -> list:
    """Оставляет только ссылки с разрешёнными протоколами."""
    if not links:
        return []
    return [ln for ln in links if isinstance(ln, str) and ln.strip().startswith(REMNAWAVE_ALLOWED_PROTOCOLS)]


class _SDKManager:
    """Кэшированный SDK-инстанс Remnawave для xuiweb."""
    def __init__(self):
        self._sdk = None
        self._base_url = None
        self._token = None
        self._lock = asyncio.Lock()

    async def get(self) -> 'RemnawaveSDK | None':
        if not REMNAWAVE_SDK_AVAILABLE:
            return None
        base_url = await get_db_setting_async('remnawave_base_url')
        token = await get_db_setting_async('remnawave_api_token')
        if not base_url or not token:
            return None
        if self._sdk and self._base_url == base_url and self._token == token:
            return self._sdk
        async with self._lock:
            if self._sdk and self._base_url == base_url and self._token == token:
                return self._sdk
            try:
                self._sdk = RemnawaveSDK(base_url=base_url, token=token)
                self._base_url, self._token = base_url, token
                logging.info(f"[REMNAWAVE] SDK инициализирован для xuiweb: {base_url}")
            except Exception as e:
                logging.error(f"[REMNAWAVE] Ошибка инициализации SDK: {e}")
                self._sdk = None
            return self._sdk

    def reset(self):
        self._sdk = self._base_url = self._token = None

_remnawave_sdk = _SDKManager()



async def _get_db_connection():
    # 0001: Подключение к SQLite (read-only), настройка таймаутов
    # ВАЖНО: На read-only подключении PRAGMA journal_mode=WAL игнорируется SQLite,
    # но мы не устанавливаем его, чтобы не создавать путаницу.
    # WAL режим уже должен быть установлен процессами записи (бот, веб-админка).
    if not os.path.exists(DATABASE_NAME):
        logging.error(f"DATABASE NOT FOUND at path: {DATABASE_NAME}")
        return None
    try:
        conn = await aiosqlite.connect(f'file:{DATABASE_NAME}?mode=ro', uri=True, timeout=30)
        # Для read-only устанавливаем только busy_timeout
        # WAL режим уже установлен другими процессами
        await conn.execute("PRAGMA busy_timeout=30000;")
        return conn
    except aiosqlite.Error as e:
        logging.error(f"Failed to connect to database: {e}")
        return None

async def get_db_setting_async(key: str, default_value: str = None) -> str:
    # 0002: Получение настройки по ключу из таблицы settings
    conn = await _get_db_connection()
    if not conn: return default_value
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
            result = await cur.fetchone()
            return result[0] if result and result[0] else default_value
    finally:
        if conn: await conn.close()


def _parse_float_setting(raw: str | None, default: float) -> float:
    try:
        if raw in (None, ''):
            return default
        return float(raw)
    except (TypeError, ValueError):
        return default


def _parse_int_setting(raw: str | None, default: int) -> int:
    try:
        if raw in (None, ''):
            return default
        return int(float(raw))
    except (TypeError, ValueError):
        return default


async def _load_text_link_host_stats_settings() -> TextLinkHostStatsSettings:
    dedup_enabled = (await get_db_setting_async('sub_link_dedup_enabled', '0')) == '1'
    dedup_mode = (await get_db_setting_async('sub_link_dedup_mode', 'random') or 'random').strip().lower()
    return TextLinkHostStatsSettings(
        enabled=dedup_enabled and dedup_mode == 'online',
        online_threshold=_parse_int_setting(
            await get_db_setting_async('sub_link_dedup_online_threshold'), 100,
        ),
        interval_sec=_parse_int_setting(
            await get_db_setting_async('sub_link_dedup_online_interval_sec'), 60,
        ),
    )


async def _load_node_stats_settings() -> 'NodeStatsSettings':
    if not XRAY_BALANCER_AVAILABLE:
        return NodeStatsSettings(enabled=False)
    enabled = (await get_db_setting_async('xray_balancer_node_stats_enabled', '0')) == '1'
    return NodeStatsSettings(
        enabled=enabled,
        max_users_per_gb=_parse_float_setting(
            await get_db_setting_async('xray_balancer_max_users_per_gb'), 50.0,
        ),
        max_users_per_cpu=_parse_float_setting(
            await get_db_setting_async('xray_balancer_max_users_per_cpu'), 80.0,
        ),
        node_load_threshold=_parse_float_setting(
            await get_db_setting_async('xray_balancer_node_load_threshold'), 1.0,
        ),
        interval_sec=_parse_int_setting(
            await get_db_setting_async('xray_balancer_node_stats_interval_sec'), 120,
        ),
    )


async def _load_xray_json_client_markers() -> tuple[str, ...]:
    markers: list[str] = []
    if (await get_db_setting_async('xray_json_client_happ', '1')) == '1':
        markers.append('happ')
    if (await get_db_setting_async('xray_json_client_incy', '1')) == '1':
        markers.append('incy')
    if (await get_db_setting_async('xray_json_client_v2raytun', '1')) == '1':
        markers.append('v2raytun')
    return tuple(markers)


async def _load_balancer_options() -> 'BalancerOptions':
    from xuiweb.balancer.settings import parse_balancer_settings

    setting_keys = (
        'xray_balancer_mode',
        'xray_balancer_strategy',
        'xray_balancer_auto_group_name',
        'xray_balancer_lte_auto_group_name',
        'xray_balancer_lte_triggers',
        'xray_balancer_auto_exclude',
        'xray_balancer_probe_url',
        'xray_balancer_probe_interval',
        'xray_balancer_probe_sampling',
        'xray_balancer_probe_timeout',
        'xray_balancer_tolerance',
        'xray_balancer_tolerance_fallback',
        'xray_balancer_load_expected',
        'xray_balancer_main_lte_baseline_ms',
        'xray_balancer_single_baseline_ms',
        'xray_balancer_lte_baseline_ms',
        'xray_balancer_lte_plus_baseline_ms',
        'xray_balancer_lte_minus_baseline_ms',
        'xray_balancer_dns_servers',
        'xray_balancer_dns_query_strategy',
        'xray_balancer_domain_strategy',
    )
    raw: dict[str, str] = {}
    for key in setting_keys:
        raw[key] = await get_db_setting_async(key, '')
    routing_raw = await get_db_setting_async('xray_balancer_routing_config', '')
    if not str(raw.get('xray_balancer_domain_strategy') or '').strip():
        routing_legacy = parse_routing_config(routing_raw)
        raw['xray_balancer_domain_strategy'] = routing_legacy.domain_strategy
    routing = parse_routing_config(routing_raw)
    routing.enabled = (await get_db_setting_async('xray_balancer_routing_enabled', '0')) == '1'
    return BalancerOptions(
        node_stats=await _load_node_stats_settings(),
        routing=routing,
        balancer=parse_balancer_settings(raw),
    )


async def get_multiple_db_settings_async(keys_with_defaults: dict[str, str]) -> dict[str, str]:
    # 0003: Пакетное чтение нескольких настроек с подстановкой значений по умолчанию
    conn = await _get_db_connection()
    final_settings = keys_with_defaults.copy()
    if not conn: return final_settings
    try:
        placeholders = ','.join('?' for _ in keys_with_defaults)
        query = f"SELECT key, value FROM settings WHERE key IN ({placeholders})"
        async with conn.cursor() as cur:
            await cur.execute(query, list(keys_with_defaults.keys()))
            db_results = await cur.fetchall()
            for key, value in db_results:
                if value: final_settings[key] = value
        return final_settings
    except aiosqlite.Error as e:
        logging.error(f"Database access error while getting multiple settings: {e}")
        return keys_with_defaults
    finally:
        if conn: await conn.close()

async def get_subscription_details_by_uuid_async(uuid: str) -> dict | None:
    # Получение данных подписки пользователя по xui_client_uuid (Remnawave)
    conn = await _get_db_connection()
    if not conn: return None
    try:
        async with conn.cursor() as cur:
            # Проверяем наличие колонки is_blocked
            await cur.execute("PRAGMA table_info(users)")
            columns = await cur.fetchall()
            column_names = [col[1] for col in columns]
            has_is_blocked = 'is_blocked' in column_names
            has_email = 'email' in column_names
            has_reg_type = 'registration_type' in column_names
            email_col = 'email' if has_email else 'NULL as email'
            reg_col = "COALESCE(registration_type, 'telegram') as registration_type" if has_reg_type else "'telegram' as registration_type"
            
            if has_is_blocked:
                await cur.execute(
                    f"SELECT username, subscription_end_date, limit_ip, telegram_id, xui_client_email, "
                    f"remnawave_short_uuid, COALESCE(is_blocked, 0) as is_blocked, {email_col}, {reg_col} "
                    f"FROM users WHERE xui_client_uuid = ?",
                    (uuid,),
                )
            else:
                await cur.execute(
                    f"SELECT username, subscription_end_date, limit_ip, telegram_id, xui_client_email, "
                    f"remnawave_short_uuid, 0 as is_blocked, {email_col}, {reg_col} "
                    f"FROM users WHERE xui_client_uuid = ?",
                    (uuid,),
                )
            
            result = await cur.fetchone()
            if not result: return None
            username, end_date_iso, limit_ip, telegram_id, client_email, remnawave_short_uuid, is_blocked, email, registration_type = result
            details = {
                "username": username, 
                "telegram_id": telegram_id, 
                "client_email": client_email or "",
                "email": (email or "").strip() if email else "",
                "registration_type": (registration_type or "telegram").strip(),
                "limit_ip": limit_ip,
                "limit_ip_display": str(limit_ip) if limit_ip and limit_ip > 0 else "Без лимита", 
                "end_date_formatted": "Нет данных", 
                "end_date_iso": end_date_iso, 
                "end_date_msk_str": None, 
                "sub_status": None, 
                "time_left_str": None,
                "remnawave_short_uuid": remnawave_short_uuid,
                "is_blocked": bool(is_blocked) if is_blocked is not None else False
            }
            if end_date_iso:
                try:
                    dt_obj = datetime.fromisoformat(end_date_iso)
                    moscow_tz = pytz.timezone('Europe/Moscow')
                    dt_msk = dt_obj.astimezone(moscow_tz) if dt_obj.tzinfo else moscow_tz.localize(dt_obj)
                    now_msk = datetime.now(moscow_tz)
                    details["end_date_formatted"] = dt_msk.strftime('%d.%m.%Y %H:%M')
                    details["end_date_msk_str"] = f"{details['end_date_formatted']} МСК"
                    delta = dt_msk - now_msk
                    if delta.total_seconds() > 0:
                        details["sub_status"] = "Активна"
                        days, rem = divmod(delta.total_seconds(), 86400)
                        hours, rem = divmod(rem, 3600)
                        details["days_left"] = int(days)
                        # Убираем минуты, показываем только дни и часы
                        parts = [f"{int(d)} дн." for d in [days] if d > 0] + [f"{int(h)} ч." for h in [hours] if h > 0]
                        details["time_left_str"] = " ".join(parts) if parts else "меньше часа"
                    else:
                        details["sub_status"] = "Истекла"; details["time_left_str"] = "—"
                        details["days_left"] = 0
                except (ValueError, TypeError):
                    details["end_date_formatted"] = "Ошибка формата даты"
            return details
    finally:
        if conn: await conn.close()

async def get_remnawave_subscription_data_async(short_uuid: str, http_client: httpx.AsyncClient | None = None) -> dict | None:
    """Получает данные подписки из Remnawave через SDK (с fallback на прямой HTTP-запрос).
    
    http_client — пуловый клиент для общих запросов. Для Remnawave всегда создаётся
    отдельный клиент, т.к. SDK игнорирует base_url при передаче внешнего клиента,
    а пуловые keepalive-соединения могут быть закрыты сервером.
    """
    if not short_uuid:
        return None

    remnawave_base_url = await get_db_setting_async('remnawave_base_url')
    if not remnawave_base_url:
        return None

    REMNAWAVE_TIMEOUT = 6.0

    # Пробуем через кэшированный SDK (аналогично remnawave_manager_instance в боте)
    sdk = await _remnawave_sdk.get()
    if sdk:
        try:
            response = await sdk.subscription.get_subscription_info_by_short_uuid(short_uuid)
            if response and response.user:
                u = response.user
                return {
                    'isFound': True,
                    'links': response.links or [],
                    'ssConfLinks': dict(response.ss_conf_links or {}),
                    'subscriptionUrl': response.subscription_url or '',
                    'user': {
                        'shortUuid': u.short_uuid,
                        'username': u.username,
                        'daysLeft': u.days_left,
                        'trafficUsed': u.traffic_used,
                        'trafficLimit': u.traffic_limit,
                        'lifetimeTrafficUsed': u.lifetime_traffic_used,
                        'trafficUsedBytes': u.traffic_used_bytes,
                        'trafficLimitBytes': u.traffic_limit_bytes,
                        'lifetimeTrafficUsedBytes': u.lifetime_traffic_used_bytes,
                        'expiresAt': u.expires_at.isoformat() if u.expires_at else '',
                        'isActive': u.is_active,
                        'userStatus': str(u.user_status),
                        'trafficLimitStrategy': str(u.traffic_limit_strategy),
                    }
                }
            return None
        except Exception as e:
            # Сбрасываем кэш SDK при ошибке — пересоздадим при следующем запросе
            _remnawave_sdk.reset()
            logging.warning(f"[REMNAWAVE] SDK ошибка, fallback на HTTP: {type(e).__name__}: {e}")

    # Fallback: прямой HTTP-запрос (всегда с новым клиентом — без keepalive)
    try:
        api_url = f"{remnawave_base_url}/api/sub/{short_uuid}/info"
        async with httpx.AsyncClient(timeout=REMNAWAVE_TIMEOUT, verify=False) as client:
            resp = await client.get(api_url, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            logging.warning(f"[REMNAWAVE] HTTP fallback: status={resp.status_code}")
            return None
        response_text = resp.text.strip()
        if not response_text:
            return None
        try:
            parsed_data = json.loads(response_text)
        except json.JSONDecodeError:
            try:
                parsed_data = json.loads(base64.b64decode(response_text).decode('utf-8'))
            except Exception:
                return None
        if isinstance(parsed_data, dict) and 'response' in parsed_data:
            return parsed_data['response']
        return parsed_data
    except Exception as e:
        logging.error(f"[REMNAWAVE] Исключение (fallback): {type(e).__name__}: {e}", exc_info=True)
        return None

async def get_user_uuid_by_telegram_id_async(telegram_id: int) -> str | None:
    try:
        conn = await _get_db_connection()
        if not conn:
            return None
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT xui_client_uuid
                    FROM users
                    WHERE telegram_id = ? AND xui_client_uuid IS NOT NULL AND xui_client_uuid != ''
                    ORDER BY (
                        CASE WHEN subscription_end_date IS NOT NULL AND datetime(subscription_end_date) > datetime('now', 'utc') THEN 0 ELSE 1 END
                    ), datetime(subscription_end_date) DESC
                    LIMIT 1
                    """,
                    (int(telegram_id),)
                )
                row = await cur.fetchone()
                return row[0] if row and row[0] else None
        finally:
            await conn.close()
    except Exception as e:
        logging.error(f"get_user_uuid_by_telegram_id_async error: {e}")
        return None

async def check_user_blocked_async(telegram_id: int) -> bool:
    # Проверка блокировки пользователя по telegram_id
    conn = await _get_db_connection()
    if not conn:
        return False
    try:
        async with conn.cursor() as cur:
            # Проверяем наличие колонки is_blocked
            await cur.execute("PRAGMA table_info(users)")
            columns = await cur.fetchall()
            column_names = [col[1] for col in columns]
            has_is_blocked = 'is_blocked' in column_names
            
            if has_is_blocked:
                await cur.execute("SELECT COALESCE(is_blocked, 0) as is_blocked FROM users WHERE telegram_id = ? LIMIT 1", (int(telegram_id),))
            else:
                return False
            
            row = await cur.fetchone()
            if row:
                return bool(row[0])
            return False
    except Exception as e:
        logging.error(f"check_user_blocked_async error: {e}")
        return False
    finally:
        if conn:
            await conn.close()

def is_browser(user_agent: str) -> bool:
    return any(b in user_agent for b in ['Mozilla', 'Chrome', 'Safari', 'Firefox', 'Opera', 'Edge'])

_LOCAL_IPS = frozenset({'127.0.0.1', '::1', 'localhost', '0.0.0.0'})
_INTERNAL_UA_PREFIXES = ('python-httpx/', 'python-requests/', 'curl/', 'wget/')


def _is_internal_request(ip_address: str, user_agent: str) -> bool:
    """Возвращает True для внутренних/локальных запросов, которые не нужно логировать."""
    if ip_address and ip_address.split(':')[0] in _LOCAL_IPS:
        return True
    if user_agent:
        ua = user_agent.lower()
        for prefix in _INTERNAL_UA_PREFIXES:
            if ua.startswith(prefix.lower()):
                return True
    return False


async def _log_device_access(
    uuid: str,
    ip_address: str,
    hwid: str,
    os: str,
    os_version: str,
    model: str,
    user_agent: str,
    status_code: int,
    response_size: int
):
    """Асинхронная запись данных устройства в devices.db"""
    if not SUBSCRIPTION_LOG_AVAILABLE:
        return

    if _is_internal_request(ip_address, user_agent):
        logging.debug(f"[devices] Пропуск внутреннего запроса для {uuid}: ip={ip_address}, ua={user_agent[:40] if user_agent else ''}")
        return

    try:
        access_time = datetime.utcnow().isoformat()
        
        await insert_access_record(
            uuid=uuid,
            ip_address=ip_address,
            hwid=hwid or '',
            os=os or '',
            os_version=os_version or '',
            model=model or '',
            user_agent=user_agent or '',
            access_time=access_time,
            method='GET',
            path=f'/api/sub/{uuid}',
            status_code=status_code,
            response_size=response_size
        )
        logging.debug(f"Записано устройство для {uuid}: HWID={hwid[:20] if hwid else 'N/A'}..., OS={os}, IP={ip_address}")
    except Exception as e:
        # Логируем ошибку, но не прерываем выполнение
        logging.debug(f"Ошибка записи устройства для {uuid}: {e}")

async def build_user_info_header_from_db(sub_details: dict | None, http_client: httpx.AsyncClient | None = None, remnawave_data: dict | None = None) -> str | None:
    # 0013a: Формирование заголовка Subscription-Userinfo из данных БД
    # remnawave_data — если уже получен выше (параллельный gather), не делать повторный запрос к API
    if not sub_details:
        return None
    
    end_date_iso = sub_details.get('end_date_iso')
    if not end_date_iso:
        return None
    
    try:
        dt_obj = datetime.fromisoformat(end_date_iso)
        if dt_obj.tzinfo is None:
            dt_obj = dt_obj.replace(tzinfo=timezone.utc)
        expire_timestamp = int(dt_obj.timestamp())
        
        upload_bytes = 0
        download_bytes = 0
        total_bytes = 0
        
        remnawave_short_uuid = sub_details.get('remnawave_short_uuid')
        
        if remnawave_short_uuid:
            if not remnawave_data:
                remnawave_data = await get_remnawave_subscription_data_async(remnawave_short_uuid, http_client)
            if remnawave_data and remnawave_data.get('isFound') and remnawave_data.get('user'):
                user_data = remnawave_data['user']
                try:
                    # Получаем данные о трафике из Remnawave
                    traffic_used_bytes = user_data.get('trafficUsedBytes', 0)
                    traffic_limit_bytes = user_data.get('trafficLimitBytes', 0)
                    
                    # Преобразуем в int
                    traffic_used_bytes = int(traffic_used_bytes) if traffic_used_bytes else 0
                    traffic_limit_bytes = int(traffic_limit_bytes) if traffic_limit_bytes else 0
                    
                    # В Remnawave обычно не разделяют upload/download, используем общий used
                    # Для совместимости с форматом Subscription-Userinfo распределяем поровну
                    if traffic_used_bytes > 0:
                        upload_bytes = traffic_used_bytes // 2
                        download_bytes = traffic_used_bytes // 2
                    
                    # total_bytes: если есть лимит - используем его, иначе 0 (бесконечный)
                    total_bytes = traffic_limit_bytes if traffic_limit_bytes > 0 else 0
                    
                    logging.debug(f"[SUBSCRIPTION-USERINFO] Remnawave трафик: used={traffic_used_bytes}, limit={traffic_limit_bytes}, upload={upload_bytes}, download={download_bytes}, total={total_bytes}")
                except (ValueError, TypeError, AttributeError) as e:
                    logging.warning(f"[SUBSCRIPTION-USERINFO] Ошибка получения трафика из Remnawave: {e}, используем 0")
                    upload_bytes = 0
                    download_bytes = 0
                    total_bytes = 0
        
        # Если Remnawave не вернул данные — используем 0 (бесконечный трафик)
        # Формируем заголовок: upload=bytes; download=bytes; total=bytes; expire=timestamp
        # total=0 означает бесконечный трафик
        header = f"upload={upload_bytes}; download={download_bytes}; total={total_bytes}; expire={expire_timestamp}"
        return header
    except (ValueError, TypeError, AttributeError) as e:
        logging.debug(f"Ошибка формирования Subscription-Userinfo из БД: {e}")
        return None
async def _get_db_rw_connection():
    """
    Подключение к БД для записи.
    Устанавливает WAL режим и настройки для безопасной параллельной работы с ботом и веб-админкой.
    КРИТИЧЕСКИ ВАЖНО: Всегда устанавливаем WAL, чтобы не перезаписывать журналы, созданные другими процессами.
    """
    try:
        conn = await aiosqlite.connect(DATABASE_NAME, timeout=30)
        # КРИТИЧЕСКИ ВАЖНО: Всегда устанавливаем WAL для безопасной параллельной работы
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.execute("PRAGMA busy_timeout=30000;")  # Унифицированный таймаут 30 сек
        await conn.execute("PRAGMA foreign_keys=ON;")
        return conn
    except aiosqlite.Error as e:
        logging.error(f"Failed to open RW database: {e}")
        return None


async def _local_only(request: Request):
    """Dependency: разрешает запрос только с localhost."""
    host = request.client.host if request.client else None
    if host not in ('127.0.0.1', '::1', 'localhost'):
        raise HTTPException(status_code=403, detail="Local access only")


@app.get("/devices/{uuid}", response_class=JSONResponse)
async def get_devices_by_uuid_endpoint(uuid: str, _: None = Depends(_local_only)):
    """
    Возвращает устройства пользователя по UUID.
    Только для запросов с localhost.
    Возвращает только записи с HWID (пустые — исключаются).
    """
    if not SUBSCRIPTION_LOG_AVAILABLE:
        return JSONResponse({"error": "devices module not available"}, status_code=503)

    try:
        from devices.database import get_db_connection, classify_subscription_user_agent
        async with get_db_connection(read_only=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT hwid, os, os_version, model, user_agent,
                       ip_address, access_time, access_count, created_at, custom_name
                FROM subscription_access
                WHERE uuid = ?
                  AND hwid IS NOT NULL
                  AND hwid != ''
                  AND hwid != '-'
                ORDER BY datetime(created_at) ASC, access_time ASC
                """,
                (uuid,)
            )
            rows = await cursor.fetchall()
            devices = []
            for row in rows:
                dev = dict(row)
                dev['model_raw'] = dev.get('model')
                dev['model'] = prettify_device_model(
                    dev.get('model'),
                    os=dev.get('os'),
                )
                dev['custom_name'] = dev.get('custom_name') or ''
                dev['app'] = classify_subscription_user_agent(dev.get('user_agent') or '')
                devices.append(dev)

        return JSONResponse({
            "uuid": uuid,
            "count": len(devices),
            "devices": devices
        })
    except Exception as e:
        logging.error(f"[devices] Ошибка запроса устройств для {uuid}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/devices/{uuid}/{hwid}", response_class=JSONResponse)
async def delete_device_endpoint(uuid: str, hwid: str, _: None = Depends(_local_only)):
    """
    Удаляет одно устройство (по hwid) пользователя (по uuid) из subscription_access.
    Только для запросов с localhost.
    """
    if not SUBSCRIPTION_LOG_AVAILABLE:
        return JSONResponse({"error": "devices module not available"}, status_code=503)

    if not hwid or len(hwid) < 4 or len(hwid) > 128:
        return JSONResponse({"ok": False, "error": "Invalid hwid"}, status_code=400)

    try:
        from devices.database import get_db_connection
        async with get_db_connection(read_only=False) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM subscription_access WHERE uuid = ? AND hwid = ?",
                (uuid, hwid)
            )
            row = await cursor.fetchone()
            if not row or row[0] == 0:
                return JSONResponse({"ok": False, "error": "Device not found"}, status_code=404)

            await db.execute(
                "DELETE FROM subscription_access WHERE uuid = ? AND hwid = ?",
                (uuid, hwid)
            )
            await db.commit()

        logging.info(f"[devices] Удалено устройство HWID={hwid[:20]}... для uuid={uuid}")
        return JSONResponse({"ok": True})
    except Exception as e:
        logging.error(f"[devices] Ошибка удаления устройства для {uuid}: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/devices/{uuid}/{hwid}/name", response_class=JSONResponse)
async def set_device_name_endpoint(uuid: str, hwid: str, request: Request, _: None = Depends(_local_only)):
    """
    Задаёт пользовательское имя устройства (uuid+hwid).
    Только для запросов с localhost. Имя санитизируется и режется до 15 символов.
    Тело: JSON {"custom_name": "..."}.
    """
    if not SUBSCRIPTION_LOG_AVAILABLE:
        return JSONResponse({"error": "devices module not available"}, status_code=503)

    if not hwid or len(hwid) < 4 or len(hwid) > 128:
        return JSONResponse({"ok": False, "error": "Invalid hwid"}, status_code=400)

    try:
        body = await request.body()
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
        raw_name = data.get('custom_name', '') if isinstance(data, dict) else ''

        from devices.database import set_device_custom_name, sanitize_device_name
        ok = await set_device_custom_name(uuid, hwid, raw_name)
        if not ok:
            return JSONResponse({"ok": False, "error": "Device not found"}, status_code=404)
        return JSONResponse({"ok": True, "custom_name": sanitize_device_name(raw_name)})
    except Exception as e:
        logging.error(f"[devices] Ошибка задания имени устройства для {uuid}: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── Кастомные тексты заглушек подписки (BLOCKED / DEVICE_LIMIT / EXPIRED) ─
# Хранятся в общем JSON settings.subpage_config (ключ stubs.*),
# редактируются в /settings/subpage. HWID/UA обрабатывает subpage;
# эти заглушки отдаёт xuiweb в JSON подписки — массивом links.
#
# Поведение BLOCKED / DEVICE_LIMIT:
#   • если список пуст → дефолтные фразы;
#   • каждая фраза → отдельная vless-заглушка (#fragment = текст).
#
# EXPIRED: то же + смешанный ввод — строка может быть полным URI подписки
# (vless://, vmess://, trojan://, ss://, hysteria2://, hy2://, tuic://), тогда
# она попадает в links без изменений.
_DEFAULT_STUBS_BLOCKED      = ['Вы были заблокированы']
_DEFAULT_STUBS_DEVICE_LIMIT = [
    '🇦🇱 Превышен лимит устройств',
    '🇦🇱 Увеличьте или удалите',
    '🇦🇱 Лишние уст-ва в боте',
]
_DEFAULT_STUBS_EXPIRED      = ['Подписка истекла']

_STUB_REASON_DEFAULTS: dict[str, list[str]] = {
    'BLOCKED': _DEFAULT_STUBS_BLOCKED,
    'DEVICE_LIMIT': _DEFAULT_STUBS_DEVICE_LIMIT,
    'EXPIRED': _DEFAULT_STUBS_EXPIRED,
}


async def _get_xuiweb_stub_texts(reason: str) -> list[str]:
    """Список строк заглушек из админки. reason ∈ BLOCKED | DEVICE_LIMIT | EXPIRED."""
    defaults = _STUB_REASON_DEFAULTS.get(reason) or _DEFAULT_STUBS_DEVICE_LIMIT
    raw = await get_db_setting_async('subpage_config', '')
    if not raw:
        return defaults
    try:
        cfg = json.loads(raw) or {}
    except (ValueError, TypeError):
        return defaults
    custom = (cfg.get('stubs') or {}).get(reason) or []
    if not isinstance(custom, list):
        return defaults
    cleaned = [str(x).strip() for x in custom if str(x).strip()]
    return cleaned or defaults


def _is_ready_made_subscription_uri(s: str) -> bool:
    low = (s or '').lstrip().lower()
    return any(
        low.startswith(p)
        for p in (
            'vless://',
            'vmess://',
            'trojan://',
            'ss://',
            'hysteria2://',
            'hy2://',
            'tuic://',
        )
    )


def _mixed_stub_entries_to_links(entries: list[str]) -> list[str]:
    """Текст → vless-заглушка с #fragment; готовый URI → как есть (до 10 шт.)."""
    from urllib.parse import quote as _q

    out: list[str] = []
    gen_i = 0
    for raw in entries[:10]:
        s = (raw or '').strip()
        if not s:
            continue
        if _is_ready_made_subscription_uri(s):
            out.append(s)
            continue
        i = gen_i % 10
        gen_i += 1
        out.append(
            f"vless://0000000{i}-0000-0000-0000-00000000000{i}@127.0.0.1:1"
            f"?security=none&type=tcp#{_q(s)}"
        )
    return out


def _build_stub_links(labels: list[str]) -> list[str]:
    """Только текстовые метки → vless-заглушки (совместимость с BLOCKED / DEVICE_LIMIT)."""
    return _mixed_stub_entries_to_links(labels)


def _hwid_limit_enforced(
    hwid_limit_enabled: str,
    limit_ip: int,
    device_hwid: str,
    device_user_agent: str,
) -> bool:
    """Лимит по HWID: VPN-клиент с переданным X-HWID и включённой настройкой."""
    return (
        SUBSCRIPTION_LOG_AVAILABLE
        and hwid_limit_enabled == '1'
        and limit_ip > 0
        and bool(device_hwid)
        and not is_browser(device_user_agent or '')
    )


async def _subscription_page_url(uuid: str) -> str:
    connect_url = await get_db_setting_async('sub_page_url', 'https://vpn.example.com')
    return f"{connect_url.rstrip('/')}/sub/{uuid}"


def _format_reg_type_label(sub_details: dict) -> str:
    """SITE — регистрация через сайт, TG — через Telegram (бот)."""
    v = str(sub_details.get('registration_type') or '').strip().lower()
    return 'SITE' if v == 'site' else 'TG'


def _api_user_identity(sub_details: dict) -> dict:
    """telegramId, email (users.email) и regType (TG/SITE) для API подписки."""
    tid = sub_details.get('telegram_id')
    try:
        telegram_id = int(tid) if tid is not None else 0
    except (ValueError, TypeError):
        telegram_id = 0
    return {
        'telegramId': telegram_id,
        'email': str(sub_details.get('email') or '').strip(),
        'regType': _format_reg_type_label(sub_details),
    }


async def _build_device_limit_response_dict(
    uuid: str,
    sub_details: dict,
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    subscription_url = await _subscription_page_url(uuid)
    stub_labels = await _get_xuiweb_stub_texts('DEVICE_LIMIT')
    stub_links = _build_stub_links(stub_labels)

    is_active = sub_details.get('sub_status') == 'Активна'
    days_left = sub_details.get('days_left', 0)

    expires_at_str = ''
    end_iso = sub_details.get('end_date_iso')
    if end_iso:
        try:
            dt_e = datetime.fromisoformat(end_iso)
            if dt_e.tzinfo is None:
                dt_e = dt_e.replace(tzinfo=timezone.utc)
            expires_at_str = dt_e.isoformat()
        except (ValueError, TypeError):
            expires_at_str = ''

    traffic_used_bytes = 0
    traffic_limit_bytes = 0
    traffic_used_str = '0'
    traffic_limit_str = '0'

    if sub_details.get('remnawave_short_uuid'):
        remnawave_data = await get_remnawave_subscription_data_async(
            sub_details['remnawave_short_uuid'], http_client
        )
        if remnawave_data and remnawave_data.get('user'):
            user_data = remnawave_data['user']
            traffic_used_bytes = user_data.get('trafficUsedBytes', 0)
            traffic_limit_bytes = user_data.get('trafficLimitBytes', 0)
            try:
                traffic_used_bytes = int(traffic_used_bytes) if traffic_used_bytes else 0
                traffic_limit_bytes = int(traffic_limit_bytes) if traffic_limit_bytes else 0
            except (ValueError, TypeError):
                traffic_used_bytes = 0
                traffic_limit_bytes = 0
            if traffic_limit_bytes > 0:
                traffic_used_gb = traffic_used_bytes / (1024 ** 3)
                traffic_limit_gb = traffic_limit_bytes / (1024 ** 3)
                traffic_used_str = f'{traffic_used_gb:.2f} GiB'
                traffic_limit_str = f'{traffic_limit_gb:.2f} GiB'
            else:
                traffic_limit_str = 'Unlimited'

    limit_ip = sub_details.get('limit_ip')
    limit_ip_display = str(limit_ip) if limit_ip and limit_ip > 0 else 'Без лимита'

    return {
        "isFound": True,
        "user": {
            "shortUuid": uuid,
            "daysLeft": days_left,
            "trafficUsed": traffic_used_str,
            "trafficLimit": traffic_limit_str,
            "lifetimeTrafficUsed": traffic_used_str,
            "lifetimeTrafficUsedBytes": str(traffic_used_bytes),
            "trafficLimitBytes": str(traffic_limit_bytes) if traffic_limit_bytes > 0 else "0",
            "trafficUsedBytes": str(traffic_used_bytes),
            "username": sub_details.get('username', ''),
            "expiresAt": expires_at_str,
            "isActive": is_active,
            "userStatus": "DEVICE_LIMIT",
            "trafficLimitStrategy": "NO_RESET",
            "limitIp": limit_ip or 0,
            "limitIpDisplay": limit_ip_display,
            **_api_user_identity(sub_details),
        },
        "links": stub_links,
        "ssConfLinks": {},
        "subscriptionUrl": subscription_url,
    }


async def _build_expired_response_dict(uuid: str, sub_details: dict) -> dict:
    exp_entries = await _get_xuiweb_stub_texts('EXPIRED')
    exp_links = _mixed_stub_entries_to_links(exp_entries)
    if not exp_links:
        exp_links = _mixed_stub_entries_to_links(_DEFAULT_STUBS_EXPIRED)

    expires_at_str = ''
    end_iso = sub_details.get('end_date_iso')
    if end_iso:
        try:
            dt_e = datetime.fromisoformat(end_iso)
            if dt_e.tzinfo is None:
                dt_e = dt_e.replace(tzinfo=timezone.utc)
            expires_at_str = dt_e.isoformat()
        except (ValueError, TypeError):
            expires_at_str = ''

    return {
        'isFound': True,
        'user': {
            'shortUuid': uuid,
            'daysLeft': 0,
            'trafficUsed': '0',
            'trafficLimit': '0',
            'lifetimeTrafficUsed': '0',
            'lifetimeTrafficUsedBytes': '0',
            'trafficLimitBytes': '0',
            'trafficUsedBytes': '0',
            'username': sub_details.get('username', ''),
            'expiresAt': expires_at_str,
            'isActive': False,
            'userStatus': 'EXPIRED',
            'trafficLimitStrategy': 'NO_RESET',
            **_api_user_identity(sub_details),
        },
        'links': exp_links,
        'ssConfLinks': {},
        'subscriptionUrl': await _subscription_page_url(uuid),
    }


def _schedule_device_access_log(
    uuid: str,
    request: Request,
    client_ip: str,
    device_hwid: str,
    device_user_agent: str,
    response_payload: dict,
) -> None:
    """Fire-and-forget запись устройства (вызывать только если HWID разрешён лимитом)."""
    payload_json = json.dumps(response_payload, ensure_ascii=False)
    asyncio.create_task(
        _log_device_access(
            uuid=uuid,
            ip_address=client_ip,
            hwid=device_hwid,
            os=request.headers.get('x-forwarded-device-os', '')
            or request.headers.get('X-Forwarded-Device-OS', ''),
            os_version=request.headers.get('x-forwarded-ver-os', '')
            or request.headers.get('X-Forwarded-Ver-OS', ''),
            model=request.headers.get('x-forwarded-device-model', '')
            or request.headers.get('X-Forwarded-Device-Model', ''),
            user_agent=device_user_agent,
            status_code=200,
            response_size=len(payload_json.encode('utf-8')),
        )
    )


@app.get("/api/settings/info", response_class=JSONResponse)
async def api_settings_info():
    """Конфиг страницы подписки (subpage), хранящийся в settings.subpage_config.

    Solid-and-stupid: всегда отдаёт то, что записано в админке. Решение,
    использовать этот конфиг или работать по своему .env, принимает САМ
    subpage по флагу API_CUSTOM_SETTINGS в его .env. Это правильный layering:
    единственный «потребитель» сам решает свою судьбу, а провайдер просто
    отдаёт данные.

    Если конфиг ещё не сохранён в админке — отдаём пустой скелет с дефолтными
    секциями (main/links/headers/stubs), subpage увидит пустые значения и
    воспользуется фолбэком на свой .env.

    Endpoint публичный (по решению владельца). Никаких чувствительных данных
    (типа bot_token, API-токенов панелей и т.п.) тут принципиально не отдаём.
    """
    try:
        raw_value = await get_db_setting_async('subpage_config', '')
        cfg: dict = {}
        if raw_value:
            try:
                parsed = json.loads(raw_value)
                if isinstance(parsed, dict):
                    cfg = parsed
            except (ValueError, TypeError):
                cfg = {}

        # Гарантируем, что верхнеуровневые секции всегда присутствуют —
        # это упрощает код subpage (он не делает .get на каждом поле).
        cfg.setdefault('main', {})
        cfg.setdefault('links', {})
        cfg.setdefault('headers', {})
        _st = cfg.setdefault('stubs', {})
        if not isinstance(_st, dict):
            _st = {}
            cfg['stubs'] = _st
        for _sk in ('HWID', 'UA', 'BLOCKED', 'DEVICE_LIMIT', 'EXPIRED'):
            _st.setdefault(_sk, [])
        cfg.setdefault('info', {})
        return JSONResponse({'cfg': cfg})
    except Exception as e:
        logging.error(f"[/api/settings/info] Ошибка получения конфига subpage: {e}", exc_info=True)
        return JSONResponse({'error': 'internal', 'message': str(e)}, status_code=500)


@app.get("/api/check-blocked/{telegram_id}", response_class=JSONResponse)
async def check_blocked(telegram_id: int):
    # API endpoint для проверки блокировки пользователя
    is_blocked = await check_user_blocked_async(telegram_id)
    return JSONResponse({"is_blocked": is_blocked})


def _apply_info_url_template(template: str, user_id: int | str) -> str:
    """Подставляет только {user_id} — ссылки задаются целиком в админке."""
    out = str(template or '').strip()
    if not out:
        return ''
    return out.replace('{user_id}', str(user_id)).strip()


async def _load_subpage_info_config() -> dict:
    raw_value = await get_db_setting_async('subpage_config', '')
    cfg: dict = {}
    if raw_value:
        try:
            parsed = json.loads(raw_value)
            if isinstance(parsed, dict):
                cfg = parsed
        except (ValueError, TypeError):
            cfg = {}
    info = cfg.get('info') if isinstance(cfg.get('info'), dict) else {}
    main = cfg.get('main') if isinstance(cfg.get('main'), dict) else {}
    defaults = {
        'enabled': True,
        'sections': {
            'subscription': True, 'devices': True, 'referral': True,
            'share': True, 'renew': True, 'faq': True,
        },
        'texts': {},
        'referral': {
            'link_bot': '',
            'link_site': '',
        },
        'renew': {'site_path': '/cabinet', 'bot_url': ''},
        'faq_items': [],
    }
    merged = {**defaults, **info}
    sec = info.get('sections') if isinstance(info.get('sections'), dict) else {}
    merged['sections'] = {**defaults['sections'], **sec}
    ref = info.get('referral') if isinstance(info.get('referral'), dict) else {}
    merged['referral'] = {
        'link_bot': str(ref.get('link_bot') or ref.get('link_bot_template') or '').strip(),
        'link_site': str(ref.get('link_site') or ref.get('link_site_template') or '').strip(),
    }
    renew = info.get('renew') if isinstance(info.get('renew'), dict) else {}
    merged['renew'] = {
        'site_path': str(renew.get('site_path') or '/cabinet').strip(),
        'bot_url': str(renew.get('bot_url') or renew.get('bot_url_template') or '').strip(),
    }
    texts = info.get('texts') if isinstance(info.get('texts'), dict) else {}
    merged['texts'] = texts
    faq = info.get('faq_items')
    merged['faq_items'] = faq if isinstance(faq, list) else []
    merged['_main'] = main
    return merged


async def _fetch_devices_for_info(uuid: str) -> tuple[list[dict], str | None]:
    """Список устройств без HWID/IP + ISO последнего access_time."""
    if not SUBSCRIPTION_LOG_AVAILABLE:
        return [], None
    try:
        from devices.database import get_db_connection
        async with get_db_connection(read_only=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT hwid, os, os_version, model, user_agent, access_time, created_at
                FROM subscription_access
                WHERE uuid = ?
                  AND hwid IS NOT NULL AND hwid != '' AND hwid != '-'
                ORDER BY datetime(access_time) DESC, datetime(created_at) DESC
                """,
                (uuid,),
            )
            rows = await cursor.fetchall()
        devices: list[dict] = []
        last_update: str | None = None
        for row in rows:
            dev = dict(row)
            access_time = str(dev.get('access_time') or '').strip()
            if access_time and not last_update:
                last_update = access_time
            devices.append({
                'os': str(dev.get('os') or '').strip(),
                'osVersion': str(dev.get('os_version') or '').strip(),
                'model': prettify_device_model(dev.get('model'), os=dev.get('os')),
                'userAgent': str(dev.get('user_agent') or '').strip()[:120],
                'accessTime': access_time,
                'createdAt': str(dev.get('created_at') or '').strip(),
            })
        return devices, last_update
    except Exception as e:
        logging.error(f'[/api/sub/info] devices for {uuid}: {e}')
        return [], None


async def _get_referral_count(telegram_id: int) -> int:
    conn = await _get_db_connection()
    if not conn or not telegram_id:
        return 0
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                'SELECT COUNT(*) FROM users WHERE invited_by = ?',
                (telegram_id,),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0
    finally:
        await conn.close()


async def _get_subscription_traffic_for_info(
    sub_details: dict,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[str, str, int, int]:
    traffic_used_str = '0'
    traffic_limit_str = 'Unlimited'
    traffic_used_bytes = 0
    traffic_limit_bytes = 0
    if sub_details.get('remnawave_short_uuid'):
        remnawave_data = await get_remnawave_subscription_data_async(
            sub_details['remnawave_short_uuid'], http_client,
        )
        if remnawave_data and remnawave_data.get('user'):
            user_data = remnawave_data['user']
            traffic_used_bytes = int(user_data.get('trafficUsedBytes') or 0)
            traffic_limit_bytes = int(user_data.get('trafficLimitBytes') or 0)
            if traffic_limit_bytes > 0:
                traffic_used_str = f'{traffic_used_bytes / (1024 ** 3):.2f} GiB'
                traffic_limit_str = f'{traffic_limit_bytes / (1024 ** 3):.2f} GiB'
            else:
                traffic_limit_str = 'Unlimited'
    return traffic_used_str, traffic_limit_str, traffic_used_bytes, traffic_limit_bytes


@app.get("/api/sub/{uuid}/info", response_class=JSONResponse)
async def api_get_subscription_info(uuid: str, request: Request):
    """Публичная сводка о подписке для /sub/{uuid}/info (без ключей и личных данных)."""
    info_cfg = await _load_subpage_info_config()
    main_cfg = info_cfg.get('_main') or {}
    if not info_cfg.get('enabled', True):
        return JSONResponse({'isFound': False, 'error': 'info_disabled'}, status_code=404)

    try:
        http_client = getattr(request.app.state, 'http_client', None)
        sub_details = await get_subscription_details_by_uuid_async(uuid)
        if not sub_details:
            return JSONResponse({'isFound': False})

        if sub_details.get('is_blocked'):
            return JSONResponse({
                'isFound': True,
                'userStatus': 'BLOCKED',
                'meta': {
                    'projectName': str(main_cfg.get('PROJECT_NAME') or '').strip(),
                    'supportUrl': str(main_cfg.get('SUPPORT_URL') or '').strip(),
                },
            })

        sections = info_cfg.get('sections') or {}
        texts = info_cfg.get('texts') or {}
        is_active = sub_details.get('sub_status') == 'Активна'
        limit_ip = int(sub_details.get('limit_ip') or 0)
        reg_type = _format_reg_type_label(sub_details)

        traffic_used_str, traffic_limit_str, traffic_used_bytes, traffic_limit_bytes = (
            await _get_subscription_traffic_for_info(sub_details, http_client)
        )

        expires_at_str = ''
        end_iso = sub_details.get('end_date_iso')
        if end_iso:
            try:
                dt_e = datetime.fromisoformat(end_iso)
                if dt_e.tzinfo is None:
                    dt_e = dt_e.replace(tzinfo=timezone.utc)
                expires_at_str = dt_e.isoformat()
            except (ValueError, TypeError):
                expires_at_str = ''

        devices_list: list[dict] = []
        last_subscription_update: str | None = None
        hwid_limit_enabled = (await get_db_setting_async('hwid_limit_enabled', '0')) == '1'
        if sections.get('devices') and hwid_limit_enabled:
            devices_list, last_subscription_update = await _fetch_devices_for_info(uuid)

        device_count = len(devices_list)
        slots_free = limit_ip <= 0 or device_count < limit_ip
        show_share = bool(sections.get('share') and is_active and slots_free)

        subscription_url = await _subscription_page_url(uuid)
        connect_url = subscription_url

        telegram_id = 0
        try:
            telegram_id = int(sub_details.get('telegram_id') or 0)
        except (ValueError, TypeError):
            telegram_id = 0

        bot_username = (await get_db_setting_async('bot_username', '') or '').strip().lstrip('@')
        site_url = (
            str(main_cfg.get('WEBSITE_URL') or '').strip()
            or (await get_db_setting_async('website_url', '') or '').strip()
        ).rstrip('/')

        ref_cfg = info_cfg.get('referral') or {}
        referral_block: dict | None = None
        if sections.get('referral') and telegram_id > 0:
            try:
                join_days = int(await get_db_setting_async('ref_bonus_on_join_days', '3'))
            except (ValueError, TypeError):
                join_days = 3
            try:
                payment_days = int(await get_db_setting_async('ref_bonus_on_payment_days', '7'))
            except (ValueError, TypeError):
                payment_days = 7
            link_bot = _apply_info_url_template(
                ref_cfg.get('link_bot') or ref_cfg.get('link_bot_template', ''),
                telegram_id,
            )
            link_site = _apply_info_url_template(
                ref_cfg.get('link_site') or ref_cfg.get('link_site_template', ''),
                telegram_id,
            )
            referral_block = {
                'linkBot': link_bot,
                'linkSite': link_site,
                'invitedCount': await _get_referral_count(telegram_id),
                'bonusJoinDays': join_days if join_days > 0 else None,
                'bonusPaymentDays': payment_days if payment_days > 0 else None,
                'hint': str(texts.get('referral_hint') or '').strip(),
            }

        renew_block: dict | None = None
        if sections.get('renew'):
            renew_cfg = info_cfg.get('renew') or {}
            if reg_type == 'SITE':
                # site_path может быть полной ссылкой (https://сайт/cabinet) — тогда
                # используем как есть; либо относительным путём (/cabinet) — клеим к WEBSITE_URL.
                site_renew = str(renew_cfg.get('site_path') or '').strip()
                if site_renew.startswith(('http://', 'https://')):
                    renew_url = site_renew
                elif site_url:
                    site_path = site_renew or '/cabinet'
                    if not site_path.startswith('/'):
                        site_path = '/' + site_path
                    renew_url = f'{site_url}{site_path}'
                else:
                    renew_url = ''
                renew_label = str(texts.get('renew_label_site') or 'Продлить на сайте').strip()
            else:
                renew_url = str(
                    renew_cfg.get('bot_url') or renew_cfg.get('bot_url_template') or ''
                ).strip()
                if not renew_url:
                    renew_url = f'https://t.me/{bot_username}' if bot_username else ''
                renew_label = str(texts.get('renew_label_tg') or 'Продлить в Telegram').strip()
            if renew_url:
                renew_block = {'label': renew_label, 'url': renew_url}

        faq_items = []
        if sections.get('faq'):
            for item in info_cfg.get('faq_items') or []:
                if not isinstance(item, dict) or not item.get('enabled', True):
                    continue
                title = str(item.get('title') or '').strip()
                if not title:
                    continue
                faq_items.append({
                    'id': str(item.get('id') or ''),
                    'title': title,
                    'heading': str(item.get('heading') or title).strip(),
                    'image': str(item.get('image') or '').strip(),
                    'html': str(item.get('html') or '').strip(),
                })

        response = {
            'isFound': True,
            'userStatus': 'BLOCKED' if sub_details.get('is_blocked') else (
                'EXPIRED' if not is_active else 'ACTIVE'
            ),
            'subscription': {
                'isActive': is_active,
                'daysLeft': int(sub_details.get('days_left') or 0),
                'timeLeftStr': str(sub_details.get('time_left_str') or '').strip(),
                'expiresAt': expires_at_str,
                'endDateMsk': str(sub_details.get('end_date_msk_str') or '').strip(),
                'subStatus': str(sub_details.get('sub_status') or '').strip(),
                'trafficUsed': traffic_used_str,
                'trafficLimit': traffic_limit_str,
                'trafficUsedBytes': traffic_used_bytes,
                'trafficLimitBytes': traffic_limit_bytes,
                'limitIp': limit_ip,
                'limitIpDisplay': str(sub_details.get('limit_ip_display') or 'Без лимита'),
                'deviceCount': device_count,
                'slotsFree': slots_free,
                'regType': reg_type,
                'lastSubscriptionUpdate': last_subscription_update,
            },
            'devices': {
                'enabled': bool(sections.get('devices') and hwid_limit_enabled),
                'count': device_count,
                'limit': limit_ip,
                'items': devices_list,
                'emptyText': str(texts.get('devices_empty') or '').strip(),
            },
            'referral': referral_block,
            'share': {
                'enabled': show_share,
                'subscriptionUrl': subscription_url if show_share else '',
                'hint': str(texts.get('share_hint') or '').strip(),
            },
            'renew': renew_block,
            'faq': faq_items,
            'meta': {
                'projectName': str(main_cfg.get('PROJECT_NAME') or '').strip(),
                'supportUrl': str(main_cfg.get('SUPPORT_URL') or '').strip(),
                'pageTitle': str(texts.get('page_title') or 'О подписке').strip(),
                'lastUpdateLabel': str(texts.get('last_update_label') or 'Последнее обновление подписки').strip(),
                'connectUrl': connect_url,
            },
        }
        return JSONResponse(response)
    except Exception as e:
        logging.error(f'[/api/sub/{uuid}/info] {e}', exc_info=True)
        return JSONResponse({'isFound': False, 'error': 'internal'}, status_code=500)


@app.get("/api/sub/{uuid}", response_class=JSONResponse)
async def api_get_subscription(uuid: str, request: Request):
    """
    API endpoint для получения данных подписки в формате JSON (Remnawave-совместимый формат).
    """
    # Извлекаем данные устройства из заголовков для логирования (до обработки запроса)
    device_hwid = request.headers.get('x-forwarded-hwid', '') or request.headers.get('X-Forwarded-HWID', '')
    device_user_agent = request.headers.get('x-forwarded-user-agent', '') or request.headers.get('X-Forwarded-User-Agent', '') or request.headers.get('user-agent', '')
    client_ip = request.headers.get('x-forwarded-for', '').split(',')[0].strip() if request.headers.get('x-forwarded-for') else ''
    if not client_ip and request.client:
        client_ip = request.client.host
    if not client_ip:
        client_ip = 'unknown'
    
    try:
        http_client = getattr(request.app.state, 'http_client', None)
        sub_details = await get_subscription_details_by_uuid_async(uuid)
        if not sub_details:
            # Логируем 404 для устройств
            if SUBSCRIPTION_LOG_AVAILABLE and (device_hwid or (device_user_agent and not is_browser(device_user_agent))):
                error_response = json.dumps({"isFound": False, "user": None, "links": [], "ssConfLinks": {}, "subscriptionUrl": None}, ensure_ascii=False)
                asyncio.create_task(
                    _log_device_access(
                        uuid=uuid,
                        ip_address=client_ip,
                        hwid=device_hwid,
                        os=request.headers.get('x-forwarded-device-os', '') or request.headers.get('X-Forwarded-Device-OS', ''),
                        os_version=request.headers.get('x-forwarded-ver-os', '') or request.headers.get('X-Forwarded-Ver-OS', ''),
                        model=request.headers.get('x-forwarded-device-model', '') or request.headers.get('X-Forwarded-Device-Model', ''),
                        user_agent=device_user_agent,
                        status_code=404,
                        response_size=len(error_response.encode('utf-8'))
                    )
                )
            return JSONResponse({
                "isFound": False,
                "user": None,
                "links": [],
                "ssConfLinks": {},
                "subscriptionUrl": None
            })
        
        # Проверяем блокировку пользователя
        if sub_details.get('is_blocked'):
            # Пользователь заблокирован — возвращаем кастомные заглушки
            # (редактируются в /settings/subpage → секция «Заглушки подписки»).
            _blocked_labels = await _get_xuiweb_stub_texts('BLOCKED')
            _blocked_links = _build_stub_links(_blocked_labels)

            connect_url = await get_db_setting_async('sub_page_url', 'https://vpn.example.com')
            subscription_url = f"{connect_url.rstrip('/')}/sub/{uuid}"
            
            blocked_response = {
                "isFound": True,
                "user": {
                    "shortUuid": uuid,
                    "daysLeft": 0,
                    "trafficUsed": "0",
                    "trafficLimit": "0",
                    "lifetimeTrafficUsed": "0",
                    "lifetimeTrafficUsedBytes": "0",
                    "trafficLimitBytes": "0",
                    "trafficUsedBytes": "0",
                    "username": sub_details.get('username', ''),
                    "expiresAt": "",
                    "isActive": False,
                    "userStatus": "BLOCKED",
                    "trafficLimitStrategy": "NO_RESET",
                    **_api_user_identity(sub_details),
                },
                "links": _blocked_links,
                "ssConfLinks": {},
                "subscriptionUrl": subscription_url
            }
            
            # Логируем блокировку для устройств
            if SUBSCRIPTION_LOG_AVAILABLE and (device_hwid or (device_user_agent and not is_browser(device_user_agent))):
                blocked_response_json = json.dumps(blocked_response, ensure_ascii=False)
                asyncio.create_task(
                    _log_device_access(
                        uuid=uuid,
                        ip_address=client_ip,
                        hwid=device_hwid,
                        os=request.headers.get('x-forwarded-device-os', '') or request.headers.get('X-Forwarded-Device-OS', ''),
                        os_version=request.headers.get('x-forwarded-ver-os', '') or request.headers.get('X-Forwarded-Ver-OS', ''),
                        model=request.headers.get('x-forwarded-device-model', '') or request.headers.get('X-Forwarded-Device-Model', ''),
                        user_agent=device_user_agent,
                        status_code=200,  # HTTP 200, но статус BLOCKED в данных
                        response_size=len(blocked_response_json.encode('utf-8'))
                    )
                )
            
            return JSONResponse(blocked_response)

        # ── Подписка истекла (дата в БД) ───────────────────────────────────────
        hwid_limit_enabled = await get_db_setting_async('hwid_limit_enabled', '0')
        limit_ip = int(sub_details.get('limit_ip') or 0)

        if sub_details.get('sub_status') == 'Истекла':
            # При включённом HWID-лимите: учтённые устройства → EXPIRED, новые сверх лимита → DEVICE_LIMIT.
            hwid_allowed = True
            if _hwid_limit_enforced(hwid_limit_enabled, limit_ip, device_hwid, device_user_agent):
                hwid_allowed = await check_hwid_allowed(uuid, device_hwid, limit_ip)
                if not hwid_allowed:
                    limit_response = await _build_device_limit_response_dict(uuid, sub_details, http_client)
                    return JSONResponse(limit_response)

            expired_response = await _build_expired_response_dict(uuid, sub_details)
            if (
                SUBSCRIPTION_LOG_AVAILABLE
                and hwid_allowed
                and (device_hwid or (device_user_agent and not is_browser(device_user_agent)))
            ):
                _schedule_device_access_log(
                    uuid, request, client_ip, device_hwid, device_user_agent, expired_response
                )

            return JSONResponse(expired_response)

        # ── HWID-лимит (активная подписка) ─────────────────────────────────────
        if _hwid_limit_enforced(hwid_limit_enabled, limit_ip, device_hwid, device_user_agent):
            if not await check_hwid_allowed(uuid, device_hwid, limit_ip):
                limit_response = await _build_device_limit_response_dict(uuid, sub_details, http_client)
                return JSONResponse(limit_response)

        short_uuid = sub_details.get('remnawave_short_uuid') or uuid
        if not sub_details.get('remnawave_short_uuid'):
            logging.warning(f"[API/sub] remnawave_short_uuid не задан для {uuid}, используем xui_client_uuid")

        remnawave_data = await get_remnawave_subscription_data_async(short_uuid, http_client)

        server_links = []
        if remnawave_data and remnawave_data.get('isFound'):
            server_links = _filter_remnawave_links(remnawave_data.get('links', []))

        # Добавляем дополнительные ссылки для активных подписок
        if sub_details and sub_details.get('sub_status') == 'Активна':
            if extra_links_json := await get_db_setting_async('sub_extra_links_active'):
                try:
                    for raw_link in json.loads(extra_links_json):
                        if raw_link:
                            server_links.append(str(raw_link).replace('[UUID]', uuid))
                except Exception:
                    pass
            if single_extra := await get_db_setting_async('sub_extra_link_active'):
                if single_extra.strip():
                    server_links.append(single_extra.strip().replace('[UUID]', uuid))

        if server_links and await get_db_setting_async('sub_link_dedup_enabled', '0') == '1':
            dedup_mode = await get_db_setting_async('sub_link_dedup_mode', 'random')
            before_dedup = len(server_links)
            dedup_mode_norm = (dedup_mode or '').strip().lower()
            host_stats = text_link_host_cache if dedup_mode_norm == 'online' else None
            online_threshold = 100
            if dedup_mode_norm == 'online':
                try:
                    online_threshold = int(
                        await get_db_setting_async('sub_link_dedup_online_threshold', '100'),
                    )
                except (TypeError, ValueError):
                    online_threshold = 100
            server_links = deduplicate_subscription_links(
                server_links,
                dedup_mode,
                host_stats=host_stats,
                online_threshold=online_threshold,
            )
            if len(server_links) != before_dedup:
                logging.info(
                    '[link_dedup] %s: %s → %s links (mode=%s)',
                    uuid, before_dedup, len(server_links), dedup_mode,
                )
        
        # Если нет ссылок, добавляем заглушку
        if not any(link.strip() and not link.strip().startswith('#') for link in server_links):
            if sub_details and sub_details.get('sub_status') == "Активна":
                if backup_link := await get_db_setting_async('sub_vless_bac'):
                    server_links.append(backup_link.strip())
                else:
                    server_links.append("vless://0000@127.0.0.1:443?security=tls#❌Сообщите в поддержку ошибка❗")
            else:
                server_links.append("vless://0000@127.0.0.1:443?security=tls#❌Подписка_неактивна_или_не_найдена")
        
        # Формируем данные пользователя
        end_date_iso = sub_details.get('end_date_iso')
        is_active = sub_details.get('sub_status') == 'Активна'
        days_left = sub_details.get('days_left', 0)
        
        # Форматируем дату окончания в ISO формат
        expires_at = None
        if end_date_iso:
            try:
                dt_obj = datetime.fromisoformat(end_date_iso)
                if dt_obj.tzinfo is None:
                    dt_obj = dt_obj.replace(tzinfo=timezone.utc)
                expires_at = dt_obj.isoformat()
            except (ValueError, TypeError):
                expires_at = None
        
        # Определяем статус пользователя
        user_status = "ACTIVE" if is_active else "EXPIRED"
        
        # Форматируем трафик
        traffic_used_bytes = 0
        traffic_limit_bytes = 0
        traffic_used_str = "0"
        traffic_limit_str = "Unlimited"
        
        # Данные о трафике из Remnawave
        if remnawave_data:
            user_data = remnawave_data.get('user', {})
            traffic_used_bytes = user_data.get('trafficUsedBytes', 0)
            traffic_limit_bytes = user_data.get('trafficLimitBytes', 0)
            try:
                traffic_used_bytes = int(traffic_used_bytes) if traffic_used_bytes else 0
                traffic_limit_bytes = int(traffic_limit_bytes) if traffic_limit_bytes else 0
            except (ValueError, TypeError):
                traffic_used_bytes = 0
                traffic_limit_bytes = 0
            
            if traffic_limit_bytes > 0:
                traffic_used_gb = traffic_used_bytes / (1024 ** 3)
                traffic_limit_gb = traffic_limit_bytes / (1024 ** 3)
                traffic_used_str = f"{traffic_used_gb:.2f} GiB"
                traffic_limit_str = f"{traffic_limit_gb:.2f} GiB"
            else:
                traffic_limit_str = "Unlimited"
        
        # Используем полный UUID как shortUuid
        short_uuid = uuid
        
        # Формируем URL подписки
        connect_url = await get_db_setting_async('sub_page_url', 'https://vpn.example.com')
        subscription_url = f"{connect_url.rstrip('/')}/sub/{uuid}"
        
        # Формируем limit_ip_display
        limit_ip = sub_details.get('limit_ip')
        limit_ip_display = str(limit_ip) if limit_ip and limit_ip > 0 else "Без лимита"
        
        # Xray JSON — для выбранных клиентов (User-Agent)
        xray_json_enabled = await get_db_setting_async('xray_json_enabled', '0')
        xray_balancer_enabled = await get_db_setting_async('xray_balancer_enabled', '0')
        json_client_markers = await _load_xray_json_client_markers()
        wants_xray_json = (
            xray_json_enabled == '1'
            and is_xray_json_client(device_user_agent, json_client_markers)
        )
        xray_config_result = None
        if wants_xray_json and sub_details.get('remnawave_short_uuid'):
            _rw_short_uuid = sub_details['remnawave_short_uuid']
            _rw_sdk = await _remnawave_sdk.get()
            if _rw_sdk and REMNAWAVE_SDK_AVAILABLE:
                try:
                    _rw_json_str = await _rw_sdk.subscription.get_subscription_by_client_type(
                        short_uuid=_rw_short_uuid,
                        client_type=RemnawaveClientType.JSON,
                    )
                    if _rw_json_str:
                        try:
                            xray_config_result = json.loads(_rw_json_str)
                        except Exception:
                            xray_config_result = _rw_json_str
                        logging.info(f"[REMNAWAVE] Xray JSON получен через SDK для {uuid} ({_rw_short_uuid})")
                except Exception as xe:
                    logging.warning(f"[REMNAWAVE] SDK ошибка Xray JSON, fallback на HTTP: {xe}")
            if xray_config_result is None:
                _rw_base_url = await get_db_setting_async('remnawave_base_url')
                _rw_token = await get_db_setting_async('remnawave_api_token')
                if _rw_base_url:
                    try:
                        _rw_api_url = f"{_rw_base_url.rstrip('/')}/api/sub/{_rw_short_uuid}/json"
                        _rw_headers = {"Accept": "application/json"}
                        if _rw_token:
                            _rw_headers["Authorization"] = f"Bearer {_rw_token}"
                        async with httpx.AsyncClient(timeout=10.0, verify=False) as _rw_client:
                            _rw_resp = await _rw_client.get(_rw_api_url, headers=_rw_headers)
                        if _rw_resp.status_code == 200:
                            try:
                                xray_config_result = _rw_resp.json()
                            except Exception:
                                xray_config_result = json.loads(_rw_resp.text)
                            logging.info(f"[REMNAWAVE] Xray JSON получен через HTTP для {uuid} ({_rw_short_uuid})")
                        else:
                            logging.warning(f"[REMNAWAVE] /api/sub/{_rw_short_uuid}/json вернул {_rw_resp.status_code}")
                    except Exception as xe:
                        logging.error(f"[REMNAWAVE] Ошибка получения Xray JSON для {uuid}: {xe}")

            if (
                xray_config_result is not None
                and xray_balancer_enabled == '1'
                and XRAY_BALANCER_AVAILABLE
            ):
                try:
                    _rw_base_url = await get_db_setting_async('remnawave_base_url')
                    _rw_token = await get_db_setting_async('remnawave_api_token')
                    _balancer_options = await _load_balancer_options()
                    logging.info(
                        '[BALANCER] settings dns=%s queryStrategy=%s domainStrategy=%s',
                        _balancer_options.balancer.dns_servers,
                        _balancer_options.balancer.dns_query_strategy,
                        _balancer_options.balancer.domain_strategy,
                    )
                    xray_config_result = await apply_xray_balancer(
                        xray_config_result,
                        rw_base_url=_rw_base_url,
                        rw_token=_rw_token,
                        http_client=http_client,
                        options=_balancer_options,
                    )
                    logging.info(f"[BALANCER] Xray JSON пересобран для {uuid}")
                except Exception as be:
                    logging.error(f"[BALANCER] Ошибка балансировки для {uuid}: {be}", exc_info=True)

        # Формируем ответ
        response_data = {
            "isFound": True,
            "user": {
                "shortUuid": short_uuid,
                "daysLeft": days_left,
                "trafficUsed": traffic_used_str,
                "trafficLimit": traffic_limit_str,
                "lifetimeTrafficUsed": traffic_used_str,
                "lifetimeTrafficUsedBytes": str(traffic_used_bytes),
                "trafficLimitBytes": str(traffic_limit_bytes) if traffic_limit_bytes > 0 else "0",
                "trafficUsedBytes": str(traffic_used_bytes),
                "username": sub_details.get('username', ''),
                "expiresAt": expires_at or "",
                "isActive": is_active,
                "userStatus": user_status,
                "trafficLimitStrategy": "NO_RESET",
                "limitIp": limit_ip or 0,
                "limitIpDisplay": limit_ip_display,
                **_api_user_identity(sub_details),
            },
            "links": server_links,
            "ssConfLinks": {},
            "subscriptionUrl": subscription_url,
            **({"xrayConfig": xray_config_result} if xray_config_result else {})
        }
        
        # Логируем данные устройства в БД (если доступен subscription_log_parser)
        # Делаем это асинхронно, чтобы не блокировать ответ API
        if SUBSCRIPTION_LOG_AVAILABLE:
            try:
                # Извлекаем данные устройства из заголовков, переданных от subpage
                device_hwid = request.headers.get('x-forwarded-hwid', '') or request.headers.get('X-Forwarded-HWID', '')
                device_os = request.headers.get('x-forwarded-device-os', '') or request.headers.get('X-Forwarded-Device-OS', '')
                device_os_ver = request.headers.get('x-forwarded-ver-os', '') or request.headers.get('X-Forwarded-Ver-OS', '')
                device_model = request.headers.get('x-forwarded-device-model', '') or request.headers.get('X-Forwarded-Device-Model', '')
                device_user_agent = request.headers.get('x-forwarded-user-agent', '') or request.headers.get('X-Forwarded-User-Agent', '') or request.headers.get('user-agent', '')
                
                # Получаем IP адрес клиента (из X-Forwarded-For или напрямую)
                client_ip = request.headers.get('x-forwarded-for', '').split(',')[0].strip() if request.headers.get('x-forwarded-for') else ''
                if not client_ip and request.client:
                    client_ip = request.client.host
                if not client_ip:
                    client_ip = 'unknown'
                
                # Записываем только если есть данные об устройстве (HWID или User-Agent указывает на VPN клиента)
                # Исключаем браузеры, чтобы не засорять БД
                if device_hwid or (device_user_agent and not is_browser(device_user_agent)):
                    # Вычисляем размер ответа
                    response_json_str = json.dumps(response_data, ensure_ascii=False)
                    response_size = len(response_json_str.encode('utf-8'))
                    
                    # Записываем в БД асинхронно (не блокируя ответ)
                    asyncio.create_task(
                        _log_device_access(
                            uuid=uuid,
                            ip_address=client_ip,
                            hwid=device_hwid,
                            os=device_os,
                            os_version=device_os_ver,
                            model=device_model,
                            user_agent=device_user_agent,
                            status_code=200,
                            response_size=response_size
                        )
                    )
            except Exception as log_err:
                # Логируем ошибку, но не прерываем выполнение
                logging.debug(f"Ошибка логирования устройства для {uuid}: {log_err}")
        
        return JSONResponse(response_data)
        
    except Exception as e:
        logging.error(f"Error in api_get_subscription for UUID {uuid}: {e}", exc_info=True)
        error_response_data = {
            "isFound": False,
            "user": None,
            "links": [],
            "ssConfLinks": {},
            "subscriptionUrl": None,
            "error": str(e)
        }
        
        # Логируем ошибку для устройств
        if SUBSCRIPTION_LOG_AVAILABLE and (device_hwid or (device_user_agent and not is_browser(device_user_agent))):
            error_response_json = json.dumps(error_response_data, ensure_ascii=False)
            asyncio.create_task(
                _log_device_access(
                    uuid=uuid,
                    ip_address=client_ip,
                    hwid=device_hwid,
                    os=request.headers.get('x-forwarded-device-os', '') or request.headers.get('X-Forwarded-Device-OS', ''),
                    os_version=request.headers.get('x-forwarded-ver-os', '') or request.headers.get('X-Forwarded-Ver-OS', ''),
                    model=request.headers.get('x-forwarded-device-model', '') or request.headers.get('X-Forwarded-Device-Model', ''),
                    user_agent=device_user_agent,
                    status_code=500,
                    response_size=len(error_response_json.encode('utf-8'))
                )
            )
        
        return JSONResponse(error_response_data, status_code=500)


@app.post("/api/sub/{uuid}/log-device", response_class=JSONResponse)
async def api_log_device_access(uuid: str, request: Request):
    """
    Endpoint для логирования устройства при ответе из кэша subpage.
    Subpage вызывает его fire-and-forget, когда отдаёт данные из кэша — чтобы xuiweb записал в subscription_access.db.
    """
    if not SUBSCRIPTION_LOG_AVAILABLE:
        return JSONResponse({"ok": True})
    try:
        device_hwid = request.headers.get('x-forwarded-hwid', '') or request.headers.get('X-Forwarded-HWID', '')
        device_os = request.headers.get('x-forwarded-device-os', '') or request.headers.get('X-Forwarded-Device-OS', '')
        device_os_ver = request.headers.get('x-forwarded-ver-os', '') or request.headers.get('X-Forwarded-Ver-OS', '')
        device_model = request.headers.get('x-forwarded-device-model', '') or request.headers.get('X-Forwarded-Device-Model', '')
        device_user_agent = request.headers.get('x-forwarded-user-agent', '') or request.headers.get('X-Forwarded-User-Agent', '') or request.headers.get('user-agent', '')
        client_ip = request.headers.get('x-forwarded-for', '').split(',')[0].strip() if request.headers.get('x-forwarded-for') else ''
        if not client_ip and request.client:
            client_ip = request.client.host
        if not client_ip:
            client_ip = 'unknown'
        if device_hwid or (device_user_agent and not is_browser(device_user_agent)):
            # HWID-лимит: не пишем «лишние» устройства (в т.ч. при EXPIRED из кэша subpage).
            if device_hwid and not is_browser(device_user_agent):
                hwid_limit_enabled = await get_db_setting_async('hwid_limit_enabled', '0')
                sub_details = await get_subscription_details_by_uuid_async(uuid)
                limit_ip = int(sub_details.get('limit_ip') or 0) if sub_details else 0
                if _hwid_limit_enforced(hwid_limit_enabled, limit_ip, device_hwid, device_user_agent):
                    if not await check_hwid_allowed(uuid, device_hwid, limit_ip):
                        logging.info(
                            f"[HWID] log-device: отклонён hwid={device_hwid[:16]}... "
                            f"для {uuid} (лимит {limit_ip})"
                        )
                        return JSONResponse({"ok": True, "blocked": True})

            # response_size берём из body если передан (subpage передаёт размер JSON)
            body = await request.body()
            try:
                data = json.loads(body) if body else {}
                response_size = data.get('response_size', 0)
            except Exception:
                response_size = 0
            await _log_device_access(
                uuid=uuid, ip_address=client_ip, hwid=device_hwid,
                os=device_os, os_version=device_os_ver, model=device_model,
                user_agent=device_user_agent, status_code=200, response_size=response_size
            )
    except Exception as e:
        logging.debug(f"Ошибка записи устройства для {uuid}: {e}")
    return JSONResponse({"ok": True})


if __name__ == '__main__':
    import uvicorn
    logging.info("Subscription API: модуль обработки подписок запущен.")
    uvicorn.run("run:app", host='127.0.0.1', port=8282)