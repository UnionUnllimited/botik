"""
Модуль для работы с базой данных устройств пользователей
"""
import asyncio
import aiosqlite
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from loguru import logger

from devices.config import DEVICES_DB_PATH

# ─── Write queue ─────────────────────────────────────────────────────────────
# Все записи идут через очередь → один фоновый воркер → нет lock-конкуренции.
_write_queue: asyncio.Queue = asyncio.Queue()
_writer_task: Optional[asyncio.Task] = None


_BATCH_SIZE = 50
_BATCH_TIMEOUT = 2.0  # секунд ожидания первой записи в батче

async def _writer_worker():
    """Фоновый воркер: батчинг записей из очереди → один INSERT/UPDATE на пачку."""
    logger.info("[DEVICES] Write worker запущен (batch mode)")
    _SQL = '''
        INSERT INTO subscription_access
            (uuid, ip_address, hwid, os, os_version, model, user_agent,
             access_time, method, path, status_code, response_size, access_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(uuid, hwid) DO UPDATE SET
            ip_address    = excluded.ip_address,
            os            = excluded.os,
            os_version    = excluded.os_version,
            model         = excluded.model,
            user_agent    = excluded.user_agent,
            access_time   = excluded.access_time,
            status_code   = excluded.status_code,
            response_size = excluded.response_size,
            access_count  = subscription_access.access_count + 1
    '''

    async with get_db_connection(read_only=False) as db:
        while True:
            # Ждём первую запись с таймаутом
            batch: list = []
            try:
                record = await asyncio.wait_for(_write_queue.get(), timeout=_BATCH_TIMEOUT)
            except asyncio.TimeoutError:
                continue

            if record is None:  # сигнал остановки
                _write_queue.task_done()
                break

            batch.append(record)

            # Добираем остаток очереди без ожидания (до лимита)
            while len(batch) < _BATCH_SIZE:
                try:
                    nxt = _write_queue.get_nowait()
                    if nxt is None:
                        # сигнал остановки пришёл в середине батча — запишем что собрали, потом выйдем
                        batch.append(None)
                        break
                    batch.append(nxt)
                except asyncio.QueueEmpty:
                    break

            stop_after = False
            real_records = []
            for item in batch:
                if item is None:
                    stop_after = True
                    _write_queue.task_done()
                else:
                    real_records.append(item)

            if real_records:
                params = [
                    (
                        r['uuid'], r['ip_address'], r['hwid'],
                        r['os'], r['os_version'], r['model'],
                        r['user_agent'], r['access_time'],
                        r['method'], r['path'],
                        r['status_code'], r['response_size'],
                    )
                    for r in real_records
                ]
                try:
                    await db.executemany(_SQL, params)
                    await db.commit()
                    logger.debug(f"[DEVICES] Батч записан: {len(real_records)} записей")
                    for _ in real_records:
                        _write_queue.task_done()
                except Exception as batch_err:
                    logger.warning(f"[DEVICES] Ошибка батч-записи ({len(real_records)} записей): {batch_err} — пробуем по одной")
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    # Fallback: пишем по одной записи, чтобы не терять данные из-за одной плохой
                    saved = 0
                    for rec_params in params:
                        try:
                            await db.execute(_SQL, rec_params)
                            await db.commit()
                            saved += 1
                        except Exception as single_err:
                            logger.error(f"[DEVICES] Не удалось записать строку uuid={rec_params[0]}: {single_err}")
                            try:
                                await db.rollback()
                            except Exception:
                                pass
                        finally:
                            _write_queue.task_done()
                    if saved:
                        logger.info(f"[DEVICES] Fallback: сохранено {saved}/{len(real_records)} записей")

            if stop_after:
                break


def _classify_user_agent(user_agent: str) -> str:
    """Классифицирует User-Agent → читаемое название приложения для статистики."""
    if not user_agent or not user_agent.strip():
        return 'Неизвестно'
    ua = user_agent.lower()
    browsers = ('mozilla', 'chrome', 'safari', 'firefox', 'edge', 'opera', 'yandex', 'brave', 'vivaldi')
    for b in browsers:
        if b in ua:
            return 'Браузер'
    # Порядок важен: более специфичные — первее
    _MAP = (
        ('happ',          'Happ'),
        ('brn-node',      'BRN-NODE'),
        ('brnnode',       'BRN-NODE'),
        ('v2raytun',      'V2RayTun'),
        ('hiddifynext',   'Hiddify Next'),
        ('hiddify',       'Hiddify'),
        ('streisand',     'Streisand'),
        ('v2box',         'V2Box'),
        ('incy',          'Incy'),
        ('foxray',        'FoxRay'),
        # Remnawave community forks
        ('koalaclash',    'Koala Clash'),
        ('koala-clash',   'Koala Clash'),
        ('koala',         'Koala Clash'),
        ('flclashx',      'FlClashX'),
        ('flclash',       'FlClashX'),
        ('prizrak',       'Prizrak-Box'),
        ('pandora-box',   'Prizrak-Box'),
        ('pandorabox',    'Prizrak-Box'),
        ('throne',        'Throne'),
        ('passwall',      'Passwall'),
        ('clashmi',       'Clash Mi'),
        ('clash-mi',      'Clash Mi'),
        ('karing',        'Karing'),
        # Go-based клиенты (UA = Go-http-client/x.x)
        # определяем их по OS/model, здесь ловим общий паттерн Go
        ('go-http-client', 'Go-клиент (Sing-box / Clash)'),
        # Широко известные
        ('shadowrocket',  'Shadowrocket'),
        ('quantumult',    'Quantumult X'),
        ('surge',         'Surge'),
        ('v2rayng',       'v2rayNG'),
        ('sing-box',      'Sing-box'),
        ('singbox',       'Sing-box'),
        ('clashmeta',     'Clash Meta'),
        ('clash.meta',    'Clash Meta'),
        ('clash',         'Clash'),
        ('nekoray',       'NekoRay'),
        ('matsuri',       'Matsuri'),
        ('shadowsocks',   'Shadowsocks'),
        ('outline',       'Outline'),
        ('psiphon',       'Psiphon'),
        ('trojan',        'Trojan'),
        ('v2fly',         'V2Fly'),
        ('xray',          'Xray'),
        ('v2ray',         'V2Ray'),
    )
    for keyword, name in _MAP:
        if keyword in ua:
            return name
    return 'Другие'


async def get_device_stats() -> dict:
    """Статистика устройств по ОС и приложениям (только приложения подписки, уникальные hwid)."""
    async with get_db_connection(read_only=True) as db:
        db.row_factory = aiosqlite.Row

        # ОС-статистика: все записи (включая устройства без HWID)
        cursor = await db.execute("""
            SELECT
                CASE WHEN os IS NULL OR os = '' THEN 'Неизвестно' ELSE os END AS os_name,
                COUNT(*) AS cnt
            FROM subscription_access
            GROUP BY os_name
            ORDER BY cnt DESC
        """)
        os_rows = await cursor.fetchall()

        # Приложения: все записи, группируем по user_agent, классифицируем в Python
        cursor = await db.execute("""
            SELECT user_agent, COUNT(*) AS cnt
            FROM subscription_access
            GROUP BY user_agent
        """)
        ua_rows = await cursor.fetchall()

        # Общая сводка
        cursor = await db.execute("""
            SELECT
                COUNT(*) AS total_records,
                COUNT(DISTINCT uuid) AS total_users,
                COUNT(CASE WHEN hwid != '' THEN hwid END) AS total_hwids
            FROM subscription_access
        """)
        total_row = await cursor.fetchone()

    os_stats = [{'name': row['os_name'], 'count': row['cnt']} for row in os_rows]

    app_counts: dict[str, int] = {}
    for row in ua_rows:
        app_name = _classify_user_agent(row['user_agent'] or '')
        app_counts[app_name] = app_counts.get(app_name, 0) + row['cnt']
    app_stats = sorted(
        [{'name': k, 'count': v} for k, v in app_counts.items()],
        key=lambda x: x['count'],
        reverse=True,
    )

    return {
        'os_stats': os_stats,
        'app_stats': app_stats,
        'total_records': total_row['total_records'] if total_row else 0,
        'total_users':   total_row['total_users']   if total_row else 0,
        'total_hwids':   total_row['total_hwids']   if total_row else 0,
    }


def _new_app_device_hwid_clause() -> str:
    """Устройство с реальным HWID (как в списках клиента)."""
    return """
        TRIM(COALESCE(hwid, '')) != ''
        AND LOWER(TRIM(hwid)) != '-'
    """


def _first_seen_on_utc_today_clause() -> str:
    """
    День первой записи устройства в таблице (created_at), календарный UTC.
    Не использовать access_time — он обновляется при каждом обращении к подписке.
    """
    return """
        COALESCE(TRIM(created_at), '') != ''
        AND date(created_at) = date('now', 'utc')
    """


async def count_new_app_devices_first_seen_today() -> int:
    """Сколько новых (первый INSERT) устройств с HWID за текущие сутки UTC."""
    app_filter = _get_app_user_agents_sql_filter()
    hwid_clause = _new_app_device_hwid_clause()
    date_clause = _first_seen_on_utc_today_clause()
    sql = f'''
        SELECT COUNT(*) AS cnt
        FROM subscription_access
        WHERE {hwid_clause}
          AND {date_clause}
          {app_filter}
    '''
    async with get_db_connection(read_only=True) as db:
        cursor = await db.execute(sql)
        row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0


async def get_new_app_devices_first_seen_today_paginated(
    page: int = 1,
    per_page: int = 20,
) -> Dict:
    """
    Новые устройства за сегодня (UTC): первая запись uuid+hwid с фильтром приложений подписки.
    Сортировка: самые свежие первые по времени первого появления.
    """
    page = max(1, page)
    per_page = max(1, min(per_page, 100))
    offset = (page - 1) * per_page
    app_filter = _get_app_user_agents_sql_filter()
    hwid_clause = _new_app_device_hwid_clause()
    date_clause = _first_seen_on_utc_today_clause()

    base_where = f'''
        WHERE {hwid_clause}
          AND {date_clause}
          {app_filter}
    '''

    async with get_db_connection(read_only=True) as db:
        db.row_factory = aiosqlite.Row

        cur_total = await db.execute(
            f'SELECT COUNT(*) FROM subscription_access {base_where}',
        )
        total_row = await cur_total.fetchone()
        total = int(total_row[0]) if total_row and total_row[0] is not None else 0

        cur = await db.execute(f'''
            SELECT uuid, hwid, os, os_version, model, user_agent, ip_address,
                   access_time, created_at, access_count
            FROM subscription_access
            {base_where}
            ORDER BY datetime(created_at) DESC
            LIMIT ? OFFSET ?
        ''', (per_page, offset))
        rows = await cur.fetchall()

    items = []
    for row in rows:
        hwid_val = (row['hwid'] or '').strip()
        if hwid_val == '-':
            hwid_val = ''
        first_ts = row['created_at'] or ''
        items.append({
            'uuid': row['uuid'] or '',
            'hwid': hwid_val,
            'os': row['os'] or '',
            'os_version': row['os_version'] or '',
            'model': row['model'] or '',
            'user_agent': row['user_agent'] or '',
            'ip_address': row['ip_address'] or '',
            'access_time': row['access_time'] or '',
            'created_at': row['created_at'] or '',
            'first_seen_raw': first_ts,
            'access_count': row['access_count'] or 1,
        })

    pages = max(1, (total + per_page - 1) // per_page)
    return {
        'items': items,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': pages,
    }


def _parse_access_time_utc(value: str | None) -> datetime | None:
    if not value or not str(value).strip():
        return None
    try:
        s = str(value).replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _subscription_stale_cutoff(*, stale_hours: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=max(1, int(stale_hours)))


def _is_subscription_access_stale(last_access: str | None, *, stale_hours: int) -> bool:
    dt = _parse_access_time_utc(last_access)
    if dt is None:
        return True
    return dt < _subscription_stale_cutoff(stale_hours=stale_hours)


async def _get_active_subscription_users() -> List[Dict]:
    """Активные подписки из router_bot.db (не истекли, не заблокированы, есть UUID)."""
    import db_helpers
    return await db_helpers.get_active_subscription_users(require_uuid=True)


async def get_max_app_subscription_access_by_uuid() -> Dict[str, str]:
    """Последнее обращение приложения подписки по uuid."""
    app_filter = _get_app_user_agents_sql_filter()
    async with get_db_connection(read_only=True) as db:
        cursor = await db.execute(f'''
            SELECT uuid, MAX(access_time) AS last_access
            FROM subscription_access
            WHERE TRIM(COALESCE(uuid, '')) != ''
              {app_filter}
            GROUP BY uuid
        ''')
        rows = await cursor.fetchall()
    return {
        str(row[0]): str(row[1])
        for row in rows
        if row[0] and row[1]
    }


async def count_stale_active_subscription_users(*, stale_hours: int = 24) -> Dict:
    """
    Активные клиенты, не обновлявшие подписку дольше stale_hours (обращения приложений).
    Без записей в subscription_access считаются «не обновляли».
    """
    active = await _get_active_subscription_users()
    last_map = await get_max_app_subscription_access_by_uuid()
    stale = sum(
        1 for u in active
        if _is_subscription_access_stale(last_map.get(u['uuid']), stale_hours=stale_hours)
    )
    return {
        'active_subscription_users': len(active),
        'stale_subscription_users': stale,
        'stale_hours': int(stale_hours),
    }


async def get_stale_active_subscription_users_paginated(
    *,
    page: int = 1,
    per_page: int = 20,
    stale_hours: int = 24,
) -> Dict:
    """Список активных клиентов без обновления подписки > stale_hours."""
    page = max(1, page)
    per_page = max(1, min(per_page, 100))
    active = await _get_active_subscription_users()
    last_map = await get_max_app_subscription_access_by_uuid()

    stale_rows: List[Dict] = []
    for u in active:
        last_raw = last_map.get(u['uuid']) or ''
        if not _is_subscription_access_stale(last_raw or None, stale_hours=stale_hours):
            continue
        stale_rows.append({
            **u,
            'last_access_raw': last_raw,
            'never_accessed': not bool(last_raw),
        })

    stale_rows.sort(
        key=lambda row: (
            0 if row['never_accessed'] else 1,
            row['last_access_raw'] or '',
        ),
    )

    total = len(stale_rows)
    offset = (page - 1) * per_page
    items = stale_rows[offset:offset + per_page]
    pages = max(1, (total + per_page - 1) // per_page)
    return {
        'items': items,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': pages,
        'stale_hours': int(stale_hours),
        'active_subscription_users': len(active),
    }


def classify_subscription_user_agent(user_agent: str) -> str:
    """Публичная обёртка для подписок к классификатору приложений."""
    return _classify_user_agent(user_agent or '')


def _is_app_user_agent(user_agent: str) -> bool:
    """Проверяет, является ли User-Agent приложением подписки (исключает браузеры)"""
    if not user_agent or user_agent == '':
        return False

    ua = user_agent.lower()
    app_user_agents = [
        'happ', 'brn-node', 'brnnode', 'v2raytun', 'v2ray', 'hiddify', 'hiddifynext',
        'clash', 'clashmeta', 'shadowrocket', 'quantumult', 'surge',
        'v2rayng', 'sing-box', 'singbox', 'v2fly', 'xray', 'trojan',
        'shadowsocks',
    ]
    browsers = [
        'mozilla', 'chrome', 'safari', 'firefox', 'edge',
        'opera', 'yandex', 'brave', 'vivaldi',
    ]
    for b in browsers:
        if b in ua:
            return False
    for c in app_user_agents:
        if c in ua:
            return True
    return True


def _subscription_requests_visibility_sql() -> str:
    """Подзапрос для списка «Запросы подписки»: всё, кроме браузеров и Telegram (логика как subpage/run.py is_browser)."""
    return """
        AND NOT (
            LOWER(COALESCE(user_agent, '')) LIKE '%telegram%'
        )
        AND NOT (
            (
                (
                    LOWER(COALESCE(user_agent,'')) LIKE '%mozilla%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%chrome%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%safari%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%firefox%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%edge%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%opera%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%msie%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%yabrowser%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%ya-browser%'
                )
                AND NOT (
                    LOWER(COALESCE(user_agent,'')) LIKE '%clash%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%v2ray%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%v2raytun%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%v2rayn%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%v2rayng%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%v2box%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%sing-box%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%singbox%'
                    OR                     LOWER(COALESCE(user_agent,'')) LIKE '%shadowrocket%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%happ%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%brn-node%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%brnnode%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%incy%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%hiddify%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%shadowsocks%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%nekoray%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%nekobox%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%mihomo%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%clash-verge%'
                    OR LOWER(COALESCE(user_agent,'')) LIKE '%clash-meta%'
                )
            )
        )
    """


def _get_app_user_agents_sql_filter() -> str:
    """SQL-условие для фильтрации только приложений подписки"""
    return """
        AND (
            NOT (
                LOWER(user_agent) LIKE '%mozilla%'
                OR LOWER(user_agent) LIKE '%chrome%'
                OR LOWER(user_agent) LIKE '%safari%'
                OR LOWER(user_agent) LIKE '%firefox%'
                OR LOWER(user_agent) LIKE '%edge%'
                OR LOWER(user_agent) LIKE '%opera%'
                OR LOWER(user_agent) LIKE '%yandex%'
                OR LOWER(user_agent) LIKE '%brave%'
                OR LOWER(user_agent) LIKE '%vivaldi%'
                OR LOWER(user_agent) LIKE '%webkit%'
            )
            AND NOT (
                LOWER(user_agent) LIKE '%telegrambot%'
                OR LOWER(user_agent) LIKE '%twitterbot%'
                OR LOWER(user_agent) LIKE '%bot%'
                OR LOWER(user_agent) LIKE '%crawler%'
                OR LOWER(user_agent) LIKE '%spider%'
                OR LOWER(user_agent) LIKE '%scraper%'
            )
            AND (
                LOWER(user_agent) LIKE '%happ%'
                OR LOWER(user_agent) LIKE '%brn-node%'
                OR LOWER(user_agent) LIKE '%brnnode%'
                OR LOWER(user_agent) LIKE '%v2raytun%'
                OR LOWER(user_agent) LIKE '%v2ray%'
                OR LOWER(user_agent) LIKE '%hiddify%'
                OR LOWER(user_agent) LIKE '%hiddifynext%'
                OR LOWER(user_agent) LIKE '%clash%'
                OR LOWER(user_agent) LIKE '%clashmeta%'
                OR LOWER(user_agent) LIKE '%shadowrocket%'
                OR LOWER(user_agent) LIKE '%quantumult%'
                OR LOWER(user_agent) LIKE '%surge%'
                OR LOWER(user_agent) LIKE '%v2rayng%'
                OR LOWER(user_agent) LIKE '%sing-box%'
                OR LOWER(user_agent) LIKE '%singbox%'
                OR LOWER(user_agent) LIKE '%v2fly%'
                OR LOWER(user_agent) LIKE '%xray%'
                OR LOWER(user_agent) LIKE '%trojan%'
                OR LOWER(user_agent) LIKE '%shadowsocks%'
                OR (user_agent IS NULL AND hwid IS NOT NULL AND hwid != '' AND LENGTH(hwid) > 0)
                OR (user_agent = '' AND hwid IS NOT NULL AND hwid != '' AND LENGTH(hwid) > 0)
            )
        )
    """


@asynccontextmanager
async def get_db_connection(read_only: bool = False):
    """Контекстный менеджер подключения к devices.db"""
    if not read_only:
        os.makedirs(os.path.dirname(DEVICES_DB_PATH), exist_ok=True)

    if read_only:
        conn = await aiosqlite.connect(f'file:{DEVICES_DB_PATH}?mode=ro', uri=True, timeout=30)
    else:
        conn = await aiosqlite.connect(DEVICES_DB_PATH, timeout=30)

    try:
        if not read_only:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.execute("PRAGMA busy_timeout=30000;")
        if not read_only:
            await conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
    finally:
        try:
            await conn.close()
        except Exception as e:
            logger.debug(f"Ошибка закрытия соединения: {e}")
        if hasattr(conn, '_connection') and conn._connection:
            try:
                conn._connection.close()
            except Exception:
                pass
        await asyncio.sleep(0.01)


async def init_database():
    """Инициализация базы данных устройств и запуск write-воркера"""
    global _writer_task

    async with get_db_connection() as db:
        # Основная таблица — одна строка на устройство (uuid+hwid уникальны)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS subscription_access (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                hwid TEXT NOT NULL DEFAULT '',
                os TEXT,
                os_version TEXT,
                model TEXT,
                user_agent TEXT,
                access_time TEXT NOT NULL,
                method TEXT,
                path TEXT,
                status_code INTEGER,
                response_size INTEGER,
                access_count INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(uuid, hwid)
            )
        ''')
        # Миграция: добавить access_count если таблица уже существует без неё
        try:
            await db.execute('ALTER TABLE subscription_access ADD COLUMN access_count INTEGER NOT NULL DEFAULT 1')
        except Exception:
            pass
        # Миграция: created_at для старых БД (без столбца или с пустым значением)
        try:
            await db.execute(
                'ALTER TABLE subscription_access ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP'
            )
        except Exception:
            pass
        # Миграция: пользовательское имя устройства (задаётся клиентом из бота)
        try:
            await db.execute('ALTER TABLE subscription_access ADD COLUMN custom_name TEXT')
        except Exception:
            pass
        # Одноразово заполняем пустой created_at текущим access_time — дальше он не меняется при UPSERT.
        # Иначе отчёт «новые за сегодня» ошибочно опирался бы на последнее обновление подписки.
        await db.execute('''
            UPDATE subscription_access
            SET created_at = access_time
            WHERE COALESCE(TRIM(created_at), '') = ''
        ''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_uuid ON subscription_access(uuid)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_access_time ON subscription_access(access_time)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_hwid ON subscription_access(hwid)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_uuid_hwid ON subscription_access(uuid, hwid)')
        await db.execute(
            'CREATE INDEX IF NOT EXISTS idx_subscription_access_created_at ON subscription_access(created_at)'
        )
        await db.commit()
        logger.info(f"База данных устройств инициализирована: {DEVICES_DB_PATH}")

    # Запускаем write-воркер если ещё не запущен
    if _writer_task is None or _writer_task.done():
        _writer_task = asyncio.ensure_future(_writer_worker())
        logger.info("[DEVICES] Write worker запущен")


async def insert_access_record(
    uuid: str,
    ip_address: str,
    hwid: str,
    os: str,
    os_version: str,
    model: str,
    user_agent: str,
    access_time: str,
    method: str,
    path: str,
    status_code: int,
    response_size: int,
):
    """Кладёт запись в очередь — write-воркер запишет в БД без блокировок."""
    await _write_queue.put({
        'uuid': uuid,
        'ip_address': ip_address,
        'hwid': (hwid or '').strip(),
        'os': os or '',
        'os_version': os_version or '',
        'model': model or '',
        'user_agent': user_agent or '',
        'access_time': access_time,
        'method': method,
        'path': path,
        'status_code': status_code,
        'response_size': response_size,
    })


DEVICE_NAME_MAX_LEN = 15


def sanitize_device_name(raw: str) -> str:
    """Приводит пользовательское имя устройства к безопасному виду.

    Записи в БД идут через параметризованные запросы (SQL-инъекция невозможна),
    но дополнительно чистим ввод: убираем управляющие символы, угловые скобки
    (безопасность для HTML-вывода), схлопываем пробелы и режем до лимита.
    """
    s = str(raw or '')
    s = re.sub(r'[\x00-\x1f\x7f]', ' ', s)   # управляющие символы / переводы строк
    s = s.replace('<', '').replace('>', '')  # чтобы не ломать HTML-разметку сообщений
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:DEVICE_NAME_MAX_LEN]


async def set_device_custom_name(uuid: str, hwid: str, custom_name: str) -> bool:
    """Задаёт/очищает пользовательское имя устройства (uuid+hwid). True — если строка найдена."""
    name = sanitize_device_name(custom_name)
    async with get_db_connection(read_only=False) as db:
        cursor = await db.execute(
            "UPDATE subscription_access SET custom_name = ? WHERE uuid = ? AND hwid = ?",
            (name or None, uuid, hwid),
        )
        await db.commit()
        updated = cursor.rowcount > 0
    if updated:
        logger.info(f"[DEVICES] custom_name обновлён uuid={uuid} hwid={hwid[:16]}... -> {name!r}")
    return updated


async def get_devices_by_uuid(uuid: str) -> List[Dict]:
    """Уникальные устройства пользователя (одна строка = одно устройство)"""
    async with get_db_connection(read_only=True) as db:
        db.row_factory = aiosqlite.Row
        app_filter = _get_app_user_agents_sql_filter()

        cursor = await db.execute(f'''
            SELECT hwid, os, os_version, model, user_agent, ip_address,
                   access_time, access_count, created_at, custom_name
            FROM subscription_access
            WHERE uuid = ?
                {app_filter}
            ORDER BY datetime(created_at) ASC, access_time ASC
        ''', (uuid,))

        rows = await cursor.fetchall()
        result = []
        for row in rows:
            hwid_value = (row['hwid'] or '').strip()
            if hwid_value == '-':
                hwid_value = ''
            created = row['created_at']
            last_t = row['access_time']
            result.append({
                'hwid': hwid_value,
                'os': row['os'] or '',
                'os_version': row['os_version'] or '',
                'model': row['model'] or '',
                'user_agent': row['user_agent'] or '',
                'ip_address': row['ip_address'] or '',
                'last_access': last_t,
                'first_access': (created or last_t or ''),
                'access_count': row['access_count'] or 1,
                'custom_name': row['custom_name'] or '',
            })
        return result


async def get_latest_access_by_uuid(uuid: str) -> Optional[Dict]:
    """Последнее обращение к подписке (только приложения подписки)"""
    async with get_db_connection(read_only=True) as db:
        db.row_factory = aiosqlite.Row
        app_filter = _get_app_user_agents_sql_filter()

        cursor = await db.execute(f'''
            SELECT
                id, uuid, ip_address, hwid, os, os_version, model, user_agent,
                access_time, method, path, status_code, response_size, created_at, custom_name
            FROM subscription_access
            WHERE uuid = ?
                {app_filter}
            ORDER BY access_time DESC
            LIMIT 1
        ''', (uuid,))

        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_recent_access_records(limit: int = 30) -> List[Dict]:
    """Последние N записей обращений к подпискам"""
    async with get_db_connection(read_only=True) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT
                id, uuid, ip_address, hwid, os, os_version, model, user_agent,
                access_time, method, path, status_code, response_size, created_at
            FROM subscription_access
            ORDER BY access_time DESC
            LIMIT ?
        ''', (limit,))

        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_subscription_requests_paginated(
    uuid: str,
    page: int = 1,
    per_page: int = 30,
) -> Dict:
    """Запросы подписки для uuid с пагинацией: не браузер и не Telegram; включает записи без hwid."""
    page = max(1, page)
    offset = (page - 1) * per_page
    vis = _subscription_requests_visibility_sql()

    async with get_db_connection(read_only=True) as db:
        db.row_factory = aiosqlite.Row

        count_cursor = await db.execute(
            f'SELECT COUNT(*) FROM subscription_access WHERE uuid = ? {vis}', (uuid,)
        )
        total = (await count_cursor.fetchone())[0]

        cursor = await db.execute(f'''
            SELECT hwid, os, os_version, model, user_agent, ip_address,
                   access_time, access_count, status_code, method, path
            FROM subscription_access
            WHERE uuid = ?
                {vis}
            ORDER BY access_time DESC
            LIMIT ? OFFSET ?
        ''', (uuid, per_page, offset))

        rows = await cursor.fetchall()
        items = []
        for row in rows:
            hwid_val = (row['hwid'] or '').strip()
            if hwid_val == '-':
                hwid_val = ''
            items.append({
                'hwid': hwid_val,
                'os': row['os'] or '',
                'os_version': row['os_version'] or '',
                'model': row['model'] or '',
                'user_agent': row['user_agent'] or '',
                'ip_address': row['ip_address'] or '',
                'access_time': row['access_time'] or '',
                'access_count': row['access_count'] or 1,
                'status_code': row['status_code'],
                'method': row['method'] or '',
                'path': row['path'] or '',
            })

        return {
            'items': items,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': max(1, (total + per_page - 1) // per_page),
        }


async def get_device_violations(include_hwid_list: bool = False) -> List[Dict]:
    """Список UUID с количеством уникальных устройств (для проверки нарушителей лимита)"""
    async with get_db_connection(read_only=True) as db:
        db.row_factory = aiosqlite.Row
        app_filter = _get_app_user_agents_sql_filter()

        cursor = await db.execute(f'''
            SELECT
                uuid,
                COUNT(DISTINCT
                    CASE
                        WHEN hwid IS NOT NULL AND hwid != '' AND LENGTH(hwid) > 0 AND hwid != '-'
                        THEN hwid
                        ELSE COALESCE(user_agent, '') || '|' || COALESCE(os, '') || '|' || COALESCE(model, '')
                    END
                ) as device_count,
                MIN(access_time) as first_access,
                MAX(access_time) as last_access
            FROM subscription_access
            WHERE uuid IS NOT NULL AND uuid != ''
                {app_filter}
            GROUP BY uuid
            HAVING device_count > 0
            ORDER BY device_count DESC
        ''')

        rows = await cursor.fetchall()
        violations = []

        for row in rows:
            try:
                uuid = row['uuid']
                device_count = row['device_count']
                first_access = row['first_access']
                last_access = row['last_access']
            except (KeyError, TypeError, IndexError):
                continue

            if not uuid or device_count == 0:
                continue

            violation = {
                'uuid': uuid,
                'device_count': device_count,
                'first_access': first_access,
                'last_access': last_access,
                'hwid_list': [],
            }

            if include_hwid_list:
                hwid_list = await get_hwid_list_for_uuid(uuid)
                violation['hwid_list'] = hwid_list
                violation['device_count'] = len(hwid_list)

            violations.append(violation)

        return violations


async def get_hwid_list_for_uuid(uuid: str) -> List[str]:
    """Список уникальных идентификаторов устройств для UUID"""
    app_filter = _get_app_user_agents_sql_filter()

    async with get_db_connection(read_only=True) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(f'''
            SELECT DISTINCT
                CASE
                    WHEN hwid IS NOT NULL AND hwid != '' AND LENGTH(hwid) > 0 AND hwid != '-'
                    THEN hwid
                    ELSE COALESCE(user_agent, '') || '|' || COALESCE(os, '') || '|' || COALESCE(model, '')
                END as device_id
            FROM subscription_access
            WHERE uuid = ?
                {app_filter}
        ''', (uuid,))

        rows = await cursor.fetchall()
        device_list = []
        for row in rows:
            try:
                device_id = row['device_id']
            except (KeyError, TypeError, IndexError):
                continue
            if device_id:
                clean = device_id.strip()
                if clean and clean != '-':
                    device_list.append(clean)

        logger.debug(f"UUID {uuid}: уникальных устройств={len(device_list)}")
        return device_list


async def get_duplicate_hwids() -> List[Dict]:
    """HWID, встречающиеся у нескольких UUID (только реальные HWID, приложения подписки)."""
    app_filter = _get_app_user_agents_sql_filter()

    async with get_db_connection(read_only=True) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(f'''
            SELECT hwid, COUNT(DISTINCT uuid) AS client_count
            FROM subscription_access
            WHERE hwid IS NOT NULL AND hwid != '' AND hwid != '-'
                AND LENGTH(hwid) > 0
                AND uuid IS NOT NULL AND uuid != ''
                {app_filter}
            GROUP BY hwid
            HAVING client_count > 1
            ORDER BY client_count DESC, hwid
        ''')
        dup_rows = await cursor.fetchall()
        if not dup_rows:
            return []

        hwids = [row['hwid'] for row in dup_rows]
        placeholders = ','.join(['?'] * len(hwids))

        cursor = await db.execute(f'''
            SELECT
                hwid,
                uuid,
                MIN(access_time) AS first_access,
                MAX(access_time) AS last_access,
                MAX(os) AS os,
                MAX(model) AS model,
                MAX(user_agent) AS user_agent,
                SUM(COALESCE(access_count, 1)) AS total_accesses
            FROM subscription_access
            WHERE hwid IN ({placeholders})
                {app_filter}
            GROUP BY hwid, uuid
            ORDER BY hwid, last_access DESC
        ''', tuple(hwids))

        detail_rows = await cursor.fetchall()

    by_hwid: Dict[str, Dict] = {
        row['hwid']: {
            'hwid': row['hwid'],
            'client_count': row['client_count'],
            'clients': [],
        }
        for row in dup_rows
    }

    for row in detail_rows:
        entry = by_hwid.get(row['hwid'])
        if not entry:
            continue
        entry['clients'].append({
            'uuid': row['uuid'],
            'first_access': row['first_access'],
            'last_access': row['last_access'],
            'os': row['os'] or '',
            'model': row['model'] or '',
            'user_agent': row['user_agent'] or '',
            'total_accesses': int(row['total_accesses'] or 0),
        })

    return list(by_hwid.values())


async def count_subscription_access_records() -> int:
    """Количество записей устройств в subscription_access."""
    async with get_db_connection(read_only=True) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM subscription_access")
        row = await cursor.fetchone()
        return int(row[0] if row else 0)


async def delete_subscription_request(uuid: str, user_agent: str) -> int:
    """Удаляет записи запросов подписки по uuid + user_agent.

    Возвращает число удалённых строк. Совпадение по точному user_agent —
    удаляются все записи этого клиента (одинаковый UA) в пределах uuid.
    """
    if not uuid or not user_agent:
        return 0
    async with get_db_connection(read_only=False) as db:
        cursor = await db.execute(
            "DELETE FROM subscription_access WHERE uuid = ? AND user_agent = ?",
            (uuid, user_agent),
        )
        await db.commit()
        deleted = cursor.rowcount or 0
        if deleted:
            logger.info(f"[DEVICES] Удалено запросов подписки: {deleted} (uuid={uuid}, ua={user_agent[:60]})")
        return deleted


async def clear_all_subscription_access() -> int:
    """Полная очистка таблицы subscription_access. Возвращает число удалённых строк."""
    async with get_db_connection(read_only=False) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM subscription_access")
        row = await cursor.fetchone()
        deleted_count = int(row[0] if row else 0)
        await db.execute("DELETE FROM subscription_access")
        await db.commit()
        logger.info(f"[DEVICES] subscription_access очищена, удалено записей: {deleted_count}")
        return deleted_count


