"""
VPN Support Bot — веб-админка.

Минимальный FastAPI-сервис рядом с ботом. Читает ту же SQLite БД
(/app/data/vpn_support.db), что и сам бот. На этом этапе — только
просмотр статистики + аутентификация.

Расширение функционала (редактирование текстов, кнопок, рестарт сервиса)
будет в следующих итерациях.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from admin_web import texts_manager, docker_ctl, buttons_manager, tickets_view, qa_manager, update_manager, broadcast_manager, ai_settings
from app import ai_assistant

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("admin_web")


# ============================================================
#  Конфигурация
# ============================================================

# Логин/пароль администратора веб-панели.
# Берём из .env: WEB_ADMIN_LOGIN / WEB_ADMIN_PASSWORD
WEB_ADMIN_LOGIN = os.getenv("WEB_ADMIN_LOGIN", "admin").strip()
WEB_ADMIN_PASSWORD = os.getenv("WEB_ADMIN_PASSWORD", "").strip()

# Секрет для подписи cookie-сессий. Если не задан — генерируем случайный
# при старте (сессии живут только до рестарта, что нормально).
SESSION_SECRET = os.getenv("WEB_SESSION_SECRET") or secrets.token_urlsafe(32)

# Путь к БД бота — она монтируется через docker-compose volume
DB_PATH = os.getenv(
    "DB_PATH",
    "/app/data/vpn_support.db",
)

# ID супергруппы поддержки — для построения ссылок на топик в Telegram.
# Преобразуется в формат t.me/c/<chat_id_without_-100>/<message_thread_id>
SUPPORT_CHAT_ID_RAW = os.getenv("SUPPORT_CHAT_ID", "").strip()


def topic_url(topic_id: int | None) -> str | None:
    """Строит ссылку на топик в Telegram-чате поддержки."""
    if not topic_id or not SUPPORT_CHAT_ID_RAW:
        return None
    try:
        chat_id = int(SUPPORT_CHAT_ID_RAW)
    except ValueError:
        return None
    # Супергруппы имеют ID вида -100xxxxxxxxxx, в URL — без префикса -100
    if chat_id < 0:
        chat_id_pos = str(chat_id).lstrip("-")
        if chat_id_pos.startswith("100"):
            chat_id_pos = chat_id_pos[3:]
        return f"https://t.me/c/{chat_id_pos}/{topic_id}"
    return None


BASE_DIR = Path(__file__).resolve().parent

# Бренд из .env (для админки и шапки)
BRAND_SHORT = os.getenv("BRAND_SHORT", "VPN Support Admin").strip()
BRAND_NAME = os.getenv("BRAND_NAME", "VPN").strip()


# ============================================================
#  Приложение
# ============================================================

app = FastAPI(
    title=BRAND_SHORT,
    docs_url=None,
    redoc_url=None,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="admin_session",
    max_age=60 * 60 * 12,  # 12 часов
    same_site="lax",
    https_only=False,  # если будет HTTPS — выставить True через env
)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Глобальные переменные для всех шаблонов — теперь {{ brand_short }} доступно везде
templates.env.globals["brand_short"] = BRAND_SHORT
templates.env.globals["brand_name"] = BRAND_NAME


# [v3.5] Фильтр tg_preview — рендерит TG-HTML в безопасный HTML для превью.
# В Telegram разрешены только: <b>, <i>, <u>, <s>, <code>, <pre>,
# <a href="...">, <tg-spoiler>. Всё остальное эскейпим.
import re as _re
import html as _html

_TG_ALLOWED_TAGS = ("b", "i", "u", "s", "code", "pre", "tg-spoiler")
_TG_TAG_RE = _re.compile(
    r"<(/?)(" + "|".join(_TG_ALLOWED_TAGS) + r")(\s[^>]*)?>",
    _re.IGNORECASE,
)
_TG_LINK_RE = _re.compile(
    r'<a\s+href=(["\'])(https?://[^"\'<>\s]+|tg://[^"\'<>\s]+)\1\s*>',
    _re.IGNORECASE,
)
_TG_LINK_CLOSE_RE = _re.compile(r"</a>", _re.IGNORECASE)


def tg_preview_filter(text: str) -> str:
    """Преобразует TG-HTML в безопасный HTML для превью в браузере.

    1) Эскейпит ВСЁ как обычный текст
    2) Возвращает разрешённые теги обратно (b/i/u/s/code/pre/a/tg-spoiler)
    3) Заменяет переносы строк \n → <br>
    4) Подменяет плейсхолдеры {BRAND_NAME}, {MAIN_BOT} и т.п. на читаемые
    """
    if not text:
        return ""
    s = str(text)

    # Подменяем плейсхолдеры до эскейпа (чтобы они были видны как ноль HTML-проблем)
    placeholders = {
        "{BRAND_NAME}": BRAND_NAME,
        "{BRAND_SHORT}": BRAND_SHORT,
        "{MAIN_BOT}": os.getenv("MAIN_BOT") or os.getenv("MAIN_BOT_USERNAME", "").strip(),
        "{COMMUNITY_CHAT_URL}": os.getenv("COMMUNITY_CHAT_URL", ""),
        "{NEWS_CHANNEL_URL}": os.getenv("NEWS_CHANNEL_URL", ""),
    }
    for k, v in placeholders.items():
        s = s.replace(k, v)

    # Эскейпим всё
    s = _html.escape(s, quote=False)

    # Возвращаем разрешённые теги БЕЗ атрибутов (атрибуты у b/i/u/s/code/pre
    # в TG не используются — а у нас в превью могут быть XSS, типа
    # <b onclick="alert(1)">).
    def _restore_tag(m):
        slash = m.group(1) or ""
        tag = m.group(2).lower()
        return f"<{slash}{tag}>"

    # После escape <b> стало &lt;b&gt; — ищем эти паттерны и возвращаем
    pat_escaped = _re.compile(
        r"&lt;(/?)(" + "|".join(_TG_ALLOWED_TAGS) + r")(\s[^&]*?)?&gt;",
        _re.IGNORECASE,
    )
    s = pat_escaped.sub(_restore_tag, s)

    # Ссылки <a href="...">
    pat_link = _re.compile(
        r'&lt;a\s+href=(["\'])(https?://[^"\'<>\s&]+|tg://[^"\'<>\s&]+)\1\s*&gt;',
        _re.IGNORECASE,
    )
    s = pat_link.sub(
        lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">',
        s,
    )
    s = s.replace("&lt;/a&gt;", "</a>").replace("&lt;/A&gt;", "</a>")

    # Переносы строк
    s = s.replace("\n", "<br>")
    return s


templates.env.filters["tg_preview"] = tg_preview_filter


# [v3.5] Фильтр tz_local — конвертирует UTC время в часовой пояс
# из настроек бота. Используется везде где админка показывает
# created_at/last_message_at — чтобы оператор видел свои локальные
# часы, а не UTC.
def tz_local_filter(value, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """
    Принимает UTC datetime / ISO-строку / SQLite-формат (без TZ)
    и возвращает строку в часовом поясе из настроек бота.
    Невалидные значения возвращает как есть (без падения шаблона).
    """
    if value is None or value == "":
        return ""
    try:
        from datetime import datetime, timezone
        # 1) Разбираем в datetime
        if isinstance(value, datetime):
            dt = value
        else:
            s = str(value).strip()
            # SQLite формат "YYYY-MM-DD HH:MM:SS" — приводим к ISO
            s_iso = s.replace(" ", "T").split(".")[0].replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(s_iso)
            except ValueError:
                # Не получилось — возвращаем как есть
                return str(value)
        # 2) Если без TZ — считаем UTC (БД хранит UTC)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # 3) Конвертируем в локальную из настроек
        try:
            from app import bot_settings as _bs
            local_tz = _bs.get_timezone()
        except Exception:
            local_tz = timezone.utc
        dt_local = dt.astimezone(local_tz)
        return dt_local.strftime(fmt)
    except Exception:
        return str(value)


def tz_local_short_filter(value) -> str:
    """Только часы:минуты — для компактных таблиц."""
    return tz_local_filter(value, "%H:%M")


def tz_local_date_filter(value) -> str:
    """Только дата — для группировки."""
    return tz_local_filter(value, "%d.%m.%Y")


templates.env.filters["tz_local"] = tz_local_filter
templates.env.filters["tz_local_short"] = tz_local_short_filter
templates.env.filters["tz_local_date"] = tz_local_date_filter

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ============================================================
#  Helpers
# ============================================================

def _is_authed(request: Request) -> bool:
    return bool(request.session.get("user"))


def _require_auth(request: Request) -> None:
    if not _is_authed(request):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )


# ============================================================
#  Rate-limit для логин-формы
#  Защита от перебора пароля: после 5 неверных попыток с одного IP
#  блокировка на 15 минут. Состояние в памяти процесса — этого хватает,
#  так как админка одна.
# ============================================================

import time as _time
from collections import defaultdict

MAX_LOGIN_ATTEMPTS = 5
LOGIN_BLOCK_SECONDS = 15 * 60  # 15 минут
LOGIN_WINDOW_SECONDS = 10 * 60  # окно подсчёта попыток

# {ip: [(timestamp, success_bool), ...]}
_login_attempts: dict[str, list] = defaultdict(list)


def _client_ip(request: Request) -> str:
    """Получаем реальный IP, учитывая X-Forwarded-For от прокси."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        # Первый IP в цепочке — клиент
        return fwd.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _is_blocked(ip: str) -> tuple[bool, int]:
    """
    Проверяет, заблокирован ли IP. Возвращает (blocked, retry_after_sec).
    """
    now = _time.time()
    attempts = _login_attempts.get(ip, [])
    # Очищаем старые попытки
    attempts = [(ts, ok) for ts, ok in attempts if now - ts < LOGIN_WINDOW_SECONDS]
    _login_attempts[ip] = attempts

    fails = [ts for ts, ok in attempts if not ok]
    if len(fails) >= MAX_LOGIN_ATTEMPTS:
        # Блок отсчитывается от самой последней неудачной попытки
        unblock_at = fails[-1] + LOGIN_BLOCK_SECONDS
        if now < unblock_at:
            return True, int(unblock_at - now)
    return False, 0


def _record_attempt(ip: str, success: bool) -> None:
    _login_attempts[ip].append((_time.time(), success))
    # Подрезаем список, чтоб не рос вечно
    if len(_login_attempts[ip]) > 50:
        _login_attempts[ip] = _login_attempts[ip][-20:]


async def _db():
    """Контекстный менеджер для подключения к БД."""
    return aiosqlite.connect(DB_PATH)


# ============================================================
#  Сбор статистики
# ============================================================

async def collect_stats() -> dict:
    """Собирает все цифры для дашборда одним заходом."""
    stats: dict = {
        "users_total": 0,
        "tickets_total": 0,
        "tickets_open": 0,
        "tickets_closed": 0,
        "banned_total": 0,
        "tickets_today": 0,
        "tickets_yesterday": 0,
        "tickets_week": 0,
        "tickets_per_day": [],     # последние 14 дней
        "recent_tickets": [],      # последние 10 тикетов
        "top_users": [],           # топ-10 активных
        "messages_total": 0,
        "messages_in": 0,
        "messages_out": 0,
        "avg_msgs_per_ticket": 0,
        "close_rate_pct": 0,
        "db_path": DB_PATH,
        "db_exists": os.path.exists(DB_PATH),
        "db_size_kb": 0,
        # [v3.5] Новые поля для переработанного KPI блока
        "queue_5min": 0,            # открытые тикеты > 5 мин
        "worst_sla_minutes": 0,     # самый старый открытый (мин)
        "tickets_by_hour": [0]*24,  # массив [N0..N23] для sparkline
        "web_yesterday": 0,         # веб-обращения за вчера (для дельты)
        "current_hour": datetime.now(tz=timezone.utc).hour,  # подсветка sparkline
    }

    if not stats["db_exists"]:
        return stats

    try:
        stats["db_size_kb"] = round(os.path.getsize(DB_PATH) / 1024, 1)
    except OSError:
        pass

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in await cur.fetchall()}

        if "users" in tables:
            cur = await db.execute("SELECT COUNT(*) FROM users")
            row = await cur.fetchone()
            stats["users_total"] = row[0] if row else 0

            cur = await db.execute("PRAGMA table_info(users)")
            cols = {r[1] for r in await cur.fetchall()}
            if "banned" in cols:
                cur = await db.execute(
                    "SELECT COUNT(*) FROM users WHERE banned=1"
                )
                row = await cur.fetchone()
                stats["banned_total"] = row[0] if row else 0

        if "tickets" in tables:
            cur = await db.execute("SELECT COUNT(*) FROM tickets")
            row = await cur.fetchone()
            stats["tickets_total"] = row[0] if row else 0

            cur = await db.execute(
                "SELECT COUNT(*) FROM tickets WHERE status='open'"
            )
            row = await cur.fetchone()
            stats["tickets_open"] = row[0] if row else 0

            cur = await db.execute(
                "SELECT COUNT(*) FROM tickets WHERE status='closed'"
            )
            row = await cur.fetchone()
            stats["tickets_closed"] = row[0] if row else 0

            # Процент закрытых тикетов
            if stats["tickets_total"] > 0:
                stats["close_rate_pct"] = round(
                    stats["tickets_closed"] / stats["tickets_total"] * 100, 1
                )

            # [v3.5] Границы сегодня/вчера — по часовому поясу из настроек
            try:
                from app import bot_settings as _bs
                today_start = _bs.get_today_start_utc()
            except Exception:
                today_start = datetime.now(tz=timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
            yesterday_start = today_start - timedelta(days=1)

            cur = await db.execute(
                "SELECT COUNT(*) FROM tickets WHERE datetime(created_at) >= datetime(?)",
                (today_start.isoformat(),),
            )
            row = await cur.fetchone()
            stats["tickets_today"] = row[0] if row else 0

            # За вчера — для сравнения с сегодня
            cur = await db.execute(
                """SELECT COUNT(*) FROM tickets
                   WHERE datetime(created_at) >= datetime(?)
                     AND datetime(created_at) < datetime(?)""",
                (yesterday_start.isoformat(), today_start.isoformat()),
            )
            row = await cur.fetchone()
            stats["tickets_yesterday"] = row[0] if row else 0

            week_start = today_start - timedelta(days=6)
            cur = await db.execute(
                "SELECT COUNT(*) FROM tickets WHERE datetime(created_at) >= datetime(?)",
                (week_start.isoformat(),),
            )
            row = await cur.fetchone()
            stats["tickets_week"] = row[0] if row else 0

            # [v3.5] Очередь: открытые тикеты которые висят > 5 минут.
            # Это «требуют немедленного внимания оператора».
            five_min_ago = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
            cur = await db.execute(
                """SELECT COUNT(*) FROM tickets
                   WHERE status = 'open'
                     AND datetime(created_at) < datetime(?)""",
                (five_min_ago.isoformat(),),
            )
            row = await cur.fetchone()
            stats["queue_5min"] = row[0] if row else 0

            # [v3.5] Самый старый открытый TG-тикет — для худшего SLA
            cur = await db.execute(
                """SELECT created_at FROM tickets
                   WHERE status = 'open'
                   ORDER BY created_at ASC LIMIT 1"""
            )
            row = await cur.fetchone()
            stats["worst_sla_minutes"] = 0
            if row and row[0]:
                try:
                    oldest = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
                    if oldest.tzinfo is None:
                        oldest = oldest.replace(tzinfo=timezone.utc)
                    delta_sec = (datetime.now(tz=timezone.utc) - oldest).total_seconds()
                    stats["worst_sla_minutes"] = int(max(0, delta_sec / 60))
                except Exception:
                    pass

            # [v3.5] Распределение тикетов по часам сегодня — для sparkline
            # Возвращаем массив из 24 чисел: индекс = час дня (0..23)
            cur = await db.execute(
                """SELECT CAST(strftime('%H', created_at) AS INTEGER) AS hour,
                          COUNT(*) AS cnt
                   FROM tickets
                   WHERE datetime(created_at) >= datetime(?)
                   GROUP BY hour
                   ORDER BY hour""",
                (today_start.isoformat(),),
            )
            by_hour_tg = {row[0]: row[1] for row in await cur.fetchall()}
            stats["tickets_by_hour"] = [by_hour_tg.get(h, 0) for h in range(24)]

            # Распределение по дням за 14 дней
            days_back = 14
            start_date = today_start - timedelta(days=days_back - 1)
            cur = await db.execute(
                """
                SELECT DATE(created_at) AS d, COUNT(*) AS c
                FROM tickets
                WHERE datetime(created_at) >= datetime(?)
                GROUP BY d
                ORDER BY d
                """,
                (start_date.isoformat(),),
            )
            rows = await cur.fetchall()
            by_day = {r["d"]: r["c"] for r in rows}
            series = []
            for i in range(days_back):
                day = (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
                series.append({
                    "date": day,
                    "label": (start_date + timedelta(days=i)).strftime("%d.%m"),
                    "count": by_day.get(day, 0),
                })
            stats["tickets_per_day"] = series

            # [v3.5] Тикеты по часам сегодня — для sparkline в дашборде.
            # 24 значения (0..23) — TG + веб-обращения.
            cur = await db.execute(
                """
                SELECT CAST(strftime('%H', created_at) AS INTEGER) AS h,
                       COUNT(*) AS c
                FROM tickets
                WHERE datetime(created_at) >= datetime(?)
                GROUP BY h
                """,
                (today_start.isoformat(),),
            )
            by_hour_tg = {r["h"]: r["c"] for r in await cur.fetchall()}

            by_hour_web = {}
            if "web_messages" in tables:
                try:
                    cur = await db.execute(
                        """
                        SELECT CAST(strftime('%H', first_msg) AS INTEGER) AS h,
                               COUNT(*) AS c
                        FROM (
                            SELECT visitor_id, MIN(created_at) AS first_msg
                            FROM web_messages
                            WHERE direction = 'in'
                            GROUP BY visitor_id
                        )
                        WHERE DATE(first_msg) = DATE('now')
                        GROUP BY h
                        """
                    )
                    by_hour_web = {r["h"]: r["c"] for r in await cur.fetchall()}
                except Exception:
                    pass

            tickets_by_hour = []
            for h in range(24):
                tickets_by_hour.append(
                    (by_hour_tg.get(h, 0) or 0) + (by_hour_web.get(h, 0) or 0)
                )
            stats["tickets_by_hour"] = tickets_by_hour
            stats["tickets_by_hour_peak"] = max(tickets_by_hour) if tickets_by_hour else 0
            stats["tickets_by_hour_peak_hour"] = (
                tickets_by_hour.index(max(tickets_by_hour))
                if stats["tickets_by_hour_peak"] > 0 else None
            )

            # [v3.5] Очередь — тикеты, открытые более 5 минут
            five_min_ago = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
            cur = await db.execute(
                """SELECT COUNT(*) FROM tickets
                   WHERE status='open'
                     AND datetime(created_at) < datetime(?)""",
                (five_min_ago.isoformat(),),
            )
            row = await cur.fetchone()
            queue_tg = row[0] if row else 0

            queue_web = 0
            if "web_visitors" in tables:
                try:
                    cur = await db.execute(
                        """SELECT COUNT(*) FROM web_visitors
                           WHERE status='open'
                             AND datetime(last_active_at) < datetime(?)""",
                        (five_min_ago.isoformat(),),
                    )
                    row = await cur.fetchone()
                    queue_web = row[0] if row else 0
                except Exception:
                    pass
            stats["queue_5min"] = queue_tg + queue_web

            # [v3.5] Худший SLA — самый старый открытый тикет
            cur = await db.execute(
                """SELECT MIN(created_at) FROM tickets WHERE status='open'"""
            )
            row = await cur.fetchone()
            oldest_tg = row[0] if row and row[0] else None

            oldest_web = None
            if "web_visitors" in tables:
                try:
                    cur = await db.execute(
                        """SELECT MIN(created_at) FROM web_visitors WHERE status='open'"""
                    )
                    row = await cur.fetchone()
                    oldest_web = row[0] if row and row[0] else None
                except Exception:
                    pass

            worst_minutes = 0
            for ts in (oldest_tg, oldest_web):
                if not ts:
                    continue
                try:
                    # SQLite даёт строку в формате 'YYYY-MM-DD HH:MM:SS'
                    dt = datetime.fromisoformat(ts.replace(" ", "T"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age_minutes = int(
                        (datetime.now(tz=timezone.utc) - dt).total_seconds() / 60
                    )
                    if age_minutes > worst_minutes:
                        worst_minutes = age_minutes
                except Exception:
                    pass
            stats["worst_sla_minutes"] = worst_minutes

            # Последние 10 тикетов TG с username
            cur = await db.execute(
                """
                SELECT t.id, t.user_id, t.topic_id, t.status, t.created_at,
                       u.username
                FROM tickets t
                LEFT JOIN users u ON u.id = t.user_id
                ORDER BY t.id DESC
                LIMIT 15
                """
            )
            tg_recent = []
            for r in await cur.fetchall():
                d = dict(r)
                d["source"] = "tg"
                d["url"] = f"/tickets/{d['id']}"
                tg_recent.append(d)

            # Топ-10 пользователей TG по числу тикетов
            cur = await db.execute(
                """
                SELECT t.user_id,
                       u.username,
                       COUNT(*) AS tickets_count,
                       SUM(CASE WHEN t.status='open' THEN 1 ELSE 0 END) AS open_count,
                       MAX(t.created_at) AS last_ticket_at,
                       MIN(t.created_at) AS first_ticket_at
                FROM tickets t
                LEFT JOIN users u ON u.id = t.user_id
                GROUP BY t.user_id, u.username
                HAVING COUNT(*) > 1
                ORDER BY tickets_count DESC, last_ticket_at DESC
                LIMIT 15
                """
            )
            tg_top = []
            for r in await cur.fetchall():
                d = dict(r)
                d["source"] = "tg"
                tg_top.append(d)

            # === Тоже самое для веб-чатов ===
            web_recent, web_top = [], []
            if "web_visitors" in tables:
                try:
                    cur = await db.execute(
                        """
                        SELECT v.visitor_id, v.user_id, v.user_name, v.user_email,
                               v.topic_id, v.created_at, v.last_active_at
                        FROM web_visitors v
                        ORDER BY v.last_active_at DESC
                        LIMIT 15
                        """
                    )
                    for r in await cur.fetchall():
                        d = dict(r)
                        web_recent.append({
                            "source": "web",
                            "id": d["visitor_id"],
                            "user_id": d["user_id"],
                            "username": d["user_name"],
                            "email": d["user_email"],
                            "topic_id": d["topic_id"],
                            "status": "open" if d["topic_id"] else "closed",
                            "created_at": d["last_active_at"] or d["created_at"],
                            "url": f"/webchats/{d['visitor_id']}",
                        })

                    # Топ веб-клиентов по числу сообщений
                    cur = await db.execute(
                        """
                        SELECT v.visitor_id, v.user_id, v.user_name, v.user_email,
                               COUNT(m.id) AS tickets_count,
                               MAX(v.last_active_at) AS last_ticket_at,
                               MIN(v.created_at) AS first_ticket_at
                        FROM web_visitors v
                        LEFT JOIN web_messages m ON m.visitor_id = v.visitor_id
                        GROUP BY v.visitor_id
                        HAVING COUNT(m.id) > 1
                        ORDER BY tickets_count DESC, last_ticket_at DESC
                        LIMIT 15
                        """
                    )
                    for r in await cur.fetchall():
                        d = dict(r)
                        web_top.append({
                            "source": "web",
                            "user_id": d["user_id"],
                            "username": d["user_name"],
                            "email": d["user_email"],
                            "visitor_id": d["visitor_id"],
                            "tickets_count": d["tickets_count"] or 0,
                            "open_count": 0,  # для совместимости шаблона
                            "last_ticket_at": d["last_ticket_at"],
                            "first_ticket_at": d["first_ticket_at"],
                        })
                except Exception:
                    pass

            # Объединяем TG и Web, сортируем по дате, берём топ-10
            all_recent = tg_recent + web_recent
            all_recent.sort(
                key=lambda x: x.get("created_at") or "", reverse=True,
            )
            stats["recent_tickets"] = all_recent[:10]

            all_top = tg_top + web_top
            all_top.sort(
                key=lambda x: x.get("tickets_count") or 0, reverse=True,
            )
            stats["top_users"] = all_top[:10]

        # Метрики по сообщениям (если таблица есть)
        if "messages" in tables:
            cur = await db.execute("SELECT COUNT(*) FROM messages")
            row = await cur.fetchone()
            stats["messages_total"] = row[0] if row else 0

            cur = await db.execute(
                "SELECT COUNT(*) FROM messages WHERE direction='in'"
            )
            row = await cur.fetchone()
            stats["messages_in"] = row[0] if row else 0

            cur = await db.execute(
                "SELECT COUNT(*) FROM messages WHERE direction='out'"
            )
            row = await cur.fetchone()
            stats["messages_out"] = row[0] if row else 0

            # Среднее число сообщений в тикете (по тем тикетам, что имеют сообщения)
            cur = await db.execute(
                """SELECT AVG(c) FROM
                   (SELECT COUNT(*) AS c FROM messages
                    WHERE ticket_id IS NOT NULL
                    GROUP BY ticket_id)"""
            )
            row = await cur.fetchone()
            avg = row[0] if row and row[0] else 0
            stats["avg_msgs_per_ticket"] = round(avg, 1)

        # === Статистика по веб-чатам (виджет с сайта) ===
        stats["web_visitors_total"] = 0
        stats["web_visitors_anon"] = 0   # [v3.5] анонимные (без tg user_id)
        stats["web_visitors_active_24h"] = 0
        stats["web_messages_total"] = 0
        stats["web_messages_today"] = 0
        stats["web_visitors_online_now"] = 0  # активны за последние 10 минут

        if "web_visitors" in tables:
            cur = await db.execute("SELECT COUNT(*) FROM web_visitors")
            row = await cur.fetchone()
            stats["web_visitors_total"] = row[0] if row else 0

            # [v3.5] Анонимные веб-гости (без привязки к tg user_id) —
            # их добавляем к users_total чтобы показать суммарное число
            # клиентов в KPI карточке «👥 Клиентов».
            # ВАЖНО: считаем ТОЛЬКО тех у кого есть сообщения (= реальные
            # тикеты). Иначе сюда попадают все кто просто открыл сайт.
            if "web_messages" in tables:
                cur = await db.execute(
                    "SELECT COUNT(DISTINCT v.visitor_id) FROM web_visitors v "
                    "INNER JOIN web_messages m ON m.visitor_id = v.visitor_id "
                    "WHERE v.user_id IS NULL"
                )
            else:
                cur = await db.execute(
                    "SELECT COUNT(*) FROM web_visitors WHERE user_id IS NULL"
                )
            row = await cur.fetchone()
            stats["web_visitors_anon"] = row[0] if row else 0

            cur = await db.execute(
                """SELECT COUNT(*) FROM web_visitors
                   WHERE datetime(last_active_at) > datetime('now', '-1 day')"""
            )
            row = await cur.fetchone()
            stats["web_visitors_active_24h"] = row[0] if row else 0

            cur = await db.execute(
                """SELECT COUNT(*) FROM web_visitors
                   WHERE datetime(last_active_at) > datetime('now', '-10 minutes')"""
            )
            row = await cur.fetchone()
            stats["web_visitors_online_now"] = row[0] if row else 0

        # [v3.5] TG онлайн = уникальные клиенты, писавшие в TG за 10 минут.
        # Считаем по таблице messages где direction='in' (входящие от клиента).
        stats["tg_online_now"] = 0
        if "messages" in tables:
            try:
                cur = await db.execute(
                    """SELECT COUNT(DISTINCT user_id) FROM messages
                       WHERE direction = 'in'
                         AND user_id IS NOT NULL
                         AND datetime(created_at) > datetime('now', '-10 minutes')"""
                )
                row = await cur.fetchone()
                stats["tg_online_now"] = row[0] if row else 0
            except Exception:
                pass

        if "web_messages" in tables:
            cur = await db.execute("SELECT COUNT(*) FROM web_messages")
            row = await cur.fetchone()
            stats["web_messages_total"] = row[0] if row else 0

            cur = await db.execute(
                """SELECT COUNT(*) FROM web_messages
                   WHERE DATE(created_at) = DATE('now')"""
            )
            row = await cur.fetchone()
            stats["web_messages_today"] = row[0] if row else 0

        # === Сравнение TG vs Web за разные периоды ===
        # Для каждого периода — отдельные счётчики и серии по дням
        stats["tg_today"] = stats.get("tickets_today", 0)
        stats["tg_week"] = stats.get("tickets_week", 0)
        stats["tg_month"] = 0
        stats["web_today"] = 0
        stats["web_week"] = 0
        stats["web_month"] = 0

        # [v3.5] Границы сегодня — по часовому поясу из настроек
        try:
            from app import bot_settings as _bs
            today_start = _bs.get_today_start_utc()
        except Exception:
            today_start = datetime.now(tz=timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
        week_start = today_start - timedelta(days=6)
        month_start = today_start - timedelta(days=29)

        # TG: за месяц
        if "tickets" in tables:
            cur = await db.execute(
                "SELECT COUNT(*) FROM tickets WHERE datetime(created_at) >= datetime(?)",
                (month_start.isoformat(),),
            )
            row = await cur.fetchone()
            stats["tg_month"] = row[0] if row else 0

        # Веб-чаты: считаем ОБРАЩЕНИЯ (визитёры которые реально написали),
        # а не всех кто просто открыл сайт с виджетом.
        # «Обращение» = визитёр у которого есть входящее сообщение (direction='in').
        # Период — по дате ПЕРВОГО входящего сообщения этого визитёра.
        if "web_visitors" in tables and "web_messages" in tables:
            try:
                # Сколько визитёров впервые написали сегодня
                cur = await db.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT visitor_id, MIN(created_at) AS first_msg
                        FROM web_messages
                        WHERE direction = 'in'
                        GROUP BY visitor_id
                    )
                    WHERE DATE(first_msg) = DATE('now')
                    """
                )
                stats["web_today"] = (await cur.fetchone())[0] or 0

                # [v3.5] За вчера — для дельты в KPI «Сегодня»
                cur = await db.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT visitor_id, MIN(created_at) AS first_msg
                        FROM web_messages
                        WHERE direction = 'in'
                        GROUP BY visitor_id
                    )
                    WHERE DATE(first_msg) = DATE('now', '-1 day')
                    """
                )
                stats["web_yesterday"] = (await cur.fetchone())[0] or 0

                # [v3.5] Веб-обращения по часам сегодня — добавляем к sparkline
                cur = await db.execute(
                    """
                    SELECT CAST(strftime('%H', first_msg) AS INTEGER) AS hour,
                           COUNT(*) AS cnt
                    FROM (
                        SELECT visitor_id, MIN(created_at) AS first_msg
                        FROM web_messages
                        WHERE direction = 'in'
                        GROUP BY visitor_id
                    )
                    WHERE DATE(first_msg) = DATE('now')
                    GROUP BY hour
                    """
                )
                by_hour_web = {row[0]: row[1] for row in await cur.fetchall()}
                if stats.get("tickets_by_hour"):
                    # Суммируем TG + web по каждому часу
                    stats["tickets_by_hour"] = [
                        stats["tickets_by_hour"][h] + by_hour_web.get(h, 0)
                        for h in range(24)
                    ]
                else:
                    stats["tickets_by_hour"] = [by_hour_web.get(h, 0) for h in range(24)]

                cur = await db.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT visitor_id, MIN(created_at) AS first_msg
                        FROM web_messages
                        WHERE direction = 'in'
                        GROUP BY visitor_id
                    )
                    WHERE datetime(first_msg) >= datetime(?)
                    """,
                    (week_start.isoformat(),),
                )
                stats["web_week"] = (await cur.fetchone())[0] or 0

                cur = await db.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT visitor_id, MIN(created_at) AS first_msg
                        FROM web_messages
                        WHERE direction = 'in'
                        GROUP BY visitor_id
                    )
                    WHERE datetime(first_msg) >= datetime(?)
                    """,
                    (month_start.isoformat(),),
                )
                stats["web_month"] = (await cur.fetchone())[0] or 0
            except Exception:
                pass

        # === Графики: 30 дней по двум источникам ===
        days_back = 30
        graph_start = today_start - timedelta(days=days_back - 1)

        # TG по дням
        tg_by_day = {}
        if "tickets" in tables:
            try:
                cur = await db.execute(
                    """SELECT DATE(created_at) AS d, COUNT(*) AS c
                       FROM tickets WHERE datetime(created_at) >= datetime(?)
                       GROUP BY d""",
                    (graph_start.isoformat(),),
                )
                tg_by_day = {r["d"]: r["c"] for r in await cur.fetchall()}
            except Exception:
                pass

        # Web по дням — считаем ОБРАЩЕНИЯ (визитёры с первым входящим
        # сообщением в этот день), а не всех кто открыл сайт.
        web_by_day = {}
        if "web_visitors" in tables and "web_messages" in tables:
            try:
                cur = await db.execute(
                    """
                    SELECT DATE(first_msg) AS d, COUNT(*) AS c
                    FROM (
                        SELECT visitor_id, MIN(created_at) AS first_msg
                        FROM web_messages
                        WHERE direction = 'in'
                        GROUP BY visitor_id
                    )
                    WHERE datetime(first_msg) >= datetime(?)
                    GROUP BY d
                    """,
                    (graph_start.isoformat(),),
                )
                web_by_day = {r["d"]: r["c"] for r in await cur.fetchall()}
            except Exception:
                pass

        tg_series, web_series = [], []
        for i in range(days_back):
            d = graph_start + timedelta(days=i)
            day_key = d.strftime("%Y-%m-%d")
            label = d.strftime("%d.%m")
            tg_series.append({
                "date": day_key, "label": label,
                "count": tg_by_day.get(day_key, 0),
            })
            web_series.append({
                "date": day_key, "label": label,
                "count": web_by_day.get(day_key, 0),
            })
        stats["tg_per_day"] = tg_series
        stats["web_per_day"] = web_series

        # === Агрегация по НЕДЕЛЯМ — 12 недель назад ===
        # Каждая точка = сумма за неделю (понедельник—воскресенье)
        weeks_back = 12
        # Начинаем с понедельника текущей недели и идём назад
        weekday = today_start.weekday()  # 0=пн, 6=вс
        current_week_mon = today_start - timedelta(days=weekday)
        weeks_start = current_week_mon - timedelta(weeks=weeks_back - 1)

        tg_weekly, web_weekly = {}, {}
        try:
            if "tickets" in tables:
                cur = await db.execute(
                    """SELECT created_at FROM tickets
                       WHERE datetime(created_at) >= datetime(?)""",
                    (weeks_start.isoformat(),),
                )
                for r in await cur.fetchall():
                    try:
                        dt = datetime.fromisoformat(
                            str(r["created_at"]).replace("Z", "").split(".")[0]
                        )
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        delta_days = (dt - weeks_start).days
                        if delta_days < 0:
                            continue
                        wk = delta_days // 7
                        if 0 <= wk < weeks_back:
                            tg_weekly[wk] = tg_weekly.get(wk, 0) + 1
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            if "web_visitors" in tables and "web_messages" in tables:
                # Берём дату первого входящего сообщения каждого визитёра
                cur = await db.execute(
                    """
                    SELECT MIN(created_at) AS first_msg
                    FROM web_messages
                    WHERE direction = 'in'
                    GROUP BY visitor_id
                    HAVING datetime(first_msg) >= datetime(?)
                    """,
                    (weeks_start.isoformat(),),
                )
                for r in await cur.fetchall():
                    try:
                        dt = datetime.fromisoformat(
                            str(r["first_msg"]).replace("Z", "").split(".")[0]
                        )
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        delta_days = (dt - weeks_start).days
                        if delta_days < 0:
                            continue
                        wk = delta_days // 7
                        if 0 <= wk < weeks_back:
                            web_weekly[wk] = web_weekly.get(wk, 0) + 1
                    except Exception:
                        pass
        except Exception:
            pass

        tg_week_series, web_week_series = [], []
        for wk in range(weeks_back):
            week_start_d = weeks_start + timedelta(weeks=wk)
            week_end_d = week_start_d + timedelta(days=6)
            # date — ISO-формат начала недели (для сортировки)
            date_key = week_start_d.strftime("%Y-%m-%d")
            # label — компактный диапазон для отображения
            label = (
                f"{week_start_d.strftime('%d.%m')}–"
                f"{week_end_d.strftime('%d.%m')}"
            )
            tg_week_series.append({
                "date": date_key, "label": label,
                "count": tg_weekly.get(wk, 0),
            })
            web_week_series.append({
                "date": date_key, "label": label,
                "count": web_weekly.get(wk, 0),
            })
        stats["tg_per_week"] = tg_week_series
        stats["web_per_week"] = web_week_series

        # === Агрегация по МЕСЯЦАМ — 12 месяцев назад ===
        months_back = 12
        # Первое число текущего месяца
        cur_month_first = today_start.replace(day=1)
        # Первое число месяца, который был months_back-1 месяцев назад
        def _shift_months(d, n):
            y = d.year
            m = d.month + n
            while m <= 0:
                m += 12
                y -= 1
            while m > 12:
                m -= 12
                y += 1
            return d.replace(year=y, month=m, day=1)

        months_start = _shift_months(cur_month_first, -(months_back - 1))

        tg_monthly_raw = {}
        web_monthly_raw = {}
        try:
            if "tickets" in tables:
                cur = await db.execute(
                    """SELECT strftime('%Y-%m', created_at) AS m, COUNT(*) AS c
                       FROM tickets WHERE datetime(created_at) >= datetime(?)
                       GROUP BY m""",
                    (months_start.isoformat(),),
                )
                tg_monthly_raw = {r["m"]: r["c"] for r in await cur.fetchall()}
        except Exception:
            pass

        try:
            if "web_visitors" in tables and "web_messages" in tables:
                cur = await db.execute(
                    """
                    SELECT strftime('%Y-%m', first_msg) AS m, COUNT(*) AS c
                    FROM (
                        SELECT visitor_id, MIN(created_at) AS first_msg
                        FROM web_messages
                        WHERE direction = 'in'
                        GROUP BY visitor_id
                    )
                    WHERE datetime(first_msg) >= datetime(?)
                    GROUP BY m
                    """,
                    (months_start.isoformat(),),
                )
                web_monthly_raw = {r["m"]: r["c"] for r in await cur.fetchall()}
        except Exception:
            pass

        month_names_ru = ["янв", "фев", "мар", "апр", "май", "июн",
                          "июл", "авг", "сен", "окт", "ноя", "дек"]
        tg_month_series, web_month_series = [], []
        for i in range(months_back):
            d = _shift_months(months_start, i)
            month_key = d.strftime("%Y-%m")
            label = f"{month_names_ru[d.month - 1]} {str(d.year)[2:]}"
            # date — ISO-сортируемый ключ (YYYY-MM)
            date_key = month_key
            tg_month_series.append({
                "date": date_key, "label": label,
                "count": tg_monthly_raw.get(month_key, 0),
            })
            web_month_series.append({
                "date": date_key, "label": label,
                "count": web_monthly_raw.get(month_key, 0),
            })
        stats["tg_per_month"] = tg_month_series
        stats["web_per_month"] = web_month_series

    return stats


# ============================================================
#  Статистика по городам (через IP из 3xUIStore)
# ============================================================

async def collect_cities_stats(
    limit_users: int = 80, period: str = "day",
) -> dict:
    """
    Возвращает топ городов по числу обращений,
    разделённый на каналы TG и Сайт.

    period: 'day' / 'week' / 'month'
    Returns: {
        "tg":  [{city, country, cc, count}, ...],
        "web": [{city, country, cc, count}, ...],
    }
    """
    result = {"tg": [], "web": []}

    try:
        from app import admin_panel
        from app import geo_cache
    except Exception as e:
        log.warning("collect_cities_stats: модули не доступны: %s", e)
        return result

    # Инициализируем таблицу кеша если ещё нет
    try:
        await geo_cache.init_geo_cache_table()
    except Exception as e:
        log.warning("init_geo_cache_table failed: %s", e)

    if not os.path.exists(DB_PATH):
        return result

    # Фильтр периода
    if period == "day":
        since_clause = "datetime(created_at) > datetime('now', '-1 day')"
        since_clause_web = "datetime(last_active_at) > datetime('now', '-1 day')"
    elif period == "week":
        since_clause = "datetime(created_at) > datetime('now', '-7 days')"
        since_clause_web = "datetime(last_active_at) > datetime('now', '-7 days')"
    else:  # month
        since_clause = "datetime(created_at) > datetime('now', '-30 days')"
        since_clause_web = "datetime(last_active_at) > datetime('now', '-30 days')"

    # =================================================
    # Шаг 1: собираем TG user_id и веб-визитёры отдельно
    # =================================================
    tg_user_ids: list[int] = []
    web_rows: list[dict] = []  # [{user_id, ip_address}]
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row

            # TG-тикеты — нужны user_id чтобы потом резолвить IP в 3xUIStore
            try:
                cur = await db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='tickets'"
                )
                if await cur.fetchone():
                    cur = await db.execute(
                        f"""SELECT DISTINCT user_id FROM tickets
                           WHERE user_id IS NOT NULL AND {since_clause}
                           ORDER BY id DESC LIMIT ?""",
                        (limit_users,),
                    )
                    tg_user_ids = [r["user_id"] for r in await cur.fetchall()]
            except Exception as e:
                log.debug("tg query failed: %s", e)

            # Веб-визитёры — берём IP из БД (теперь сохраняется в /session)
            try:
                cur = await db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='web_visitors'"
                )
                if await cur.fetchone():
                    cur = await db.execute(
                        f"""SELECT visitor_id, user_id, ip_address
                            FROM web_visitors
                            WHERE {since_clause_web}
                            ORDER BY last_active_at DESC LIMIT ?""",
                        (limit_users,),
                    )
                    web_rows = [dict(r) for r in await cur.fetchall()]
            except Exception as e:
                log.debug("web_visitors query failed: %s", e)
    except Exception as e:
        log.warning("collect_cities_stats: чтение БД упало: %s", e)
        return result

    log.info("collect_cities_stats: TG users=%d, web visitors=%d",
             len(tg_user_ids), len(web_rows))

    # =================================================
    # Шаг 2: для TG резолвим IP через 3xUIStore
    # =================================================
    tg_user_ips: dict[int, str] = {}
    if tg_user_ids:
        try:
            client = admin_panel.get_client()
            await client.start()

            async def _get_ip_for_user(uid: int) -> str | None:
                try:
                    sec = await client.get_security(uid)
                    if sec and isinstance(sec, dict):
                        latest = sec.get("latest_access") or {}
                        ip = latest.get("ip_address")
                        if ip:
                            return str(ip).strip()
                except Exception as e:
                    log.debug("get_security failed for %s: %s", uid, e)
                return None

            batch_size = 8
            for i in range(0, len(tg_user_ids), batch_size):
                batch = tg_user_ids[i:i + batch_size]
                tasks = [_get_ip_for_user(uid) for uid in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for uid, ip in zip(batch, results):
                    if isinstance(ip, str) and ip:
                        tg_user_ips[uid] = ip
                if i + batch_size < len(tg_user_ids):
                    await asyncio.sleep(0.2)
            log.info("collect_cities_stats: TG IPs resolved: %d/%d",
                     len(tg_user_ips), len(tg_user_ids))
        except Exception as e:
            log.warning("collect_cities_stats: TG IP resolve failed: %s", e)

    # =================================================
    # Шаг 3: для веба берём IP из БД (если есть)
    # Для веб-визитёров с user_id, но без ip_address (старые записи)
    # пробуем fallback на 3xUIStore
    # =================================================
    web_ips: list[str] = []  # просто список IP, тк каждый визитёр уникален
    web_user_ids_without_ip: list[int] = []
    for row in web_rows:
        ip = row.get("ip_address")
        if ip:
            web_ips.append(ip)
        elif row.get("user_id"):
            web_user_ids_without_ip.append(row["user_id"])

    # Fallback: для старых веб-визитёров с user_id но без IP — пробуем 3xUIStore
    if web_user_ids_without_ip and len(web_ips) < len(web_rows):
        try:
            client = admin_panel.get_client()
            await client.start()
            for uid in web_user_ids_without_ip[:30]:  # ограничим
                try:
                    sec = await client.get_security(uid)
                    if sec and isinstance(sec, dict):
                        latest = sec.get("latest_access") or {}
                        ip = latest.get("ip_address")
                        if ip:
                            web_ips.append(str(ip).strip())
                except Exception:
                    pass
        except Exception as e:
            log.debug("fallback for web users failed: %s", e)

    log.info("collect_cities_stats: web IPs: %d", len(web_ips))

    # =================================================
    # Шаг 4: резолвим все уникальные IP → город через ip-api
    # =================================================
    all_ips = set(tg_user_ips.values()) | set(web_ips)
    if not all_ips:
        return result

    geo_map = await geo_cache.resolve_many(list(all_ips))

    # =================================================
    # Шаг 5: считаем города отдельно для TG и веб
    # =================================================
    def _aggregate(ips: list[str]) -> list[dict]:
        counts: dict[tuple, int] = {}
        for ip in ips:
            g = geo_map.get(ip) or {"city": "—", "country": "—", "country_code": ""}
            city = g.get("city") or "—"
            country = g.get("country") or "—"
            cc = g.get("country_code") or ""
            if city == "—" and country == "—":
                continue
            key = (city, country, cc)
            counts[key] = counts.get(key, 0) + 1
        items = [
            {"city": k[0], "country": k[1], "cc": k[2], "count": v}
            for k, v in counts.items()
        ]
        items.sort(key=lambda x: x["count"], reverse=True)
        return items[:15]

    result["tg"] = _aggregate(list(tg_user_ips.values()))
    result["web"] = _aggregate(web_ips)

    log.info("collect_cities_stats: TG cities=%d, web cities=%d",
             len(result["tg"]), len(result["web"]))
    return result


# ============================================================
#  Маршруты
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    if _is_authed(request):
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error},
    )


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if not WEB_ADMIN_PASSWORD:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": (
                    "WEB_ADMIN_PASSWORD не задан в .env. "
                    "Заполните его и перезапустите контейнер."
                ),
            },
            status_code=500,
        )

    # Rate-limit: проверяем IP до проверки пароля
    ip = _client_ip(request)
    blocked, retry_after = _is_blocked(ip)
    if blocked:
        minutes = retry_after // 60 + 1
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": (
                    f"Слишком много неудачных попыток. "
                    f"Попробуйте через {minutes} мин."
                ),
            },
            status_code=429,
        )

    # Безопасное сравнение — защита от тайминг-атак
    login_ok = secrets.compare_digest(
        username.strip(), WEB_ADMIN_LOGIN
    )
    pass_ok = secrets.compare_digest(
        password.strip(), WEB_ADMIN_PASSWORD
    )

    if not (login_ok and pass_ok):
        _record_attempt(ip, success=False)
        log.warning("LOGIN_FAIL ip=%s username=%r", ip, username[:30])
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Неверный логин или пароль"},
            status_code=401,
        )

    _record_attempt(ip, success=True)
    log.info("LOGIN_OK ip=%s username=%s", ip, username)
    request.session["user"] = username
    request.session["login_at"] = datetime.now(tz=timezone.utc).isoformat()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    _require_auth(request)

    try:
        stats = await collect_stats()
        error = None
    except Exception as e:
        stats = {"db_exists": os.path.exists(DB_PATH), "db_path": DB_PATH}
        error = f"Ошибка чтения БД: {type(e).__name__}: {e}"
        log.exception("collect_stats failed: %s", e)

    # [v3.5] AI-статистика: токены за сегодня, пользователи, лимит
    try:
        from admin_web import ai_settings as _ai
        ai_stats = {
            "enabled": _ai.is_enabled(),
            "provider": _ai.get_provider(),
            "model": _ai.get_model(),
            "limit_per_user": _ai.get_max_tokens_per_user_day(),
            "tokens_today_total": 0,
            "users_today_count": 0,
            "top_users": [],
            "escalates_today": 0,
        }
        import aiosqlite as _aiosqlite_dash
        async with _aiosqlite_dash.connect(DB_PATH) as db:
            # Общие токены за сегодня
            try:
                cur = await db.execute(
                    "SELECT COALESCE(SUM(tokens), 0), COUNT(DISTINCT user_id) "
                    "FROM ai_user_tokens WHERE date = date('now')",
                )
                row = await cur.fetchone()
                if row:
                    ai_stats["tokens_today_total"] = int(row[0] or 0)
                    ai_stats["users_today_count"] = int(row[1] or 0)
            except Exception:
                pass
            # Топ-5 пользователей по токенам сегодня
            try:
                cur = await db.execute(
                    "SELECT user_id, tokens FROM ai_user_tokens "
                    "WHERE date = date('now') "
                    "ORDER BY tokens DESC LIMIT 5",
                )
                ai_stats["top_users"] = [
                    {"user_id": r[0], "tokens": int(r[1])}
                    for r in await cur.fetchall()
                ]
            except Exception:
                pass
            # Сколько диалогов в истории сегодня (приблизительно: уникальные user_id
            # которые писали AI сегодня)
            try:
                cur = await db.execute(
                    "SELECT COUNT(DISTINCT user_id) FROM ai_history "
                    "WHERE DATE(created_at) = date('now')",
                )
                row = await cur.fetchone()
                if row:
                    ai_stats["dialogs_today"] = int(row[0] or 0)
            except Exception:
                ai_stats["dialogs_today"] = 0

            # [v3.5] Статистика исходов AI за сегодня:
            # resolved — AI ответил сам, escalated — передал оператору
            try:
                # [v3.5] TZ-offset берётся из настроек — даты по локальной зоне
                try:
                    from app import bot_settings as _bs
                    _tz_mod_resolved = _bs.get_sqlite_tz_modifier()
                except Exception:
                    _tz_mod_resolved = "+0 hours"
                cur = await db.execute(
                    f"SELECT outcome, COUNT(*) FROM ai_events "
                    f"WHERE date(created_at, '{_tz_mod_resolved}') = "
                    f"date('now', '{_tz_mod_resolved}') "
                    f"GROUP BY outcome"
                )
                outcomes = {r[0]: int(r[1]) for r in await cur.fetchall()}
                ai_stats["resolved_today"] = outcomes.get("resolved", 0)
                ai_stats["escalated_today"] = outcomes.get("escalated", 0)
                total = ai_stats["resolved_today"] + ai_stats["escalated_today"]
                if total > 0:
                    ai_stats["resolution_rate"] = round(
                        ai_stats["resolved_today"] / total * 100, 1
                    )
                else:
                    ai_stats["resolution_rate"] = 0
            except Exception:
                ai_stats["resolved_today"] = 0
                ai_stats["escalated_today"] = 0
                ai_stats["resolution_rate"] = 0

            # Статистика исходов за 7 дней (для тренда)
            try:
                cur = await db.execute(
                    f"SELECT outcome, COUNT(*) FROM ai_events "
                    f"WHERE date(created_at, '{_tz_mod_resolved}') >= "
                    f"date('now', '{_tz_mod_resolved}', '-6 days') "
                    f"GROUP BY outcome"
                )
                week = {r[0]: int(r[1]) for r in await cur.fetchall()}
                ai_stats["resolved_week"] = week.get("resolved", 0)
                ai_stats["escalated_week"] = week.get("escalated", 0)
                _t = ai_stats["resolved_week"] + ai_stats["escalated_week"]
                ai_stats["resolution_rate_week"] = (
                    round(ai_stats["resolved_week"] / _t * 100, 1) if _t else 0
                )
            except Exception:
                ai_stats["resolved_week"] = 0
                ai_stats["escalated_week"] = 0
                ai_stats["resolution_rate_week"] = 0

            # [v3.5] За вчера — для сравнения с сегодня
            try:
                cur = await db.execute(
                    f"SELECT outcome, COUNT(*) FROM ai_events "
                    f"WHERE date(created_at, '{_tz_mod_resolved}') = "
                    f"date('now', '{_tz_mod_resolved}', '-1 day') "
                    f"GROUP BY outcome"
                )
                yest = {r[0]: int(r[1]) for r in await cur.fetchall()}
                ai_stats["resolved_yesterday"] = yest.get("resolved", 0)
                ai_stats["escalated_yesterday"] = yest.get("escalated", 0)
                _t = ai_stats["resolved_yesterday"] + ai_stats["escalated_yesterday"]
                ai_stats["resolution_rate_yesterday"] = (
                    round(ai_stats["resolved_yesterday"] / _t * 100, 1) if _t else 0
                )
            except Exception:
                ai_stats["resolved_yesterday"] = 0
                ai_stats["escalated_yesterday"] = 0
                ai_stats["resolution_rate_yesterday"] = 0

            # [v3.5] За 30 дней — для общего тренда
            try:
                cur = await db.execute(
                    "SELECT outcome, COUNT(*) FROM ai_events "
                    "WHERE date >= date('now', '-29 days') "
                    "GROUP BY outcome"
                )
                month = {r[0]: int(r[1]) for r in await cur.fetchall()}
                ai_stats["resolved_month"] = month.get("resolved", 0)
                ai_stats["escalated_month"] = month.get("escalated", 0)
                _t = ai_stats["resolved_month"] + ai_stats["escalated_month"]
                ai_stats["resolution_rate_month"] = (
                    round(ai_stats["resolved_month"] / _t * 100, 1) if _t else 0
                )
            except Exception:
                ai_stats["resolved_month"] = 0
                ai_stats["escalated_month"] = 0
                ai_stats["resolution_rate_month"] = 0

            # [v3.5] Разбивка по дням за 30 дней — для графика
            # Учитываем TZ-offset: ai_events.date в UTC, а оператору нужны
            # сутки по своему часовому поясу. Применяем offset к created_at.
            try:
                from app import bot_settings as _bs
                _tz_mod = _bs.get_sqlite_tz_modifier()
            except Exception:
                _tz_mod = "+0 hours"
            try:
                cur = await db.execute(
                    f"""
                    SELECT date(created_at, '{_tz_mod}') AS local_date,
                           outcome, COUNT(*) AS c
                    FROM ai_events
                    WHERE date(created_at, '{_tz_mod}') >= date('now', '{_tz_mod}', '-29 days')
                    GROUP BY local_date, outcome
                    """
                )
                by_day = {}
                for r in await cur.fetchall():
                    d, outc, c = r[0], r[1], int(r[2])
                    by_day.setdefault(d, {"resolved": 0, "escalated": 0})
                    by_day[d][outc] = c
                series_30 = []
                from datetime import date as _date
                # [v3.5] Локальная "сегодня" по таймзоне оператора
                try:
                    today_d = _bs.get_today_start_utc().astimezone(
                        _bs.get_timezone()
                    ).date()
                except Exception:
                    today_d = _date.today()
                for i in range(30):
                    d = today_d - timedelta(days=29 - i)
                    key = d.strftime("%Y-%m-%d")
                    data = by_day.get(key, {"resolved": 0, "escalated": 0})
                    series_30.append({
                        "date": key,
                        "label": d.strftime("%d.%m"),
                        "resolved": data["resolved"],
                        "escalated": data["escalated"],
                    })
                ai_stats["ai_per_day_30"] = series_30
            except Exception as e:
                log.debug("ai_per_day_30 failed: %s", e)
                ai_stats["ai_per_day_30"] = []

            # [v3.5] Почасовая разбивка за сегодня — в локальной таймзоне
            try:
                cur = await db.execute(
                    f"""
                    SELECT CAST(strftime('%H', created_at, '{_tz_mod}') AS INT) AS h,
                           outcome, COUNT(*) AS c
                    FROM ai_events
                    WHERE date(created_at, '{_tz_mod}') = date('now', '{_tz_mod}')
                    GROUP BY h, outcome
                    """
                )
                by_hour_t = {}
                for r in await cur.fetchall():
                    h, outc, c = r[0], r[1], int(r[2])
                    by_hour_t.setdefault(h, {"resolved": 0, "escalated": 0})
                    by_hour_t[h][outc] = c
                hours_today = []
                for h in range(24):
                    data = by_hour_t.get(h, {"resolved": 0, "escalated": 0})
                    hours_today.append({
                        "date": f"{h:02d}:00",
                        "label": f"{h:02d}",
                        "resolved": data["resolved"],
                        "escalated": data["escalated"],
                    })
                ai_stats["ai_per_hour_today"] = hours_today
            except Exception as e:
                log.debug("ai_per_hour_today failed: %s", e)
                ai_stats["ai_per_hour_today"] = []

            # [v3.5] Почасовая разбивка за вчера — в локальной таймзоне
            try:
                cur = await db.execute(
                    f"""
                    SELECT CAST(strftime('%H', created_at, '{_tz_mod}') AS INT) AS h,
                           outcome, COUNT(*) AS c
                    FROM ai_events
                    WHERE date(created_at, '{_tz_mod}') = date('now', '{_tz_mod}', '-1 day')
                    GROUP BY h, outcome
                    """
                )
                by_hour_y = {}
                for r in await cur.fetchall():
                    h, outc, c = r[0], r[1], int(r[2])
                    by_hour_y.setdefault(h, {"resolved": 0, "escalated": 0})
                    by_hour_y[h][outc] = c
                hours_yest = []
                for h in range(24):
                    data = by_hour_y.get(h, {"resolved": 0, "escalated": 0})
                    hours_yest.append({
                        "date": f"{h:02d}:00",
                        "label": f"{h:02d}",
                        "resolved": data["resolved"],
                        "escalated": data["escalated"],
                    })
                ai_stats["ai_per_hour_yesterday"] = hours_yest
            except Exception as e:
                log.debug("ai_per_hour_yesterday failed: %s", e)
                ai_stats["ai_per_hour_yesterday"] = []
        stats["ai"] = ai_stats
    except Exception as e:
        log.warning("dashboard: AI stats failed: %s", e)
        stats["ai"] = {"enabled": False}

    # Города НЕ загружаем здесь — они подгружаются по клику через AJAX
    # (см. /api/cities)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": stats,
            "error": error,
            "user": request.session.get("user"),
            "brand_short": BRAND_SHORT,
        },
    )


@app.get("/api/cities")
async def api_cities(request: Request, period: str = "day"):
    """API для подгрузки городов по клику. period: day/week/month.
    Возвращает: {"ok": true, "tg": [...], "web": [...]}
    """
    _require_auth(request)
    if period not in ("day", "week", "month"):
        period = "day"
    try:
        data = await asyncio.wait_for(
            collect_cities_stats(period=period), timeout=25.0
        )
        return {
            "ok": True,
            "tg": data.get("tg", []),
            "web": data.get("web", []),
            "period": period,
        }
    except asyncio.TimeoutError:
        log.warning("api_cities: timeout для period=%s", period)
        return {"ok": False, "error": "timeout", "tg": [], "web": []}
    except Exception as e:
        log.exception("api_cities: failed: %s", e)
        return {"ok": False, "error": str(e), "tg": [], "web": []}


@app.get("/healthz")
async def healthz():
    """Простой healthcheck — без аутентификации."""
    return {"ok": True, "db_exists": os.path.exists(DB_PATH)}


# ============================================================
#  ТЕКСТЫ
# ============================================================

@app.get("/texts", response_class=HTMLResponse)
async def texts_index(request: Request, saved: str | None = None):
    _require_auth(request)
    try:
        groups = texts_manager.list_texts_grouped()
        total = sum(len(g["entries"]) for g in groups)
        error = None
    except Exception as e:
        groups = []
        total = 0
        error = f"Не удалось прочитать texts.py: {e}"
        log.exception("texts list failed: %s", e)

    return templates.TemplateResponse(
        "texts.html",
        {
            "request": request,
            "groups": groups,
            "total": total,
            "error": error,
            "user": request.session.get("user"),
            "saved_name": saved,
            "protected_texts": list(texts_manager.PROTECTED_TEXTS),
        },
    )


@app.get("/texts/{name}", response_class=HTMLResponse)
async def text_edit(request: Request, name: str):
    _require_auth(request)
    item = texts_manager.get_text(name)
    if not item:
        raise HTTPException(status_code=404, detail="Текст не найден")
    return templates.TemplateResponse(
        "text_edit.html",
        {
            "request": request,
            "item": item,
            "user": request.session.get("user"),
            "error": None,
            "ok": None,
        },
    )


@app.post("/texts/{name}", response_class=HTMLResponse)
async def text_save(
    request: Request,
    name: str,
    value: str = Form(...),
):
    _require_auth(request)
    item = texts_manager.get_text(name)
    if not item:
        raise HTTPException(status_code=404, detail="Текст не найден")

    success, message = texts_manager.update_text(name, value)
    log.info("TEXT_EDIT %s: %s — %s", name, success, message)

    # Перечитываем актуальное значение из файла (то что записалось)
    fresh = texts_manager.get_text(name) or {"name": name, "value": value}

    if success:
        return RedirectResponse(f"/texts?saved={name}", status_code=303)

    return templates.TemplateResponse(
        "text_edit.html",
        {
            "request": request,
            "item": fresh,
            "user": request.session.get("user"),
            "error": message,
            "ok": None,
        },
        status_code=400,
    )


@app.post("/texts/{name}/delete", response_class=HTMLResponse)
async def text_delete(request: Request, name: str):
    """Удаление текста из БД."""
    _require_auth(request)
    success, message = texts_manager.delete_text(name)
    log.info("TEXT_DELETE %s: %s — %s", name, success, message)
    if success:
        return RedirectResponse("/texts?deleted=1", status_code=303)
    # Ошибка → возвращаем на список с alert
    try:
        groups = texts_manager.list_texts_grouped()
    except Exception:
        groups = []
    return templates.TemplateResponse(
        "texts.html",
        {
            "request": request,
            "groups": groups,
            "user": request.session.get("user"),
            "error": message,
            "saved_name": None,
        },
        status_code=400,
    )


@app.post("/texts", response_class=HTMLResponse)
async def text_create(
    request: Request,
    name: str = Form(...),
    value: str = Form(""),
):
    """Создание нового текста (через форму на /texts)."""
    _require_auth(request)
    success, message = texts_manager.create_text(name, value)
    log.info("TEXT_CREATE %s: %s — %s", name, success, message)
    if success:
        return RedirectResponse(
            f"/texts/{name.strip().upper()}?created=1", status_code=303,
        )
    try:
        groups = texts_manager.list_texts_grouped()
    except Exception:
        groups = []
    return templates.TemplateResponse(
        "texts.html",
        {
            "request": request,
            "groups": groups,
            "user": request.session.get("user"),
            "error": message,
            "saved_name": None,
        },
        status_code=400,
    )


# ============================================================
#  КНОПКИ (редактор клиентских клавиатур)
# ============================================================

@app.get("/buttons", response_class=HTMLResponse)
async def buttons_index(request: Request, saved: str | None = None):
    _require_auth(request)
    try:
        # [v3.5] Показываем только корневые меню (на которые никто не ссылается).
        # Подменю видны через навигацию внутри родительского меню.
        roots = buttons_manager.list_root_menus()
        all_menus = buttons_manager.list_menus()
        # Подменю — для информации, если оператор хочет их найти напрямую
        root_names = {m["name"] for m in roots}
        submenus = [m for m in all_menus if m["name"] not in root_names]
        error = None
    except Exception as e:
        roots, submenus = [], []
        error = f"Не удалось прочитать keyboards.py: {e}"
        log.exception("buttons list failed: %s", e)

    return templates.TemplateResponse(
        "buttons.html",
        {
            "request": request,
            "menus": roots,
            "submenus": submenus,
            "user": request.session.get("user"),
            "error": error,
            "saved_menu": saved,
            "protected_menus": list(buttons_manager.PROTECTED_MENUS),
            "unremovable_menus": list(buttons_manager.UNREMOVABLE_MENUS),
        },
    )


# ============================================================
#  [v3.5] НАСТРОЙКИ ПОВЕДЕНИЯ БОТА
# ============================================================

@app.get("/bot-settings", response_class=HTMLResponse)
async def bot_settings_view(
    request: Request,
    saved: str | None = None,
    error: str | None = None,
):
    """Страница настроек бота: тумблеры и числовые поля сгруппированы."""
    _require_auth(request)
    from app import bot_settings as _bs
    # [v3.5] Убеждаемся что таблица существует (бот мог не запуститься
    # после миграции, а админка должна работать независимо).
    try:
        await _bs.init_settings_db()
    except Exception as e:
        log.warning("init bot_settings table failed: %s", e)
    # Перечитываем кеш чтобы видеть актуальные значения сразу
    try:
        await _bs.reload_cache()
    except Exception as e:
        log.warning("reload bot_settings cache failed: %s", e)

    data = _bs.get_all()
    return templates.TemplateResponse(
        "bot_settings.html",
        {
            "request": request,
            "groups": data["groups"],
            "user": request.session.get("user"),
            "saved": saved,
            "error": error,
        },
    )


@app.post("/bot-settings/{key}", response_class=HTMLResponse)
async def bot_settings_update(request: Request, key: str, value: str = Form("")):
    """POST: сохранить значение настройки. Принимает форму с полем `value`."""
    _require_auth(request)
    from app import bot_settings as _bs

    schema = _bs.SETTINGS_SCHEMA.get(key)
    if not schema:
        return RedirectResponse(
            "/bot-settings?error=Неизвестная настройка", status_code=303,
        )

    type_, default, group, label, _desc = schema

    # Нормализуем значение по типу
    if type_ == "bool":
        normalized = "true" if value in ("true", "1", "on", "yes") else "false"
    elif type_ == "int":
        try:
            normalized = str(int(value))
        except (ValueError, TypeError):
            return RedirectResponse(
                f"/bot-settings?error=Введите число для «{label}»",
                status_code=303,
            )
    elif type_ == "select":
        # [v3.5] Значение должно быть в списке допустимых options
        allowed = {v for v, _l in _bs.SETTINGS_OPTIONS.get(key, [])}
        if allowed and value not in allowed:
            return RedirectResponse(
                f"/bot-settings?error=Недопустимое значение для «{label}»",
                status_code=303,
            )
        normalized = value
    else:
        normalized = value

    await _bs.set_value(key, normalized)
    return RedirectResponse(
        f"/bot-settings?saved={key}", status_code=303,
    )


@app.post("/bot-settings/{key}/reset", response_class=HTMLResponse)
async def bot_settings_reset(request: Request, key: str):
    """Сбросить настройку к дефолту."""
    _require_auth(request)
    from app import bot_settings as _bs
    if key not in _bs.SETTINGS_SCHEMA:
        return RedirectResponse("/bot-settings", status_code=303)
    await _bs.reset_to_default(key)
    return RedirectResponse(
        f"/bot-settings?saved={key}_reset", status_code=303,
    )





@app.get("/buttons/{menu}", response_class=HTMLResponse)
async def buttons_menu(request: Request, menu: str, saved: str | None = None):
    _require_auth(request)
    try:
        m = buttons_manager.get_menu(menu)
    except Exception as e:
        log.exception("buttons_menu read failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    if m is None:
        raise HTTPException(status_code=404, detail="Меню не найдено")

    # Для автоподсказки имени новой кнопки — список занятых callback-имён
    used_callbacks = buttons_manager.list_used_callbacks()
    suggested_callback = buttons_manager.suggest_next_callback(used_callbacks)

    # [v3.5] Текст меню — у КАЖДОГО меню должен быть редактор.
    # Если меню есть в MENU_TO_TEXT (статические main_menu/pay_menu/...) —
    # используем «родной» ключ. Иначе авто-ключ MENU_<UPPER_NAME> для
    # динамических меню типа mykey_menu, mykey_reset_confirm_keyboard.
    menu_text_key = buttons_manager.MENU_TO_TEXT.get(menu)
    menu_text_is_auto = False
    if not menu_text_key:
        import re as _re
        sanitized = _re.sub(r"[^A-Za-z0-9_]", "_", menu).upper()
        menu_text_key = f"MENU_{sanitized}"
        menu_text_is_auto = True

    menu_text_value = ""
    item = texts_manager.get_text(menu_text_key)
    if item:
        menu_text_value = item.get("value", "")

    # [v3.5] Если в БД пусто И это динамическое меню — подставляем дефолтный
    # шаблон с плейсхолдерами, чтобы админ сразу видел структуру.
    # Список доступных переменных для каждого меню — показывается в UI.
    menu_text_variables = []
    # Меню для которых есть дефолтный шаблон в коде (показываем кнопку «Сброс»)
    menus_with_default = {"mykey_menu", "mykey_reset_confirm_keyboard", "mykey_back_keyboard"}
    menu_text_has_default = menu in menus_with_default

    if not menu_text_value and menu_text_is_auto:
        if menu == "mykey_menu":
            menu_text_value = (
                "🔑 <b>Мой ключ</b>\n\n"
                "• Статус: {status}\n"
                "• Действует до: <b>{end_date}</b>\n"
                "• Зарегистрированы: {registered}\n"
                "• Лимит устройств: <b>{limit_ip}</b>\n"
                "• Username Telegram: @{username}\n"
                "• Email: <code>{email}</code>\n"
                "• Баланс партнёра: <b>{partner_balance} ₽</b>\n"
                "• Трафик за всё время: <b>{traffic_lifetime}</b>\n\n"
                "🔑 <b>Ваш ключ подключения</b>\n"
                "<i>Нажмите, чтобы скопировать. Не передавайте никому.</i>\n"
                "<blockquote>{sub_link}</blockquote>"
            )
        elif menu == "mykey_reset_confirm_keyboard":
            menu_text_value = (
                "🔁 <b>Сброс подписки — что это?</b>\n\n"
                "Сброс — это перевыпуск ссылок и конфигов вашей подписки. "
                "Используется если ваши конфиги попали к третьим лицам "
                "(через бот {MAIN_BOT} или через страницу подключения).\n\n"
                "После сброса:\n"
                "• Старые конфиги перестанут работать\n"
                "• Нужно будет переподключить все устройства заново\n\n"
                "Если согласны — нажмите «Запросить сброс»."
            )
        elif menu == "mykey_back_keyboard":
            menu_text_value = (
                "Возврат к главному меню. Используйте кнопки ниже."
            )

    # [v3.5] Список меню для которых код бота УЖЕ читает шаблон из БД —
    # для них warning о «динамическом меню» не показываем (всё работает).
    menus_with_bot_template_support = {"mykey_menu"}
    if menu in menus_with_bot_template_support:
        menu_text_is_auto = False  # бот реально использует — скрываем warning

    # Список переменных доступных для текущего меню (для подсказки в UI)
    if menu == "mykey_menu":
        menu_text_variables = [
            ("{status}", "🟢 активна (ещё 30 дн.) / 🔴 истекла 5 дн. назад"),
            ("{end_date}", "Дата окончания подписки, например «15.07.2026 23:59»"),
            ("{days_left}", "Сколько дней осталось (число)"),
            ("{registered}", "Дата регистрации клиента"),
            ("{limit_ip}", "Лимит устройств (число или ∞)"),
            ("{username}", "TG username клиента"),
            ("{email}", "Email клиента"),
            ("{partner_balance}", "Баланс партнёра в рублях"),
            ("{traffic_lifetime}", "Трафик за всё время (например 12.34 GB)"),
            ("{sub_link}", "Ссылка для подключения к VPN"),
        ]
    elif menu == "mykey_reset_confirm_keyboard":
        menu_text_variables = [
            ("{BRAND_NAME}", "Название бренда из .env"),
            ("{MAIN_BOT}", "@username основного бота из .env"),
        ]

    # [v3.5] Путь от корня до текущего меню — для breadcrumb
    breadcrumb = buttons_manager.find_breadcrumb(menu)
    parents = buttons_manager.find_parents(menu)

    return templates.TemplateResponse(
        "buttons_menu.html",
        {
            "request": request,
            "menu": m,
            "user": request.session.get("user"),
            "error": None,
            "saved_index": saved,
            "used_callbacks": used_callbacks,
            "suggested_callback": suggested_callback,
            "menu_text_key": menu_text_key,
            "menu_text_value": menu_text_value,
            "menu_text_is_auto": menu_text_is_auto,
            "menu_text_has_default": menu_text_has_default,
            "menu_text_variables": menu_text_variables,
            "protected_menus": list(buttons_manager.PROTECTED_MENUS),
            "unremovable_menus": list(buttons_manager.UNREMOVABLE_MENUS),
            "breadcrumb": breadcrumb,
            "parents": parents,
        },
    )


@app.get("/buttons/{menu}/{index}", response_class=HTMLResponse)
async def button_edit(request: Request, menu: str, index: int):
    _require_auth(request)
    m = buttons_manager.get_menu(menu)
    if m is None:
        raise HTTPException(status_code=404, detail="Меню не найдено")
    if index < 0 or index >= len(m["flat"]):
        raise HTTPException(status_code=404, detail="Кнопка не найдена")

    btn = m["flat"][index]

    # [v3.5] У каждой callback-кнопки должен быть редактируемый текст.
    # Логика ключа:
    #   1. Если callback есть в CALLBACK_TO_TEXT → используем «родной» ключ
    #      (например m_pay → PAY_MENU, бот точно читает этот ключ)
    #   2. Иначе → авто-ключ BTN_<UPPER_CALLBACK> — текст сохранится в БД.
    #      Для виртуальных/динамических кнопок (mk_reset, mykey_open и т.д.)
    #      бот может подгружать этот текст в будущем по конвенции имени.
    faq_text_key = None
    faq_text_value = ""
    faq_is_auto = False  # True = ключ авто-сгенерирован, не привязан в коде
    if btn.get("kind") == "callback":
        cb_value = btn.get("value", "")
        faq_text_key = buttons_manager.CALLBACK_TO_TEXT.get(cb_value)
        if not faq_text_key and cb_value:
            # Авто-ключ для динамических кнопок без явной привязки.
            # Sanitize: только [A-Z0-9_], верхний регистр.
            import re as _re
            sanitized = _re.sub(r"[^A-Za-z0-9_]", "_", cb_value).upper()
            faq_text_key = f"BTN_{sanitized}"
            faq_is_auto = True
        if faq_text_key:
            item = texts_manager.get_text(faq_text_key)
            if item:
                faq_text_value = item.get("value", "")

    if not btn.get("editable", True):
        return templates.TemplateResponse(
            "button_edit.html",
            {
                "request": request, "menu": m, "btn": btn,
                "user": request.session.get("user"),
                "error": (
                    "Эта кнопка ссылается на URL из переменной .env "
                    f"(<code>{btn['value']}</code>). Чтобы изменить — "
                    "правьте .env, не код."
                ),
                "readonly": True,
                "faq_text_key": faq_text_key,
                "faq_text_value": faq_text_value,
                "faq_is_auto": faq_is_auto,
            },
        )

    return templates.TemplateResponse(
        "button_edit.html",
        {
            "request": request, "menu": m, "btn": btn,
            "user": request.session.get("user"),
            "error": None, "readonly": False,
            "faq_text_key": faq_text_key,
            "faq_text_value": faq_text_value,
            "faq_is_auto": faq_is_auto,
        },
    )


# ============================================================
#  МЕНЮ — создание/удаление целых меню и редактирование их текстов
#
#  ВАЖНО: эти роуты ОБЯЗАНЫ объявляться ПЕРЕД общим
#  POST /buttons/{menu}/{index}, иначе FastAPI будет пытаться
#  распарсить `_delete_menu` / `_save_text` как int index
#  и валиться с 422 (Input should be a valid integer).
# ============================================================

@app.post("/buttons/_create_menu", response_class=HTMLResponse)
async def menu_create(
    request: Request,
    name: str = Form(...),
):
    """Создаёт новое (пользовательское) меню."""
    _require_auth(request)
    success, message = buttons_manager.create_menu(name)
    log.info("MENU_CREATE %s: ok=%s msg=%s", name, success, message)

    if success:
        return RedirectResponse(f"/buttons/{name}?created=1", status_code=303)

    return RedirectResponse(
        f"/buttons?error={message}", status_code=303,
    )


@app.post("/buttons/{menu}/_delete_menu", response_class=HTMLResponse)
async def menu_delete(request: Request, menu: str):
    """Удаляет целое меню. Жёсткая защита для UNREMOVABLE_MENUS."""
    _require_auth(request)

    # [v3.5] Жёсткая защита — некоторые меню удалить нельзя ВООБЩЕ.
    # Даже если кто-то отправит POST вручную, минуя UI.
    if menu in buttons_manager.UNREMOVABLE_MENUS:
        log.warning("MENU_DELETE BLOCKED: %s is in UNREMOVABLE_MENUS", menu)
        return RedirectResponse(
            f"/buttons/{menu}?error=Это меню удалить нельзя (системное)",
            status_code=303,
        )

    success, message = buttons_manager.delete_menu(menu)
    log.info("MENU_DELETE %s: ok=%s msg=%s", menu, success, message)

    if success:
        return RedirectResponse("/buttons?deleted_menu=1", status_code=303)

    return RedirectResponse(
        f"/buttons/{menu}?error={message}", status_code=303,
    )


@app.post("/buttons/{menu}/_save_text", response_class=HTMLResponse)
async def menu_text_save(
    request: Request,
    menu: str,
    value: str = Form(...),
):
    """
    Сохраняет текст меню (приветствие / описание раздела).
    [v3.5] Если меню не в MENU_TO_TEXT — используем авто-ключ
    MENU_<UPPER_NAME>, чтобы и для динамических меню текст сохранялся.
    """
    _require_auth(request)
    text_key = buttons_manager.MENU_TO_TEXT.get(menu)
    if not text_key:
        # Авто-ключ для динамических меню (mykey_menu и т.п.)
        import re as _re
        sanitized = _re.sub(r"[^A-Za-z0-9_]", "_", menu).upper()
        text_key = f"MENU_{sanitized}"

    success, message = texts_manager.update_text(text_key, value)
    log.info("MENU_TEXT_SAVE %s (key=%s): ok=%s msg=%s",
             menu, text_key, success, message)

    if success:
        return RedirectResponse(
            f"/buttons/{menu}?text_saved=1", status_code=303,
        )

    return RedirectResponse(
        f"/buttons/{menu}?error={message}", status_code=303,
    )


@app.post("/buttons/{menu}/_reset_text", response_class=HTMLResponse)
async def menu_text_reset(request: Request, menu: str):
    """
    [v3.5] Сбрасывает текст меню к дефолтному шаблону из кода.
    Удаляет запись из БД — при следующем открытии страницы редактор
    подставит дефолт из код-шаблона (DEFAULT_DYNAMIC_MENU_TEMPLATES).

    Используется когда админ удалил весь текст по ошибке и хочет вернуть
    оригинальный шаблон с переменными.
    """
    _require_auth(request)
    text_key = buttons_manager.MENU_TO_TEXT.get(menu)
    if not text_key:
        import re as _re
        sanitized = _re.sub(r"[^A-Za-z0-9_]", "_", menu).upper()
        text_key = f"MENU_{sanitized}"

    # Удаляем запись из БД — при reload бот пересоберёт fallback
    success, message = texts_manager.delete_text(text_key)
    log.info("MENU_TEXT_RESET %s (key=%s): ok=%s msg=%s",
             menu, text_key, success, message)

    # Считаем успехом даже "ключа нет в БД" — это значит дефолт и так используется
    if success or "нет в БД" in (message or ""):
        return RedirectResponse(
            f"/buttons/{menu}?text_reset=1", status_code=303,
        )
    return RedirectResponse(
        f"/buttons/{menu}?error={message}", status_code=303,
    )


@app.post("/buttons/{menu}/{index}/_toggle_hidden", response_class=HTMLResponse)
async def button_toggle_hidden(
    request: Request,
    menu: str,
    index: int,
    hidden: str = Form("0"),
):
    """
    [v3.5] Скрыть/показать кнопку (не удаляя).
    hidden = "1" → скрыть, "0" → показать.

    [v3.5] Поддерживает виртуальные меню (mykey_menu и пр.) — для них
    используется отдельная таблица virtual_button_hidden, потому что
    самих кнопок в content_buttons нет (генерятся в коде бота).
    """
    _require_auth(request)
    want_hide = hidden == "1"
    from app import content_db as _cdb
    from admin_web import buttons_manager

    # [v3.5] Виртуальные меню обрабатываются отдельно
    if menu in buttons_manager._VIRTUAL_MENUS:
        tpl = buttons_manager._VIRTUAL_MENUS[menu]
        if index < 0 or index >= len(tpl["buttons"]):
            return RedirectResponse(
                f"/buttons/{menu}?error=Кнопка не найдена", status_code=303,
            )
        button_value = tpl["buttons"][index]["value"]
        await _cdb.set_virtual_button_hidden(menu, button_value, want_hide)
        log.info("VIRTUAL_BUTTON_TOGGLE menu=%s value=%s hidden=%s",
                 menu, button_value, want_hide)
        ok = True
    else:
        ok = await _cdb.set_button_hidden(menu, index, want_hide)
        log.info("BUTTON_TOGGLE_HIDDEN menu=%s pos=%d hidden=%s ok=%s",
                 menu, index, want_hide, ok)

    # Сигнал боту перечитать кеш
    try:
        from admin_web import texts_manager
        texts_manager._touch_signal()
    except Exception:
        pass
    if not ok:
        return RedirectResponse(
            f"/buttons/{menu}?error=Кнопка не найдена", status_code=303,
        )
    action = "скрыта" if want_hide else "показана"
    return RedirectResponse(
        f"/buttons/{menu}?message=Кнопка {action}", status_code=303,
    )


@app.post("/buttons/{menu}/{index}/_save_faq_text", response_class=HTMLResponse)
async def button_faq_text_save(
    request: Request,
    menu: str,
    index: int,
    value: str = Form(...),
):
    """
    Сохраняет FAQ-текст, который кнопка отправляет при нажатии клиентом.
    [v3.5] Если callback нет в CALLBACK_TO_TEXT — создаём авто-ключ
    BTN_<UPPER_CALLBACK>, чтобы текст можно было редактировать для
    динамических кнопок.
    """
    _require_auth(request)
    m = buttons_manager.get_menu(menu)
    if m is None or index < 0 or index >= len(m["flat"]):
        raise HTTPException(status_code=404, detail="Кнопка не найдена")

    btn = m["flat"][index]
    callback_val = btn.get("value", "") if btn.get("kind") == "callback" else ""
    if not callback_val:
        raise HTTPException(
            status_code=400,
            detail="Только для callback-кнопок можно задать FAQ-текст",
        )

    # Сначала пробуем родной ключ
    text_key = buttons_manager.CALLBACK_TO_TEXT.get(callback_val)
    if not text_key:
        # Авто-ключ для динамических кнопок
        import re as _re
        sanitized = _re.sub(r"[^A-Za-z0-9_]", "_", callback_val).upper()
        text_key = f"BTN_{sanitized}"

    success, message = texts_manager.update_text(text_key, value)
    log.info("BUTTON_FAQ_TEXT_SAVE %s[%d] (key=%s): ok=%s msg=%s",
             menu, index, text_key, success, message)

    if success:
        return RedirectResponse(
            f"/buttons/{menu}/{index}?faq_saved=1", status_code=303,
        )

    return RedirectResponse(
        f"/buttons/{menu}/{index}?error={message}", status_code=303,
    )


# [v3.5] КРИТИЧНО: маршрут добавления кнопки ОБЯЗАН быть зарегистрирован
# ВЫШЕ /buttons/{menu}/{index} — иначе FastAPI пытается распарсить '_add'
# как int и возвращает 422 (баг "int_parsing"). Префикс «_» делает имя
# гарантированно не-числовым.
@app.post("/buttons/{menu}/_add", response_class=HTMLResponse)
async def button_add(
    request: Request, menu: str,
    text: str = Form(...),
    action_type: str = Form("text"),         # 'text' | 'submenu' | 'url'
    response_text: str = Form(""),           # для action_type='text'
    submenu_name: str = Form(""),            # для action_type='submenu'
    url_value: str = Form(""),               # для action_type='url'
):
    """[v3.5] Универсальное добавление кнопки трёх типов:
    - text: callback с автогенерированным cb + сохранение текста в response_text
    - submenu: создание нового меню + кнопка-переход в текущем
    - url: обычная URL-кнопка

    После создания — всегда редирект в edit page новой кнопки (для text/url)
    или в само подменю (для submenu). Логика единая: всегда можно сразу
    доработать только что созданную кнопку.
    """
    _require_auth(request)
    text = (text or "").strip()
    action_type = (action_type or "text").strip().lower()

    if not text:
        return RedirectResponse(
            f"/buttons/{menu}?error=Надпись на кнопке не может быть пустой",
            status_code=303,
        )

    if action_type not in ("text", "submenu", "url"):
        return RedirectResponse(
            f"/buttons/{menu}?error=Неизвестный тип кнопки",
            status_code=303,
        )

    # [v3.5] Защита: нельзя добавлять кнопки в виртуальные меню (mykey_menu)
    m_check = buttons_manager.get_menu(menu)
    if m_check and m_check.get("is_virtual"):
        return RedirectResponse(
            f"/buttons/{menu}?error=Это динамическое меню — добавление кнопок невозможно",
            status_code=303,
        )

    import re as _re
    import secrets as _secrets

    def _gen_dyn_cb(prefix: str = "dyn_") -> str:
        """Генерирует уникальный callback_data (≤64 символа)."""
        used = set(buttons_manager.list_used_callbacks())
        for _ in range(20):
            cb = prefix + _secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8].lower()
            if cb not in used:
                return cb
        return prefix + _secrets.token_urlsafe(10)[:12].lower()

    def _slugify(s: str, fallback: str) -> str:
        """Имя меню из надписи: только [a-z0-9_], максимум 28 символов."""
        s = (s or "").strip().lower()
        s = _re.sub(r"[^a-z0-9_]+", "_", s).strip("_")
        if not s or not s[0].isalpha():
            s = fallback + "_" + s if s else fallback
        return s[:28] or fallback

    # Узнаём индекс новой кнопки (для редиректа в edit-page)
    m = buttons_manager.get_menu(menu)
    if m is None:
        return RedirectResponse(
            f"/buttons?error=Меню {menu} не найдено",
            status_code=303,
        )
    new_index = len(m["flat"])

    # === 1) URL-кнопка ===
    if action_type == "url":
        url_value = (url_value or "").strip()
        if not url_value:
            return RedirectResponse(
                f"/buttons/{menu}?error=URL не может быть пустым",
                status_code=303,
            )
        success, message = buttons_manager.add_button(
            menu, text, "url", url_value,
        )
        log.info("BUTTON_ADD url %s text=%r idx=%d: ok=%s",
                 menu, text, new_index, success)
        if success:
            # Редирект в edit page — можно поправить URL/надпись если что
            return RedirectResponse(
                f"/buttons/{menu}/{new_index}?created=1",
                status_code=303,
            )
        return RedirectResponse(
            f"/buttons/{menu}?error={message}", status_code=303,
        )

    # === 2) Кнопка с текстом ответа ===
    if action_type == "text":
        if not response_text.strip():
            return RedirectResponse(
                f"/buttons/{menu}?error=Текст ответа не может быть пустым",
                status_code=303,
            )
        cb = _gen_dyn_cb("dyn_")
        success, message = buttons_manager.add_button(
            menu, text, "callback", cb,
            response_text=response_text.strip(),
        )
        log.info(
            "BUTTON_ADD text %s text=%r cb=%s idx=%d len=%d: ok=%s",
            menu, text, cb, new_index, len(response_text), success,
        )
        if success:
            # Редирект в edit page — можно доработать текст ответа
            return RedirectResponse(
                f"/buttons/{menu}/{new_index}?created=1",
                status_code=303,
            )
        return RedirectResponse(
            f"/buttons/{menu}?error={message}", status_code=303,
        )

    # === 3) Кнопка-подменю ===
    desired_name = (submenu_name or "").strip().lower()
    if not desired_name:
        desired_name = _slugify(text, fallback="submenu")
    existing_menus = {m["name"] for m in buttons_manager.list_menus()}
    if desired_name in existing_menus:
        for i in range(2, 50):
            candidate = f"{desired_name}_{i}"
            if candidate not in existing_menus and len(candidate) <= 30:
                desired_name = candidate
                break
        else:
            return RedirectResponse(
                f"/buttons/{menu}?error=Не смог придумать уникальное имя подменю",
                status_code=303,
            )

    # Создаём пустое меню
    ok_menu, msg_menu = buttons_manager.create_menu(desired_name, title=text)
    if not ok_menu:
        return RedirectResponse(
            f"/buttons/{menu}?error=Не удалось создать подменю: {msg_menu}",
            status_code=303,
        )

    # Добавляем кнопку в текущее меню — submenu_name указывает на новое
    cb = _gen_dyn_cb("dyn_")
    ok_btn, msg_btn = buttons_manager.add_button(
        menu, text, "callback", cb,
        submenu_name=desired_name,
    )
    log.info(
        "BUTTON_ADD submenu %s → %s text=%r cb=%s idx=%d: ok=%s",
        menu, desired_name, text, cb, new_index, ok_btn,
    )
    if ok_btn:
        # Редирект сразу в новое подменю чтобы наполнить кнопками
        return RedirectResponse(
            f"/buttons/{desired_name}?created=1", status_code=303,
        )
    return RedirectResponse(
        f"/buttons/{menu}?error={msg_btn}", status_code=303,
    )


@app.post("/buttons/{menu}/{index}", response_class=HTMLResponse)
async def button_save(
    request: Request, menu: str, index: int,
    text: str = Form(...),
    kind: str = Form(...),
    value: str = Form(...),
):
    _require_auth(request)
    m = buttons_manager.get_menu(menu)
    if m is None:
        raise HTTPException(status_code=404, detail="Меню не найдено")

    success, message = buttons_manager.update_button(
        menu, index, text, kind, value,
    )
    log.info("BUTTON_EDIT %s[%d]: ok=%s msg=%s", menu, index, success, message)

    if success:
        return RedirectResponse(
            f"/buttons/{menu}?saved={index}",
            status_code=303,
        )

    # При ошибке перерисовываем форму с введёнными значениями
    btn = m["flat"][index]
    btn["text"] = text
    btn["kind"] = kind
    btn["value"] = value
    return templates.TemplateResponse(
        "button_edit.html",
        {
            "request": request, "menu": m, "btn": btn,
            "user": request.session.get("user"),
            "error": message, "readonly": False,
        },
        status_code=400,
    )


@app.post("/buttons/{menu}/{index}/move", response_class=HTMLResponse)
async def button_move(
    request: Request, menu: str, index: int,
    direction: str = Form(...),
):
    _require_auth(request)
    success, message = buttons_manager.move_button(menu, index, direction)
    log.info("BUTTON_MOVE %s[%d] %s: ok=%s msg=%s",
             menu, index, direction, success, message)

    if success:
        new_index = index - 1 if direction == "up" else index + 1
        return RedirectResponse(
            f"/buttons/{menu}?saved={new_index}",
            status_code=303,
        )

    # При ошибке возвращаемся на страницу меню — рендерим её с ошибкой
    m = buttons_manager.get_menu(menu)
    return templates.TemplateResponse(
        "buttons_menu.html",
        {
            "request": request, "menu": m,
            "user": request.session.get("user"),
            "error": message, "saved_index": None,
        },
        status_code=400,
    )


@app.post("/buttons/{menu}/{index}/delete", response_class=HTMLResponse)
async def button_delete(request: Request, menu: str, index: int):
    _require_auth(request)
    success, message = buttons_manager.delete_button(menu, index)
    log.info("BUTTON_DELETE %s[%d]: ok=%s msg=%s",
             menu, index, success, message)

    if success:
        return RedirectResponse(f"/buttons/{menu}?deleted=1", status_code=303)

    m = buttons_manager.get_menu(menu)
    return templates.TemplateResponse(
        "buttons_menu.html",
        {
            "request": request, "menu": m,
            "user": request.session.get("user"),
            "error": message, "saved_index": None,
        },
        status_code=400,
    )


# ============================================================
#  ТИКЕТЫ (история переписки)
# ============================================================

@app.get("/tickets", response_class=HTMLResponse)
async def tickets_list(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
    source: str | None = None,  # 'tg' | 'web' | None
):
    _require_auth(request)

    if status and status not in ("open", "closed"):
        status = None
    if source and source not in ("tg", "web"):
        source = None
    page = max(1, page)
    per_page = 50
    offset = (page - 1) * per_page

    try:
        result = await tickets_view.list_tickets(
            status=status, search=q, limit=per_page, offset=offset,
            source=source,
        )
        error = None
    except Exception as e:
        log.exception("tickets_list failed")
        result = {"tickets": [], "total": 0}
        error = f"Ошибка чтения БД: {e}"

    # [v3.5] Считаем возраст каждого открытого тикета для SLA-индикатора
    from datetime import datetime as _dt
    _now = _dt.utcnow()
    for t in result.get("tickets", []):
        if t.get("status") != "open":
            continue
        # Берём время последнего сообщения (или создания если нет)
        ts_str = t.get("last_message_at") or t.get("created_at")
        if not ts_str:
            continue
        try:
            # SQLite сохраняет в формате 'YYYY-MM-DD HH:MM:SS'
            if isinstance(ts_str, str):
                ts = _dt.fromisoformat(ts_str.replace("T", " ").split(".")[0])
            else:
                ts = ts_str
            age = (_now - ts).total_seconds() / 60.0
            t["age_minutes"] = max(0, int(age))
        except Exception:
            pass

    total_pages = max(1, (result["total"] + per_page - 1) // per_page)

    # Статистика веб-чатов
    web_stats = {"total": 0, "active_today": 0, "messages_today": 0, "open_now": 0}
    # Счётчики для плашек TG и Web (одинаковая структура)
    tg_count = tg_open = tg_closed = 0
    web_count = web_open = web_closed = web_online_now = 0

    try:
        import aiosqlite
        from app.database import DB_PATH
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                # TG счётчики
                cur = await db.execute("SELECT COUNT(*) FROM tickets")
                tg_count = (await cur.fetchone())[0]
                cur = await db.execute(
                    "SELECT COUNT(*) FROM tickets WHERE status='open'"
                )
                tg_open = (await cur.fetchone())[0]
                tg_closed = tg_count - tg_open
            except Exception:
                pass

            try:
                # [v3.5] "Всего" = только visitor с реальной перепиской.
                # Иначе показывает миллион записей "просто открыл сайт".
                # ОПТИМИЗАЦИЯ: WHERE EXISTS (SELECT...) на каждую строку
                # — N+1. Делаем DISTINCT JOIN с web_messages — один проход.
                cur = await db.execute(
                    "SELECT COUNT(DISTINCT v.visitor_id) FROM web_visitors v "
                    "INNER JOIN web_messages m ON m.visitor_id = v.visitor_id"
                )
                web_stats["total"] = (await cur.fetchone())[0]
                web_count = web_stats["total"]
                cur = await db.execute(
                    "SELECT COUNT(*) FROM web_visitors "
                    "WHERE datetime(last_active_at) > datetime('now', '-1 day')"
                )
                web_stats["active_today"] = (await cur.fetchone())[0]
                cur = await db.execute(
                    "SELECT COUNT(*) FROM web_messages "
                    "WHERE datetime(created_at) > datetime('now', '-1 day')"
                )
                web_stats["messages_today"] = (await cur.fetchone())[0]
                # «Открыты сейчас» = активны за 10 минут
                cur = await db.execute(
                    "SELECT COUNT(*) FROM web_visitors "
                    "WHERE datetime(last_active_at) > datetime('now', '-10 minutes')"
                )
                web_stats["open_now"] = (await cur.fetchone())[0]
                web_online_now = web_stats["open_now"]
                # [v3.5] Активные веб-чаты = со status='open' (эскалация была)
                # Колонка status добавлена миграцией; для старых БД где колонки
                # ещё нет — fallback на topic_id (исторический способ).
                try:
                    cur = await db.execute(
                        "SELECT COUNT(*) FROM web_visitors "
                        "WHERE COALESCE(status, 'closed') = 'open'"
                    )
                    web_open = (await cur.fetchone())[0]
                except Exception:
                    # Колонка status ещё не создана — fallback
                    cur = await db.execute(
                        "SELECT COUNT(*) FROM web_visitors WHERE topic_id IS NOT NULL"
                    )
                    web_open = (await cur.fetchone())[0]
                web_closed = web_count - web_open
            except Exception:
                pass  # таблицы могут не существовать на свежей установке
    except Exception:
        pass

    # [v3.5] SLA пороги для подсветки старых открытых тикетов
    try:
        from app import security
        sla_yellow, sla_red = await security.get_sla_thresholds()
    except Exception:
        sla_yellow, sla_red = 5, 30

    return templates.TemplateResponse(
        "tickets.html",
        {
            "request": request,
            "tickets": result["tickets"],
            "total": result["total"],
            "user": request.session.get("user"),
            "status": status,
            "source": source,
            "q": q or "",
            "page": page,
            "total_pages": total_pages,
            "error": error or request.query_params.get("error"),
            "message": request.query_params.get("message"),
            "web_stats": web_stats,
            "tg_count": tg_count,
            "tg_open": tg_open,
            "tg_closed": tg_closed,
            "web_count": web_count,
            "web_open": web_open,
            "web_closed": web_closed,
            "web_online_now": web_online_now,
            "sla_yellow": sla_yellow,
            "sla_red": sla_red,
            "total_open": (tg_open or 0) + (web_open or 0),
        },
    )


@app.get("/tickets/{ticket_id}", response_class=HTMLResponse)
async def ticket_view(request: Request, ticket_id: int):
    _require_auth(request)

    try:
        ticket = await tickets_view.get_ticket(ticket_id)
    except Exception as e:
        log.exception("ticket_view failed")
        raise HTTPException(status_code=500, detail=str(e))

    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")

    # Список авто-ответов для дропдауна в форме
    try:
        qa_list = qa_manager.list_answers()
    except Exception as e:
        log.warning("ticket_view: qa list failed: %s", e)
        qa_list = []

    # Блок с инфой о подписке клиента (если user_id известен)
    # Делается в фоне с таймаутом — чтобы страница не висела если 3xUIStore лагает
    subscription_html = ""
    if ticket.get("user_id"):
        try:
            from app import admin_panel
            subscription_html = await asyncio.wait_for(
                admin_panel.build_ticket_info_block(ticket["user_id"]),
                timeout=6.0,
            )
        except asyncio.TimeoutError:
            subscription_html = (
                "<i>⏱ Админка отвечает медленно — попробуй обновить страницу</i>"
            )
        except Exception as e:
            log.warning("ticket_view: subscription failed: %s", e)
            subscription_html = ""

    return templates.TemplateResponse(
        "ticket.html",
        {
            "request": request,
            "ticket": ticket,
            "user": request.session.get("user"),
            "topic_url": topic_url(ticket.get("topic_id")),
            "admin_panel_url": os.getenv("ADMIN_PANEL_URL", ""),
            "qa_list": qa_list,
            "subscription_html": subscription_html,
        },
    )


# ============================================================
#  Helper: отправка media group в Telegram (несколько фоток одним сообщением)
# ============================================================

async def _send_media_group(
    session, bot_token: str, chat_id: str, thread_id,
    photo_items: list[tuple[bytes, str]],
    first_caption: str = "",
) -> None:
    """
    Отправляет до 10 фоток одним сообщением через sendMediaGroup.

    В Telegram media group файлы передаются как multipart attached_<N>,
    а порядок и метаданные — в JSON-поле "media".

    Args:
        photo_items: список (bytes, filename)
        first_caption: caption ставится только на первое фото в группе
    """
    import aiohttp
    import json as _json

    if not photo_items:
        return

    # Telegram лимитирует группу до 10 файлов
    items = photo_items[:10]

    url = f"https://api.telegram.org/bot{bot_token}/sendMediaGroup"
    form = aiohttp.FormData()
    form.add_field("chat_id", chat_id)
    if thread_id is not None:
        form.add_field("message_thread_id", str(thread_id))

    media_array = []
    for i, (data, fname) in enumerate(items):
        attach_name = f"file_{i}"
        media_obj = {
            "type": "photo",
            "media": f"attach://{attach_name}",
        }
        if i == 0 and first_caption:
            media_obj["caption"] = first_caption
            media_obj["parse_mode"] = "HTML"
        media_array.append(media_obj)
        form.add_field(
            attach_name, data,
            filename=fname,
            content_type="application/octet-stream",
        )
    form.add_field("media", _json.dumps(media_array))

    r = await session.post(url, data=form)
    resp = await r.json()
    if not resp.get("ok"):
        log.warning("_send_media_group failed for chat %s: %s", chat_id, resp)


@app.post("/tickets/{ticket_id}/reply", response_class=HTMLResponse)
async def ticket_reply(
    request: Request, ticket_id: int,
    text: str = Form(""),
    qa_key: str = Form(""),
    photo: UploadFile = File(None),
):
    """
    Отправляет ответ оператора клиенту в TG.
    Принимает:
      - text: обычный текст ответа
      - qa_key: ключ авто-ответа из админки (если задан, его текст ИЛИ
                добавляется к text, либо используется как основной)
      - photo: фото-аттачмент (опционально)
    Хотя бы что-то должно быть (текст или фото).
    """
    _require_auth(request)

    # Авто-ответ
    qa_text = ""
    qa_photos: list[str] = []  # список имён файлов в QA_PHOTOS_DIR
    if qa_key:
        try:
            qa = qa_manager.get_answer(qa_key)
            if qa and qa.get("text"):
                qa_text = qa["text"]
            if qa and qa.get("photos"):
                qa_photos = list(qa["photos"])
        except Exception as e:
            log.warning("ticket_reply: qa лookup failed: %s", e)

    # Финальный текст
    # [v3.5 fix] Защита от дублирования: если оператор уже вставил QA-текст
    # в textarea (например JS «Вставить в текст»), и при этом qa_key пришёл —
    # не добавлять QA-текст ещё раз.
    final_text = (text or "").strip()
    if qa_text:
        if final_text:
            # Если text уже содержит qa_text (полностью или начало) — пропускаем
            qa_t_norm = qa_text.strip()
            text_norm = final_text.strip()
            if qa_t_norm and qa_t_norm in text_norm:
                # qa_text уже внутри text — ничего не делаем
                log.info("ticket_reply: qa_text уже в text, не дублируем")
            else:
                final_text = qa_t_norm + "\n\n" + text_norm
        else:
            final_text = qa_text
    if len(final_text) > 4000:
        final_text = final_text[:4000]

    has_photo = bool(photo and getattr(photo, "filename", ""))
    # Если оператор не загрузил своё фото, но у QA есть фото — используем фото QA
    has_qa_photo = bool(qa_photos) and not has_photo
    if not final_text and not has_photo and not has_qa_photo:
        return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)

    user = request.session.get("user", "Оператор")

    try:
        ticket = await tickets_view.get_ticket(ticket_id)
    except Exception as e:
        log.exception("ticket_reply: get_ticket failed")
        raise HTTPException(status_code=500, detail=str(e))

    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")

    topic_id = ticket.get("topic_id")
    user_id = ticket.get("user_id")
    if not topic_id or not user_id:
        raise HTTPException(
            status_code=400,
            detail="У тикета нет topic_id или user_id — ответить нельзя",
        )

    # Подпись оператора берём из настроек виджета (operator_label)
    operator_label = "Оператор"
    try:
        from app import widget_settings
        ws = await widget_settings.get_settings()
        if ws.get("operator_label"):
            operator_label = ws["operator_label"].strip()
    except Exception as e:
        log.warning("ticket_reply: не смог загрузить operator_label: %s", e)

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    support_chat = int(os.getenv("SUPPORT_CHAT_ID", "0"))
    if not bot_token or not support_chat:
        raise HTTPException(
            status_code=500,
            detail="BOT_TOKEN или SUPPORT_CHAT_ID не настроены",
        )

    # Если фото — читаем байты (либо загруженное оператором, либо из QA)
    # photo_items = список (bytes, filename) для отправки.
    # Один элемент = одно фото (или своё, или единственное QA).
    # Несколько = QA с несколькими фото → отправляем sendMediaGroup.
    photo_items: list[tuple[bytes, str]] = []
    if has_photo:
        try:
            single_bytes = await photo.read()
            photo_items.append((single_bytes, photo.filename or "photo.jpg"))
        except Exception as e:
            log.warning("ticket_reply: не смог прочитать фото: %s", e)
    elif has_qa_photo and qa_photos:
        # Берём фото(ки) из авто-ответа с диска
        for qa_p in qa_photos:
            try:
                full_path = qa_manager.get_photo_full_path(qa_p)
                if full_path and full_path.exists():
                    with open(full_path, "rb") as f:
                        photo_items.append((f.read(), qa_p))
                else:
                    log.warning("ticket_reply: QA-фото %s не найдено на диске", qa_p)
            except Exception as e:
                log.warning("ticket_reply: не смог прочитать QA-фото %s: %s", qa_p, e)
        if photo_items:
            log.info("ticket_reply: использую %d фото из QA %s",
                     len(photo_items), qa_key)

    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:

            # === Несколько фоток → sendMediaGroup ===
            if len(photo_items) >= 2:
                # caption ставим только на первое фото в группе
                caption_user = f"<b>{operator_label}:</b>"
                if final_text:
                    caption_user += "\n" + final_text
                # Лимит caption в media group = 1024 символа на первый item
                if len(caption_user) > 1024:
                    first_caption = f"<b>{operator_label}:</b>"
                    text_after = final_text
                else:
                    first_caption = caption_user
                    text_after = None

                # Шлём клиенту
                await _send_media_group(
                    session, bot_token, str(user_id), None,
                    photo_items, first_caption,
                )
                if text_after:
                    url_sm = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                    await session.post(url_sm, json={
                        "chat_id": user_id,
                        "text": f"<b>{operator_label}:</b>\n{text_after}",
                        "parse_mode": "HTML",
                    })

                # Дублируем в топик группы
                topic_caption = f"👨‍💼 <b>{user} (из админки):</b>"
                if final_text and len(final_text) < 900:
                    topic_caption += "\n" + final_text
                await _send_media_group(
                    session, bot_token, str(support_chat), topic_id,
                    photo_items, topic_caption,
                )

            # === Одно фото → sendPhoto ===
            elif len(photo_items) == 1:
                photo_bytes, photo_filename = photo_items[0]
                caption_user = f"<b>{operator_label}:</b>"
                if final_text:
                    caption_user += "\n" + final_text
                if len(caption_user) > 1024:
                    short_caption = f"<b>{operator_label}:</b>"
                else:
                    short_caption = caption_user

                # → клиенту
                url_photo_user = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
                form_user = aiohttp.FormData()
                form_user.add_field("chat_id", str(user_id))
                form_user.add_field("caption", short_caption)
                form_user.add_field("parse_mode", "HTML")
                form_user.add_field("photo", photo_bytes,
                                    filename=photo_filename,
                                    content_type="application/octet-stream")
                r_pu = await session.post(url_photo_user, data=form_user)
                resp_pu = await r_pu.json()
                if not resp_pu.get("ok"):
                    log.warning("ticket_reply: photo не доставлено клиенту: %s",
                                resp_pu)
                    # [v3.5 fix] HTML fallback — без parse_mode
                    if "parse" in str(resp_pu).lower() or "html" in str(resp_pu).lower():
                        form_user2 = aiohttp.FormData()
                        form_user2.add_field("chat_id", str(user_id))
                        form_user2.add_field("caption",
                                             short_caption.replace("<b>", "").replace("</b>", ""))
                        form_user2.add_field("photo", photo_bytes,
                                             filename=photo_filename,
                                             content_type="application/octet-stream")
                        r_pu2 = await session.post(url_photo_user, data=form_user2)
                        resp_pu2 = await r_pu2.json()
                        log.info(
                            "ticket_reply: фото fallback plain → %s",
                            "ok" if resp_pu2.get("ok") else resp_pu2,
                        )

                # Если текст длинный — отправим отдельным сообщением
                if len(caption_user) > 1024 and final_text:
                    url_sm = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                    r_sm = await session.post(url_sm, json={
                        "chat_id": user_id,
                        "text": f"<b>{operator_label}:</b>\n{final_text}",
                        "parse_mode": "HTML",
                    })
                    resp_sm = await r_sm.json()
                    if not resp_sm.get("ok"):
                        log.warning(
                            "ticket_reply: text после фото не доставлен клиенту: %s",
                            resp_sm,
                        )
                        # [v3.5 fix] Fallback: если HTML парсинг упал — пробуем
                        # без parse_mode (как plain-text). Это бывает когда в QA
                        # есть < > & не как теги. Без этого fallback клиент
                        # получает фото без текста, что выглядит как баг.
                        if "parse" in str(resp_sm).lower() or "html" in str(resp_sm).lower():
                            r_sm2 = await session.post(url_sm, json={
                                "chat_id": user_id,
                                "text": f"{operator_label}:\n{final_text}",
                            })
                            resp_sm2 = await r_sm2.json()
                            log.info(
                                "ticket_reply: fallback plain-text → %s",
                                "ok" if resp_sm2.get("ok") else resp_sm2,
                            )

                # → дублируем в топик группы
                url_photo_topic = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
                form_topic = aiohttp.FormData()
                form_topic.add_field("chat_id", str(support_chat))
                form_topic.add_field("message_thread_id", str(topic_id))
                topic_caption = f"👨‍💼 <b>{user} (из админки):</b>"
                if final_text and len(final_text) < 900:
                    topic_caption += "\n" + final_text
                form_topic.add_field("caption", topic_caption)
                form_topic.add_field("parse_mode", "HTML")
                form_topic.add_field("photo", photo_bytes,
                                     filename=photo_filename,
                                     content_type="application/octet-stream")
                await session.post(url_photo_topic, data=form_topic)

            # === Только текст (без фото) ===
            elif final_text:
                # → клиенту
                url_user = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                r1 = await session.post(url_user, json={
                    "chat_id": user_id,
                    "text": f"<b>{operator_label}:</b>\n{final_text}",
                    "parse_mode": "HTML",
                })
                resp1 = await r1.json()
                if not resp1.get("ok"):
                    log.warning("ticket_reply: не доставлено клиенту: %s", resp1)

                # → дублируем в топик
                url_topic = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                await session.post(url_topic, json={
                    "chat_id": support_chat,
                    "message_thread_id": topic_id,
                    "text": f"👨‍💼 <b>{user} (из админки):</b>\n{final_text}",
                    "parse_mode": "HTML",
                })

        # Сохраняем в БД для истории
        try:
            from app import database as _db_mod
            db_kind = "photo" if photo_items else "text"
            db_text = final_text or "[фото]"
            await _db_mod.save_message(
                ticket_id=ticket_id, user_id=user_id, topic_id=topic_id,
                direction="out", kind=db_kind, text=db_text,
                operator_name=user,
            )
            # AI: помечаем что оператор подключился → AI замолкает в этом тикете
            await _db_mod.mark_ticket_operator_joined(user_id)
        except Exception as e:
            log.warning("ticket_reply: save_message/mark_joined failed: %s", e)

        log.info("TICKET_REPLY %d by %s (photos=%d qa=%s): %.40r",
                 ticket_id, user, len(photo_items), qa_key or "—", final_text)
    except Exception as e:
        log.exception("ticket_reply: failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    return RedirectResponse(f"/tickets/{ticket_id}#reply-form", status_code=303)


@app.post("/tickets/{ticket_id}/close", response_class=HTMLResponse)
async def ticket_close(
    request: Request, ticket_id: int,
    silent: str = Form(""),  # "1" если тихое закрытие
):
    """
    Закрывает тикет из админки.
    silent=1 → клиенту НЕ шлём уведомление о закрытии (только в группе пометка).
    """
    _require_auth(request)
    is_silent = silent == "1"

    try:
        ticket = await tickets_view.get_ticket(ticket_id)
    except Exception as e:
        log.exception("ticket_close: get_ticket failed")
        raise HTTPException(status_code=500, detail=str(e))

    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")

    topic_id = ticket.get("topic_id")
    user_id = ticket.get("user_id")
    user = request.session.get("user", "Оператор")

    # Закрываем в БД
    # [v3.5] КРИТИЧНО: закрываем ВСЕ open тикеты клиента, не только конкретный.
    # Иначе при возврате клиента бот находит другой open тикет в БД и
    # сообщение клиента форвардится тихо вместо переоткрытия со звуком.
    try:
        import aiosqlite as _aiosqlite
        async with _aiosqlite.connect(DB_PATH) as db:
            if user_id:
                # Закрываем все открытые тикеты этого клиента
                cur = await db.execute(
                    "UPDATE tickets SET status='closed' "
                    "WHERE user_id=? AND status='open'",
                    (user_id,),
                )
                closed_count = cur.rowcount
            else:
                cur = await db.execute(
                    "UPDATE tickets SET status='closed' WHERE id=?",
                    (ticket_id,),
                )
                closed_count = cur.rowcount
            await db.commit()
        log.info(
            "TICKET_CLOSE %d by %s (silent=%s, closed_count=%d, user_id=%s)",
            ticket_id, user, is_silent, closed_count, user_id,
        )
    except Exception as e:
        log.exception("ticket_close: db update failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    # [v3.5] Очищаем AI-историю клиента — при следующем обращении AI
    # начнёт с чистого листа.
    if user_id:
        try:
            from app import ai_assistant
            await ai_assistant.clear_history(str(user_id))
        except Exception as e:
            log.warning("ticket_close: AI cleanup failed: %s", e)

    # Уведомление в TG: в группу и (если не silent) клиенту
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    support_chat = int(os.getenv("SUPPORT_CHAT_ID", "0"))
    if bot_token and support_chat:
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                # [v3.5] Меняем только иконку — НЕ слать "🔒 Тикет закрыт"
                # в топик. Админ закрыл через UI и уже видит результат —
                # дублирующее сообщение в топике только засоряет диалог.
                if topic_id:
                    await _set_topic_icon_via_api(
                        session, bot_token, support_chat, topic_id, "closed",
                    )

                # Клиенту — только если не silent
                if user_id and not is_silent:
                    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                    try:
                        await session.post(url, json={
                            "chat_id": user_id,
                            "text": "🔒 <b>Ваш тикет закрыт</b>\n\nЕсли проблема "
                                    "появится снова — создайте новый.",
                            "parse_mode": "HTML",
                        })
                    except Exception:
                        pass  # клиент мог заблокировать бота
        except Exception as e:
            log.warning("ticket_close: notify failed: %s", e)

    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


# ============================================================
#  Helper: смена иконки топика из админки.
#  Админка — отдельный контейнер, без доступа к bot-объекту, поэтому
#  делаем прямые HTTP-вызовы. Иконки кэшируются в _ICON_CACHE при
#  первом обращении.
# ============================================================

# Те же preferences что в app/topic_icons.py — 4 роли по 1 эмодзи.
# [v3.5] tg и web обе используют 💬, источник виден через префикс имени.
_ICON_PREFS_ADMIN = {
    "tg":     ["💬"],
    "web":    ["💬"],
    "closed": ["✅"],
    "banned": ["🤡"],
}

_ICON_CACHE: dict[str, str] = {}  # emoji → custom_emoji_id
_ICON_LOADED = False


async def _ensure_icons_loaded(session, bot_token: str) -> None:
    """Подгружает доступные иконки топиков через Bot API (один раз)."""
    global _ICON_LOADED
    if _ICON_LOADED:
        return
    try:
        url = f"https://api.telegram.org/bot{bot_token}/getForumTopicIconStickers"
        async with session.get(url) as r:
            data = await r.json()
        if data.get("ok"):
            for sticker in data["result"]:
                emoji = sticker.get("emoji")
                cid = sticker.get("custom_emoji_id")
                if emoji and cid and emoji not in _ICON_CACHE:
                    _ICON_CACHE[emoji] = cid
            log.info("admin: подгружено %d иконок топиков", len(_ICON_CACHE))
        _ICON_LOADED = True
    except Exception as e:
        log.warning("admin: не удалось загрузить иконки топиков: %s", e)


def _resolve_icon(role: str) -> tuple[str | None, str | None]:
    """Возвращает (emoji, icon_id) для роли. None если ничего нет."""
    for e in _ICON_PREFS_ADMIN.get(role, []):
        cid = _ICON_CACHE.get(e)
        if cid:
            return e, cid
    return None, None


async def _set_topic_icon_via_api(
    session, bot_token: str, chat_id: int, topic_id: int, role: str,
) -> bool:
    """
    Меняет иконку топика через прямой HTTP-вызов editForumTopic.
    Возвращает True если успешно.

    КЛЮЧЕВОЙ МОМЕНТ: editForumTopic требует передавать name и
    icon_custom_emoji_id ВМЕСТЕ — иначе один из параметров игнорируется.
    Получаем текущее имя топика через getForumTopicIconStickers/иначе и
    передаём вместе.
    """
    if not topic_id or not chat_id:
        return False
    await _ensure_icons_loaded(session, bot_token)
    emoji, icon_id = _resolve_icon(role)
    if not icon_id:
        log.warning(
            "admin: нет иконки для роли %r (в кэше %d эмодзи)",
            role, len(_ICON_CACHE),
        )
        return False
    try:
        url = f"https://api.telegram.org/bot{bot_token}/editForumTopic"
        # name не передаём — оно сохранится текущее
        async with session.post(url, json={
            "chat_id": chat_id,
            "message_thread_id": topic_id,
            "icon_custom_emoji_id": icon_id,
        }) as r:
            data = await r.json()
        if data.get("ok"):
            log.info(
                "admin set_topic_icon: топик %s → %s (роль %s)",
                topic_id, emoji, role,
            )
            return True
        else:
            desc = data.get("description", "")
            if "TOPIC_NOT_MODIFIED" in desc or "not modified" in desc.lower():
                # Иконка уже та же — не ошибка
                return True
            log.warning(
                "admin set_topic_icon failed: topic=%s role=%s response=%s",
                topic_id, role, data,
            )
            return False
    except Exception as e:
        log.warning("admin set_topic_icon exception: %s", e)
        return False


@app.post("/tickets/{ticket_id}/reopen", response_class=HTMLResponse)
async def ticket_reopen(request: Request, ticket_id: int):
    """Переоткрывает закрытый TG-тикет из админки."""
    _require_auth(request)

    try:
        ticket = await tickets_view.get_ticket(ticket_id)
    except Exception as e:
        log.exception("ticket_reopen: get_ticket failed")
        raise HTTPException(status_code=500, detail=str(e))

    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")

    topic_id = ticket.get("topic_id")
    user_id = ticket.get("user_id")
    user = request.session.get("user", "Оператор")

    try:
        import aiosqlite as _aiosqlite
        async with _aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE tickets SET status='open' WHERE id=?",
                (ticket_id,),
            )
            await db.commit()
        log.info("TICKET_REOPEN %d by %s", ticket_id, user)
    except Exception as e:
        log.exception("ticket_reopen: db update failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    # Уведомление в группу и попытка вернуть иконку открытого тикета
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    support_chat = int(os.getenv("SUPPORT_CHAT_ID", "0"))
    if bot_token and support_chat and topic_id:
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                await session.post(url, json={
                    "chat_id": support_chat,
                    "message_thread_id": topic_id,
                    "text": f"🔓 Тикет переоткрыт оператором {user} (из админки)",
                })
                # Если топик был закрыт — открываем
                url_reopen = f"https://api.telegram.org/bot{bot_token}/reopenForumTopic"
                try:
                    await session.post(url_reopen, json={
                        "chat_id": support_chat,
                        "message_thread_id": topic_id,
                    })
                except Exception:
                    pass
                # Меняем иконку обратно на «tg» (📱 — источник TG)
                await _set_topic_icon_via_api(
                    session, bot_token, support_chat, topic_id, "tg",
                )
        except Exception as e:
            log.warning("ticket_reopen: notify failed: %s", e)

    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


# ============================================================
#  ВЕБ-ЧАТЫ: открыть/закрыть из админки
# ============================================================

@app.post("/webchats/{visitor_id}/close", response_class=HTMLResponse)
async def webchat_close(
    request: Request, visitor_id: str,
    silent: str = Form(""),
):
    """
    Закрывает веб-чат. В нашей модели "закрыт" = topic_id сброшен в NULL
    (визитёр уходит в архив). Сама переписка сохраняется в web_messages.
    """
    _require_auth(request)
    is_silent = silent == "1"
    user = request.session.get("user", "Оператор")

    from app import web_chat_db
    visitor = await web_chat_db.get_visitor(visitor_id)
    if not visitor:
        raise HTTPException(status_code=404, detail="Чат не найден")

    old_topic_id = visitor.get("topic_id")

    # Уведомление в группу — ДО сброса topic_id (иначе некуда писать)
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    support_chat = int(os.getenv("SUPPORT_CHAT_ID", "0"))
    if bot_token and support_chat and old_topic_id:
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                # [v3.5] Меняем только иконку — НЕ слать "🔒 Веб-чат закрыт"
                # в топик (админ закрыл через UI и видит результат).
                await _set_topic_icon_via_api(
                    session, bot_token, support_chat, old_topic_id, "closed",
                )
                # Закрываем сам топик в TG
                try:
                    url_close = f"https://api.telegram.org/bot{bot_token}/closeForumTopic"
                    await session.post(url_close, json={
                        "chat_id": support_chat,
                        "message_thread_id": old_topic_id,
                    })
                except Exception:
                    pass
        except Exception as e:
            log.warning("webchat_close: TG notify failed: %s", e)

    # [v3.5] НОВАЯ ЛОГИКА: при закрытии topic_id ОСТАЁТСЯ — это
    # вечный тикет одного клиента. Меняем только status='closed'.
    # При повторном обращении клиента — тот же topic используется,
    # переоткрывается через reopen_forum_topic.
    try:
        from app import web_chat_db as _wcd
        await _wcd.set_visitor_status(visitor_id, "closed")
        log.info("WEBCHAT_CLOSE %s by %s (silent=%s, status='closed', "
                 "topic %s остаётся для истории)",
                 visitor_id, user, is_silent, old_topic_id)
    except Exception as e:
        log.exception("webchat_close: status update failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    # [v3.5] Сбрасываем AI-состояние визитёра — теперь если он напишет снова,
    # AI сможет ему отвечать (operator_joined=0, история очищена).
    try:
        await web_chat_db.reset_operator_joined(visitor_id)
        from app import ai_assistant
        await ai_assistant.clear_history(visitor_id)
    except Exception as e:
        log.warning("webchat_close: AI cleanup failed: %s", e)

    # Уведомить клиента в виджете через сохранение сообщения от оператора
    # (он увидит при следующем pollе)
    if not is_silent:
        try:
            await web_chat_db.add_message(
                visitor_id, "out",
                "🔒 Этот чат закрыт оператором. Если возникнут вопросы — "
                "напишите снова, и мы откроем новое обращение.",
                sender="Система",
            )
        except Exception as e:
            log.warning("webchat_close: client notify failed: %s", e)

    return RedirectResponse(f"/webchats/{visitor_id}", status_code=303)


@app.post("/webchats/{visitor_id}/reopen", response_class=HTMLResponse)
async def webchat_reopen(request: Request, visitor_id: str):
    """
    Переоткрывает веб-чат — создаёт новый топик в группе поддержки
    и привязывает к визитёру.
    """
    _require_auth(request)
    user = request.session.get("user", "Оператор")

    from app import web_chat_db
    visitor = await web_chat_db.get_visitor(visitor_id)
    if not visitor:
        raise HTTPException(status_code=404, detail="Чат не найден")

    if visitor.get("topic_id"):
        # Уже открыт
        return RedirectResponse(f"/webchats/{visitor_id}", status_code=303)

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    support_chat = int(os.getenv("SUPPORT_CHAT_ID", "0"))
    if not bot_token or not support_chat:
        raise HTTPException(
            status_code=500,
            detail="BOT_TOKEN или SUPPORT_CHAT_ID не настроены",
        )

    # Создаём новый топик в TG-группе (или переоткрываем старый, если был)
    import aiohttp
    new_topic_id = None
    reused = False
    last_topic_id = visitor.get("last_topic_id")

    async with aiohttp.ClientSession() as session:
        # Сначала пробуем переоткрыть существующий старый топик —
        # вся переписка в нём сохранена.
        if last_topic_id:
            try:
                url_reopen = f"https://api.telegram.org/bot{bot_token}/reopenForumTopic"
                async with session.post(url_reopen, json={
                    "chat_id": support_chat,
                    "message_thread_id": last_topic_id,
                }) as r:
                    rdata = await r.json()

                reopen_ok = rdata.get("ok") or "TOPIC_NOT_MODIFIED" in str(
                    rdata.get("description", "")
                )
                if reopen_ok:
                    # Пробуем послать тестовое сообщение — это даст 100% уверенность
                    # что топик существует. Если упадёт "message thread not found",
                    # значит топик был удалён вручную.
                    url_send = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                    async with session.post(url_send, json={
                        "chat_id": support_chat,
                        "message_thread_id": last_topic_id,
                        "text": f"🔓 <b>Чат переоткрыт оператором {user}</b> (из админки)",
                        "parse_mode": "HTML",
                    }) as r2:
                        sdata = await r2.json()
                    if sdata.get("ok"):
                        new_topic_id = last_topic_id
                        reused = True
                        await _set_topic_icon_via_api(
                            session, bot_token, support_chat, new_topic_id, "web",
                        )
                        log.info(
                            "WEBCHAT_REOPEN: переоткрыт старый топик %s для %s",
                            last_topic_id, visitor_id,
                        )
                    else:
                        log.warning(
                            "WEBCHAT_REOPEN: пробный sendMessage в %s провалился: %s — создаю новый",
                            last_topic_id, sdata,
                        )
                else:
                    log.warning(
                        "WEBCHAT_REOPEN: reopen старого топика %s провалился: %s — создаю новый",
                        last_topic_id, rdata,
                    )
            except Exception as e:
                log.warning(
                    "WEBCHAT_REOPEN: reopen старого топика exception: %s — создаю новый",
                    e,
                )

        # Если не получилось переоткрыть — создаём новый
        if not reused:
            try:
                name_parts = []
                if visitor.get("user_name"):
                    name_parts.append(visitor["user_name"])
                if visitor.get("user_id"):
                    name_parts.append(f"id:{visitor['user_id']}")
                if not name_parts:
                    name_parts.append(f"web:{visitor_id[:8]}")
                topic_name = "🌐 " + " · ".join(name_parts)
                if len(topic_name) > 128:
                    topic_name = topic_name[:125] + "…"

                await _ensure_icons_loaded(session, bot_token)
                _emoji, web_icon = _resolve_icon("web")

                url = f"https://api.telegram.org/bot{bot_token}/createForumTopic"
                payload = {"chat_id": support_chat, "name": topic_name}
                if web_icon:
                    payload["icon_custom_emoji_id"] = web_icon
                async with session.post(url, json=payload) as r:
                    resp = await r.json()
                if resp.get("ok"):
                    new_topic_id = resp["result"]["message_thread_id"]
                else:
                    raise RuntimeError(f"createForumTopic failed: {resp}")
            except Exception as e:
                log.exception("webchat_reopen: create topic failed")
                raise HTTPException(
                    status_code=500,
                    detail=f"Не удалось создать топик: {e}",
                )

    # Привязываем topic_id и чистим last_topic_id
    try:
        import aiosqlite as _aiosqlite
        async with _aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE web_visitors SET topic_id = ?, last_topic_id = NULL "
                "WHERE visitor_id = ?",
                (new_topic_id, visitor_id),
            )
            await db.commit()
        log.info("WEBCHAT_REOPEN %s by %s, topic=%s (reused=%s)",
                 visitor_id, user, new_topic_id, reused)
    except Exception as e:
        log.exception("webchat_reopen: db update failed: %s", e)

    # Контекстное сообщение в новый топик
    try:
        async with aiohttp.ClientSession() as session:
            ctx_parts = [
                f"🔓 <b>Чат переоткрыт оператором {user} из админки</b>",
                "",
                f"visitor_id: <code>{visitor_id}</code>",
            ]
            if visitor.get("user_id"):
                ctx_parts.append(f"user_id: <code>{visitor['user_id']}</code>")
            if visitor.get("user_email"):
                ctx_parts.append(f"email: {visitor['user_email']}")
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            await session.post(url, json={
                "chat_id": support_chat,
                "message_thread_id": new_topic_id,
                "text": "\n".join(ctx_parts),
                "parse_mode": "HTML",
            })
    except Exception as e:
        log.warning("webchat_reopen: notify topic failed: %s", e)

    return RedirectResponse(f"/webchats/{visitor_id}", status_code=303)


# ============================================================
#  УПРАВЛЕНИЕ ПОДПИСКОЙ КЛИЕНТА (продлить / уменьшить / сбросить)
#  Универсальные endpoint'ы — берут user_id из формы, поэтому работают
#  и для TG-тикетов, и для веб-чатов.
# ============================================================

@app.post("/subscription/extend", response_class=HTMLResponse)
async def subscription_extend(
    request: Request,
    user_id: int = Form(...),
    days: int = Form(...),
    redirect_to: str = Form("/tickets"),
):
    """Продлить подписку клиента на N дней. Клиента уведомляем."""
    _require_auth(request)
    if days <= 0 or days > 3650:
        raise HTTPException(status_code=400, detail="days должно быть 1..3650")
    operator = request.session.get("user", "Оператор")
    try:
        from app import admin_panel
        client = admin_panel.get_client()
        await client.start()
        success, msg = await client.extend_subscription(
            user_id, days, notify_user=True,
        )
        log.info(
            "SUB_EXTEND_WEB by=%s user=%s days=+%d ok=%s msg=%s",
            operator, user_id, days, success, msg,
        )
    except Exception as e:
        log.exception("subscription_extend failed: %s", e)
        success, msg = False, str(e)

    # Префикс в URL для тоста после редиректа
    flag = "sub_ok" if success else "sub_err"
    sep = "&" if "?" in redirect_to else "?"
    return RedirectResponse(
        f"{redirect_to}{sep}{flag}={days}d", status_code=303,
    )


@app.post("/subscription/reduce", response_class=HTMLResponse)
async def subscription_reduce(
    request: Request,
    user_id: int = Form(...),
    days: int = Form(...),
    redirect_to: str = Form("/tickets"),
):
    """Уменьшить срок подписки на N дней. Клиента НЕ уведомляем."""
    _require_auth(request)
    if days <= 0 or days > 3650:
        raise HTTPException(status_code=400, detail="days должно быть 1..3650")
    operator = request.session.get("user", "Оператор")
    try:
        from app import admin_panel
        client = admin_panel.get_client()
        await client.start()
        success, msg = await client.reduce_subscription(user_id, days)
        log.info(
            "SUB_REDUCE_WEB by=%s user=%s days=-%d ok=%s msg=%s",
            operator, user_id, days, success, msg,
        )
    except Exception as e:
        log.exception("subscription_reduce failed: %s", e)
        success, msg = False, str(e)

    flag = "sub_ok" if success else "sub_err"
    sep = "&" if "?" in redirect_to else "?"
    return RedirectResponse(
        f"{redirect_to}{sep}{flag}=-{days}d", status_code=303,
    )


@app.post("/subscription/revoke", response_class=HTMLResponse)
async def subscription_revoke(
    request: Request,
    user_id: int = Form(...),
    redirect_to: str = Form("/tickets"),
):
    """Сбросить (отозвать) подписку клиента."""
    _require_auth(request)
    operator = request.session.get("user", "Оператор")
    try:
        from app import admin_panel
        client = admin_panel.get_client()
        await client.start()
        success, msg = await client.revoke_subscription(user_id)
        log.info(
            "SUB_REVOKE_WEB by=%s user=%s ok=%s msg=%s",
            operator, user_id, success, msg,
        )
    except Exception as e:
        log.exception("subscription_revoke failed: %s", e)
        success, msg = False, str(e)

    flag = "sub_ok" if success else "sub_err"
    sep = "&" if "?" in redirect_to else "?"
    return RedirectResponse(
        f"{redirect_to}{sep}{flag}=revoke", status_code=303,
    )


# ============================================================
#  АВТО-ОТВЕТЫ ОПЕРАТОРА
# ============================================================

@app.get("/qa", response_class=HTMLResponse)
async def qa_index(
    request: Request,
    saved: str | None = None,
    deleted: str | None = None,
):
    _require_auth(request)
    try:
        items = qa_manager.list_answers()
        error = None
    except Exception as e:
        items = []
        error = f"Не удалось прочитать keyboards.py: {e}"
        log.exception("qa list failed")

    return templates.TemplateResponse(
        "qa.html",
        {
            "request": request,
            "items": items,
            "user": request.session.get("user"),
            "error": error,
            "saved_key": saved,
            "deleted_key": deleted,
        },
    )


@app.get("/qa/new", response_class=HTMLResponse)
async def qa_new_form(request: Request):
    _require_auth(request)
    return templates.TemplateResponse(
        "qa_new.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "form": {"key": "", "label": "", "text": ""},
            "error": None,
        },
    )


@app.post("/qa/new", response_class=HTMLResponse)
async def qa_new_save(
    request: Request,
    key: str = Form(...),
    label: str = Form(...),
    text: str = Form(...),
):
    _require_auth(request)
    # Нормализуем ключ
    key = key.strip().lower()
    if not key.startswith("qa_"):
        key = "qa_" + key

    ok, msg = qa_manager.create_answer(key, label, text)
    log.warning("QA_CREATE key=%s ok=%s msg=%s", key, ok, msg)
    if not ok:
        return templates.TemplateResponse(
            "qa_new.html",
            {
                "request": request,
                "user": request.session.get("user"),
                "form": {"key": key, "label": label, "text": text},
                "error": msg,
            },
            status_code=400,
        )
    return RedirectResponse(f"/qa?saved={key}", status_code=303)


@app.post("/qa/{key}/delete", response_class=HTMLResponse)
async def qa_delete(request: Request, key: str):
    _require_auth(request)
    ok, msg = qa_manager.delete_answer(key)
    log.warning("QA_DELETE key=%s ok=%s msg=%s", key, ok, msg)
    if not ok:
        # Возвращаемся на страницу редактора с ошибкой
        item = qa_manager.get_answer(key)
        return templates.TemplateResponse(
            "qa_edit.html",
            {
                "request": request,
                "item": item,
                "user": request.session.get("user"),
                "error": f"Удаление не удалось: {msg}",
            },
            status_code=400,
        )
    return RedirectResponse(f"/qa?deleted={key}", status_code=303)


@app.post("/qa/{key}/_toggle_hidden", response_class=HTMLResponse)
async def qa_toggle_hidden(
    request: Request,
    key: str,
    hidden: str = Form("0"),
):
    """
    [v3.5] Скрыть/показать авто-ответ (не удаляя из БД).
    hidden = "1" → скрыть (бот его не показывает),
    "0" → показать.
    """
    _require_auth(request)
    want_hide = hidden == "1"
    from app import content_db as _cdb
    ok = await _cdb.set_qa_hidden(key, want_hide)
    log.info("QA_TOGGLE_HIDDEN key=%s hidden=%s ok=%s",
             key, want_hide, ok)
    # Сигнал боту перечитать кеш
    try:
        from admin_web import texts_manager
        texts_manager._touch_signal()
    except Exception:
        pass
    if not ok:
        return RedirectResponse(
            "/qa?error=Авто-ответ не найден", status_code=303,
        )
    action = "скрыт" if want_hide else "показан"
    return RedirectResponse(
        f"/qa?message=Авто-ответ «{key}» {action}", status_code=303,
    )


# ============================================================
#  QA — фото авто-ответов
# ============================================================

@app.post("/qa/{key}/upload_photo", response_class=HTMLResponse)
async def qa_upload_photo(
    request: Request, key: str,
    photo: list[UploadFile] = File(...),
):
    """
    Загружает одно или несколько фото к авто-ответу. Каждое добавляется
    к существующему списку (не заменяет). Лимит — MAX_PHOTOS_PER_QA штук.
    """
    _require_auth(request)

    if not photo:
        return RedirectResponse(
            f"/qa/{key}?photo_err=Файл не выбран",
            status_code=303,
        )

    # Фильтруем пустые слоты (HTML может прислать <input type=file multiple> без выбора)
    valid_photos = [p for p in photo if p and getattr(p, "filename", "")]
    if not valid_photos:
        return RedirectResponse(
            f"/qa/{key}?photo_err=Файл не выбран",
            status_code=303,
        )

    uploaded = 0
    last_err = None
    for p in valid_photos:
        try:
            photo_bytes = await p.read()
        except Exception as e:
            last_err = f"Не удалось прочитать {p.filename}: {e}"
            log.warning("QA_PHOTO_UPLOAD read error: %s", last_err)
            continue

        ok, msg = qa_manager.add_photo(key, photo_bytes, p.filename)
        log.info("QA_PHOTO_UPLOAD key=%s file=%s ok=%s msg=%s (%d bytes)",
                 key, p.filename, ok, msg, len(photo_bytes))
        if ok:
            uploaded += 1
        else:
            last_err = msg
            # Прерываемся на первой ошибке (например, лимит)
            break

    if uploaded > 0:
        if last_err:
            # Часть загрузили, часть нет
            return RedirectResponse(
                f"/qa/{key}?photo_saved={uploaded}&photo_err={last_err}",
                status_code=303,
            )
        return RedirectResponse(
            f"/qa/{key}?photo_saved={uploaded}", status_code=303,
        )

    return RedirectResponse(
        f"/qa/{key}?photo_err={last_err or 'Не удалось загрузить'}",
        status_code=303,
    )


@app.post("/qa/{key}/remove_photo", response_class=HTMLResponse)
async def qa_remove_photo(
    request: Request, key: str,
    filename: str = Form(...),
):
    """Удаляет одно конкретное фото из списка."""
    _require_auth(request)
    # Защита от path traversal в имени
    if "/" in filename or ".." in filename or "\\" in filename:
        return RedirectResponse(
            f"/qa/{key}?photo_err=Некорректное имя файла",
            status_code=303,
        )
    ok, msg = qa_manager.remove_photo(key, filename)
    log.info("QA_PHOTO_REMOVE key=%s file=%s ok=%s msg=%s",
             key, filename, ok, msg)
    if ok:
        return RedirectResponse(f"/qa/{key}?photo_removed=1", status_code=303)
    return RedirectResponse(f"/qa/{key}?photo_err={msg}", status_code=303)


@app.post("/qa/{key}/clear_photo", response_class=HTMLResponse)
async def qa_clear_photo(request: Request, key: str):
    """Удаляет ВСЕ фото у авто-ответа."""
    _require_auth(request)
    ok, msg = qa_manager.clear_photo(key)
    log.info("QA_PHOTO_CLEAR key=%s ok=%s msg=%s", key, ok, msg)
    if ok:
        return RedirectResponse(f"/qa/{key}?photo_cleared=1", status_code=303)
    return RedirectResponse(f"/qa/{key}?photo_err={msg}", status_code=303)


@app.get("/qa/photo/{filename}")
async def qa_photo_serve(request: Request, filename: str):
    """
    Раздаёт файл QA-фото для превью в админке.
    Безопасность: filename проверяется на отсутствие '/', '..' и т.п.
    """
    _require_auth(request)
    # Простая защита от path traversal
    if "/" in filename or ".." in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Bad filename")

    full = qa_manager.QA_PHOTOS_DIR / filename
    if not full.exists() or not full.is_file():
        raise HTTPException(status_code=404, detail="Photo not found")

    # Определяем MIME по расширению
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    }
    media_type = mime_map.get(ext, "application/octet-stream")

    from fastapi.responses import FileResponse
    return FileResponse(str(full), media_type=media_type)


@app.get("/qa/{key}", response_class=HTMLResponse)
async def qa_edit(request: Request, key: str):
    _require_auth(request)
    item = qa_manager.get_answer(key)
    if item is None:
        raise HTTPException(status_code=404, detail="Авто-ответ не найден")

    return templates.TemplateResponse(
        "qa_edit.html",
        {
            "request": request,
            "item": item,
            "user": request.session.get("user"),
            "error": None,
        },
    )


@app.post("/qa/{key}", response_class=HTMLResponse)
async def qa_save(
    request: Request,
    key: str,
    label: str = Form(...),
    text: str = Form(...),
):
    _require_auth(request)
    item = qa_manager.get_answer(key)
    if item is None:
        raise HTTPException(status_code=404, detail="Авто-ответ не найден")

    # Сохраняем только то, что изменилось
    errors: list[str] = []

    if label != item["label"] and item["has_button"]:
        ok, msg = qa_manager.update_label(key, label)
        log.info("QA_LABEL %s: ok=%s msg=%s", key, ok, msg)
        if not ok:
            errors.append(f"Надпись: {msg}")

    if text != item["text"]:
        ok, msg = qa_manager.update_text(key, text)
        log.info("QA_TEXT %s: ok=%s msg=%s", key, ok, msg)
        if not ok:
            errors.append(f"Текст: {msg}")

    if errors:
        # Перерисовываем форму с введёнными значениями и ошибками
        item["label"] = label
        item["text"] = text
        return templates.TemplateResponse(
            "qa_edit.html",
            {
                "request": request,
                "item": item,
                "user": request.session.get("user"),
                "error": "; ".join(errors),
            },
            status_code=400,
        )

    return RedirectResponse(f"/qa?saved={key}", status_code=303)


# ============================================================
#  СЕРВИС (рестарт бота через Docker socket)
# ============================================================

# Pending-подтверждения рестарта: { session_user: {pin, expires_at} }
_pending_restarts: dict[str, dict] = {}
_RESTART_PIN_TTL = 90  # секунд


def _collect_system_metrics() -> dict:
    """
    [v3.5] Собирает системные метрики хоста для страницы /service.
    Без зависимости от psutil — использует только stdlib + /proc.
    Все ошибки тихие — если что-то не получилось, поле = None.

    Возвращает:
      {
        "db_size_mb": float,
        "db_path": str,
        "disk": {"total_gb": float, "used_gb": float, "free_gb": float, "percent": int},
        "ram": {"total_gb": float, "used_gb": float, "free_gb": float, "percent": int},
        "cpu": {"load_1": float, "load_5": float, "load_15": float,
                "cores": int, "percent_1min": int},
        "uptime_hours": float | None,
      }
    """
    import os as _os
    import shutil as _shutil

    metrics: dict = {}

    # === БД ===
    try:
        from app.database import DB_PATH as _DB_PATH
        db_path = _DB_PATH
    except Exception:
        db_path = "/app/data/vpn_support.db"
    try:
        size_bytes = _os.path.getsize(db_path)
        metrics["db_size_mb"] = round(size_bytes / (1024 * 1024), 2)
        metrics["db_path"] = db_path
    except Exception:
        metrics["db_size_mb"] = None
        metrics["db_path"] = db_path

    # === Диск (где лежит БД и docker volumes) ===
    try:
        # Берём корень контейнера — в нём mount-ы данных как /app/data
        total, used, free = _shutil.disk_usage("/")
        metrics["disk"] = {
            "total_gb": round(total / (1024 ** 3), 2),
            "used_gb":  round(used  / (1024 ** 3), 2),
            "free_gb":  round(free  / (1024 ** 3), 2),
            "percent":  int(used / total * 100) if total else 0,
        }
    except Exception:
        metrics["disk"] = None

    # === RAM — из /proc/meminfo ===
    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                val_str = parts[1].strip().split()[0]
                try:
                    meminfo[key] = int(val_str)  # kB
                except ValueError:
                    pass
        total_kb = meminfo.get("MemTotal", 0)
        # MemAvailable точнее MemFree — учитывает кеш
        avail_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        used_kb = total_kb - avail_kb
        metrics["ram"] = {
            "total_gb": round(total_kb / (1024 ** 2), 2),
            "used_gb":  round(used_kb  / (1024 ** 2), 2),
            "free_gb":  round(avail_kb / (1024 ** 2), 2),
            "percent":  int(used_kb / total_kb * 100) if total_kb else 0,
        }
    except Exception:
        metrics["ram"] = None

    # === CPU ===
    try:
        load_1, load_5, load_15 = _os.getloadavg()
        cores = _os.cpu_count() or 1
        # Грубая оценка % загрузки за 1 мин — load_1 / cores * 100
        percent_1min = min(100, int(load_1 / cores * 100))
        metrics["cpu"] = {
            "load_1":  round(load_1, 2),
            "load_5":  round(load_5, 2),
            "load_15": round(load_15, 2),
            "cores":   cores,
            "percent_1min": percent_1min,
        }
    except Exception:
        metrics["cpu"] = None

    # === Uptime хоста (часов) ===
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        metrics["uptime_hours"] = round(secs / 3600, 1)
    except Exception:
        metrics["uptime_hours"] = None

    return metrics


@app.get("/service", response_class=HTMLResponse)
async def service_page(
    request: Request,
    update_token: str | None = None,
    applied: str | None = None,
):
    _require_auth(request)
    try:
        info = await docker_ctl.container_info()
    except Exception as e:
        log.exception("container_info failed")
        info = {"error": str(e), "available": False}

    user = request.session.get("user", "")
    pending = _pending_restarts.get(user)
    if pending and pending["expires_at"] < _time.time():
        _pending_restarts.pop(user, None)
        pending = None

    # Превью обновления, если есть токен
    update_preview = None
    if update_token:
        update_preview = update_manager.analyze_upload(update_token)

    # [v3.5] Системные метрики (диск, RAM, CPU, размер БД)
    sys_metrics = _collect_system_metrics()

    return templates.TemplateResponse(
        "service.html",
        {
            "request": request,
            "info": info,
            "user": user,
            "pending": pending,
            "error": None,
            "ok": None,
            "sys_metrics": sys_metrics,
            "update_preview": update_preview,
            "update_applied": applied,
            "update_max_mb": update_manager.MAX_ZIP_SIZE // 1024 // 1024,
            "update_error": None,
        },
    )


@app.post("/service/restart-request", response_class=HTMLResponse)
async def restart_request(request: Request):
    """Шаг 1: оператор нажал «Перезапустить бота» — выдаём ПИН."""
    _require_auth(request)
    user = request.session.get("user", "")

    pin = f"{secrets.randbelow(10000):04d}"
    _pending_restarts[user] = {
        "pin": pin,
        "expires_at": _time.time() + _RESTART_PIN_TTL,
    }
    log.warning("RESTART_REQUEST user=%s pin_issued", user)

    info = await docker_ctl.container_info()
    return templates.TemplateResponse(
        "service.html",
        {
            "request": request,
            "info": info,
            "user": user,
            "pending": _pending_restarts[user],
            "error": None,
            "ok": None,
        },
    )


@app.post("/service/restart-confirm", response_class=HTMLResponse)
async def restart_confirm(request: Request, pin: str = Form(...)):
    """Шаг 2: оператор ввёл ПИН — если совпал, делаем рестарт."""
    _require_auth(request)
    user = request.session.get("user", "")
    pending = _pending_restarts.get(user)

    info = await docker_ctl.container_info()

    if not pending:
        return templates.TemplateResponse(
            "service.html",
            {
                "request": request,
                "info": info,
                "user": user,
                "pending": None,
                "error": "Нет активного запроса. Нажми «Перезапустить» снова.",
                "ok": None,
            },
            status_code=400,
        )

    if _time.time() > pending["expires_at"]:
        _pending_restarts.pop(user, None)
        return templates.TemplateResponse(
            "service.html",
            {
                "request": request,
                "info": info,
                "user": user,
                "pending": None,
                "error": "ПИН истёк (даётся 90 секунд). Попробуй ещё раз.",
                "ok": None,
            },
            status_code=400,
        )

    if pin.strip() != pending["pin"]:
        return templates.TemplateResponse(
            "service.html",
            {
                "request": request,
                "info": info,
                "user": user,
                "pending": pending,
                "error": "Неверный ПИН.",
                "ok": None,
            },
            status_code=400,
        )

    # ПИН верный — выполняем полный rebuild (down + up --build) detached
    # [v3.5] Вместо simple restart делаем полный цикл: down + up --build.
    # Это пересобирает образ если Dockerfile/requirements обновились.
    # Команда detached — контейнер админки умрёт во время down,
    # потом восстановится при up. Через ~60-90 сек страница доступна снова.
    _pending_restarts.pop(user, None)
    success, message = await docker_ctl.rebuild_down_up()
    log.warning("REBUILD user=%s success=%s msg=%s", user, success, message)

    # Запрашиваем статус ещё раз — он мог измениться
    info = await docker_ctl.container_info()

    return templates.TemplateResponse(
        "service.html",
        {
            "request": request,
            "info": info,
            "user": user,
            "pending": None,
            "error": None if success else message,
            "ok": message if success else None,
        },
    )


# ============================================================
#  ОБНОВЛЕНИЕ ЧЕРЕЗ ЗАГРУЗКУ ZIP
# ============================================================

# ============================================================
#  ОБНОВЛЕНИЕ ЧЕРЕЗ ЗАГРУЗКУ ZIP
#  Интегрировано со страницей /service — всё рендерится через service.html
# ============================================================

async def _render_service(
    request: Request,
    *,
    update_preview=None,
    update_applied=None,
    update_error=None,
    error=None,
    ok=None,
    status_code: int = 200,
):
    """Универсальный рендер service.html со всем необходимым контекстом."""
    try:
        info = await docker_ctl.container_info()
    except Exception as e:
        log.exception("container_info failed")
        info = {"error": str(e), "available": False}

    user = request.session.get("user", "")
    pending = _pending_restarts.get(user)
    if pending and pending["expires_at"] < _time.time():
        _pending_restarts.pop(user, None)
        pending = None

    return templates.TemplateResponse(
        "service.html",
        {
            "request": request,
            "info": info,
            "user": user,
            "pending": pending,
            "error": error,
            "ok": ok,
            "update_preview": update_preview,
            "update_applied": update_applied,
            "update_max_mb": update_manager.MAX_ZIP_SIZE // 1024 // 1024,
            "update_error": update_error,
        },
        status_code=status_code,
    )


@app.post("/update/upload", response_class=HTMLResponse)
async def update_upload(
    request: Request,
    archive: UploadFile = File(...),
):
    """Принимает zip-файл и редиректит на /service?update_token=..."""
    _require_auth(request)

    if not archive.filename:
        return await _render_service(
            request, update_error="Файл не выбран", status_code=400,
        )

    try:
        content = await archive.read()
    except Exception as e:
        log.exception("upload read failed")
        return await _render_service(
            request, update_error=f"Не удалось прочитать файл: {e}",
            status_code=400,
        )

    result = update_manager.upload_zip(content, archive.filename)
    log.info("UPLOAD %s: ok=%s size=%d", archive.filename, result["ok"], len(content))

    if not result["ok"]:
        return await _render_service(
            request, update_error=result["message"], status_code=400,
        )

    return RedirectResponse(f"/service?update_token={result['token']}", status_code=303)


@app.post("/update/request-pin", response_class=HTMLResponse)
async def update_request_pin(request: Request, token: str = Form(...)):
    """Запрашиваем ПИН для применения архива."""
    _require_auth(request)
    pin_info = update_manager.request_apply_pin(token)
    if pin_info is None:
        return RedirectResponse("/service", status_code=303)
    preview = update_manager.analyze_upload(token)
    if preview is not None:
        preview["pin"] = pin_info["pin"]
        preview["pin_expires_in"] = pin_info["expires_in"]
    return await _render_service(request, update_preview=preview)


@app.post("/update/apply", response_class=HTMLResponse)
async def update_apply(
    request: Request,
    background: BackgroundTasks,
    token: str = Form(...),
    pin: str = Form(...),
):
    """Применяет обновление + запускает асинхронный rebuild контейнеров."""
    _require_auth(request)
    user = request.session.get("user", "?")

    result = update_manager.apply_upload(token, pin)
    log.warning("APPLY_UPDATE user=%s ok=%s msg=%s",
                user, result["ok"], result["message"])

    if not result["ok"]:
        preview = update_manager.analyze_upload(token)
        return await _render_service(
            request, update_preview=preview,
            update_error=result["message"], status_code=400,
        )

    # Применение прошло — запускаем перезапуск контейнера в фоне.
    # [v3.5] КРИТИЧНО: используем RESTART, а НЕ rebuild_all.
    # `rebuild_all` делает `docker compose up -d --build` — это пересобирает
    # образ из Dockerfile с хостовых исходников, что ЗАТИРАЕТ файлы которые
    # мы только что распаковали внутри контейнера. Restart просто
    # перезапускает Python-процесс, файлы в /app/ сохраняются.
    background.add_task(_run_restart_async)

    return await _render_service(
        request,
        update_applied={
            "extracted": result["extracted_count"],
            "backed_up": result["backed_up_count"],
            "backup_path": result["backup_path"],
        },
    )


async def _run_restart_async():
    """[v3.5] Фоновая задача: перезапускает контейнер после обновления.
    НЕ делает rebuild — иначе файлы из архива потерялись бы при пересборке
    образа из Dockerfile (`COPY . /app` берёт с хоста, не из контейнера).
    """
    import asyncio
    await asyncio.sleep(2)
    log.warning("RESTART after update: starting...")
    try:
        ok, msg = await docker_ctl.restart_container()
        log.warning("RESTART finished: ok=%s msg=%s", ok, msg)
    except Exception as e:
        log.exception("RESTART failed: %s", e)


async def _run_rebuild_async():
    """Фоновая задача: docker compose up -d --build.
    [v3.5] Больше НЕ используется после apply_upload — оставлено для
    ручной кнопки «Пересобрать» в админке если такая будет.
    """
    import asyncio
    await asyncio.sleep(2)
    log.warning("REBUILD: starting docker compose up -d --build")
    try:
        ok, msg = await docker_ctl.rebuild_all()
        log.warning("REBUILD finished: ok=%s msg=%s", ok, msg)
    except Exception as e:
        log.exception("REBUILD failed: %s", e)


# ============================================================
#  AI-АССИСТЕНТ
# ============================================================

def _mask_api_key(key: str | None) -> str:
    """Маскирует ключ для отображения: 'sk-abc...xyz9'."""
    if not key or len(key) < 12:
        return "—"
    return key[:7] + "..." + key[-4:]


@app.get("/ai", response_class=HTMLResponse)
async def ai_settings_page(
    request: Request,
    message: str | None = None,
    error: str | None = None,
):
    _require_auth(request)
    current_provider = ai_settings.get_provider()
    api_key = ai_settings.get_api_key()  # ключ текущего провайдера
    stats = ai_settings.stats_summary(days=7)

    # Список провайдеров для дропдауна
    providers_list = []
    for pid, cfg in ai_settings.PROVIDERS.items():
        provider_key = ai_settings.get_api_key(pid)
        providers_list.append({
            "id": pid,
            "name": cfg["name"],
            "free": cfg["free"],
            "description": cfg["description"],
            "api_keys_url": cfg["api_keys_url"],
            "api_key_prefix": cfg["api_key_prefix"],
            "models": cfg["models"],
            "has_key": bool(provider_key),
            "key_masked": _mask_api_key(provider_key) if provider_key else "—",
        })

    return templates.TemplateResponse(
        "ai.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "enabled": ai_settings.is_enabled(),
            "current_provider": current_provider,
            "providers": providers_list,
            "current_model": ai_settings.get_model(),
            "api_key_set": bool(api_key),
            "api_key_masked": _mask_api_key(api_key),
            # Для обратной совместимости в шаблоне:
            "model": ai_settings.get_model(),
            "max_tokens": ai_settings.get_max_tokens_per_user_day(),
            "system_prompt": ai_settings.get_system_prompt(),
            "assistant_name": ai_settings.get_assistant_name(),
            "telegram_extra": ai_settings.get_telegram_extra(),
            "webchat_extra": ai_settings.get_webchat_extra(),
            "stats": stats,
            "message": message,
            "error": error,
        },
    )


@app.post("/ai/toggle", response_class=HTMLResponse)
async def ai_toggle(request: Request):
    _require_auth(request)
    new_state = not ai_settings.is_enabled()
    # Включить можно только если есть ключ
    if new_state and not ai_settings.get_api_key():
        return RedirectResponse(
            "/ai?error=Сначала добавьте ключ OpenAI", status_code=303,
        )
    ai_settings.set_enabled(new_state)
    log.warning("AI_TOGGLE user=%s → %s",
                request.session.get("user"), new_state)
    msg = "AI включён" if new_state else "AI выключен"
    return RedirectResponse(f"/ai?message={msg}", status_code=303)


@app.post("/ai/save_key", response_class=HTMLResponse)
async def ai_save_key(
    request: Request,
    api_key: str = Form(""),
    provider: str = Form(""),
):
    _require_auth(request)
    api_key = (api_key or "").strip()
    provider = (provider or "").strip().lower()
    if provider not in ai_settings.PROVIDERS:
        provider = ai_settings.get_provider()
    if not api_key:
        return RedirectResponse(
            "/ai?error=Ключ не указан", status_code=303,
        )
    # Проверяем префикс ключа (sk- для OpenAI, gsk_ для Groq)
    expected_prefix = ai_settings.PROVIDERS[provider]["api_key_prefix"]
    if expected_prefix and not api_key.startswith(expected_prefix):
        return RedirectResponse(
            f"/ai?error=Ключ {ai_settings.PROVIDERS[provider]['name']} "
            f"должен начинаться с «{expected_prefix}»",
            status_code=303,
        )
    ai_settings.set_api_key(api_key, provider=provider)
    log.warning("AI_API_KEY_SET user=%s provider=%s",
                request.session.get("user"), provider)
    return RedirectResponse(
        f"/ai?message=Ключ {ai_settings.PROVIDERS[provider]['name']} сохранён",
        status_code=303,
    )


@app.post("/ai/clear_key", response_class=HTMLResponse)
async def ai_clear_key(request: Request, provider: str = Form("")):
    _require_auth(request)
    provider = (provider or "").strip().lower()
    if provider not in ai_settings.PROVIDERS:
        provider = ai_settings.get_provider()
    ai_settings.set_api_key(None, provider=provider)
    # Если удалили ключ ТЕКУЩЕГО провайдера — заодно выключаем AI
    if provider == ai_settings.get_provider():
        ai_settings.set_enabled(False)
    log.warning("AI_API_KEY_CLEARED user=%s provider=%s",
                request.session.get("user"), provider)
    return RedirectResponse(
        f"/ai?message=Ключ {ai_settings.PROVIDERS[provider]['name']} удалён",
        status_code=303,
    )


@app.post("/ai/test_key", response_class=HTMLResponse)
async def ai_test_key(request: Request, provider: str = Form("")):
    """Тестирует ключ указанного провайдера (или текущего активного)."""
    _require_auth(request)
    provider = (provider or "").strip().lower()
    if provider not in ai_settings.PROVIDERS:
        provider = ai_settings.get_provider()
    api_key = ai_settings.get_api_key(provider=provider)
    if not api_key:
        return RedirectResponse(
            f"/ai?error=Ключ {ai_settings.PROVIDERS[provider]['name']} не сохранён",
            status_code=303,
        )
    model = ai_settings.get_model(provider=provider)
    ok, msg = await ai_assistant.test_api_key(api_key, model, provider=provider)
    log.info("AI_TEST_KEY provider=%s ok=%s msg=%s", provider, ok, msg)
    if ok:
        return RedirectResponse(f"/ai?message={msg}", status_code=303)
    return RedirectResponse(f"/ai?error={msg}", status_code=303)


@app.post("/ai/switch_provider", response_class=HTMLResponse)
async def ai_switch_provider(request: Request, provider: str = Form(...)):
    """Переключает активный провайдер (например с OpenAI на Groq)."""
    _require_auth(request)
    provider = provider.strip().lower()
    if provider not in ai_settings.PROVIDERS:
        return RedirectResponse(
            f"/ai?error=Неизвестный провайдер: {provider}", status_code=303,
        )
    ai_settings.set_provider(provider)
    # Если ключа нового провайдера нет — выключаем AI
    if not ai_settings.get_api_key(provider=provider):
        ai_settings.set_enabled(False)
    log.warning("AI_PROVIDER_SWITCHED user=%s → %s",
                request.session.get("user"), provider)
    return RedirectResponse(
        f"/ai?message=Активный провайдер: {ai_settings.PROVIDERS[provider]['name']}",
        status_code=303,
    )


@app.post("/ai/save_config", response_class=HTMLResponse)
async def ai_save_config(
    request: Request,
    model: str = Form(""),
    max_tokens: int = Form(10000),
    provider: str = Form(""),
):
    _require_auth(request)
    # Провайдер — для какого сохраняем модель
    provider = (provider or "").strip().lower()
    if provider not in ai_settings.PROVIDERS:
        provider = ai_settings.get_provider()

    # Список разрешённых моделей зависит от провайдера
    allowed_models = {
        m[0] for m in ai_settings.PROVIDERS[provider]["models"]
    }
    if model and model not in allowed_models:
        return RedirectResponse(
            f"/ai?error=Неизвестная модель для {ai_settings.PROVIDERS[provider]['name']}: {model}",
            status_code=303,
        )
    if max_tokens < 500 or max_tokens > 100000:
        return RedirectResponse(
            "/ai?error=Лимит должен быть 500-100000", status_code=303,
        )
    if model:
        ai_settings.set_model(model, provider=provider)
    ai_settings.set_max_tokens_per_user_day(max_tokens)
    log.info("AI_CONFIG_SAVED provider=%s model=%s max_tokens=%d",
             provider, model, max_tokens)
    return RedirectResponse(
        "/ai?message=Настройки сохранены", status_code=303,
    )


@app.post("/ai/save_prompt", response_class=HTMLResponse)
async def ai_save_prompt(
    request: Request,
    system_prompt: str = Form(""),
    reset: str = Form(""),
):
    _require_auth(request)
    if reset:
        ai_settings.set_system_prompt("")  # пусто → дефолт
        log.info("AI_PROMPT_RESET")
        return RedirectResponse(
            "/ai?message=Промпт сброшен на дефолтный", status_code=303,
        )
    if not system_prompt or not system_prompt.strip():
        return RedirectResponse(
            "/ai?error=Промпт не может быть пустым", status_code=303,
        )
    if len(system_prompt) > 16000:
        return RedirectResponse(
            "/ai?error=Промпт слишком длинный (>16000 символов)", status_code=303,
        )
    ai_settings.set_system_prompt(system_prompt)
    log.info("AI_PROMPT_SAVED len=%d", len(system_prompt))
    return RedirectResponse(
        "/ai?message=Промпт сохранён", status_code=303,
    )


@app.post("/ai/save_name", response_class=HTMLResponse)
async def ai_save_name(
    request: Request,
    assistant_name: str = Form(""),
    reset: str = Form(""),
):
    """Сохраняет имя AI-ассистента (то что видит клиент)."""
    _require_auth(request)
    if reset:
        ai_settings.set_assistant_name(None)  # сброс на дефолт
        log.info("AI_NAME_RESET user=%s", request.session.get("user"))
        return RedirectResponse(
            f"/ai?message=Имя сброшено на «{ai_settings.DEFAULT_ASSISTANT_NAME}»",
            status_code=303,
        )
    name = (assistant_name or "").strip()
    if not name:
        return RedirectResponse(
            "/ai?error=Имя не может быть пустым", status_code=303,
        )
    if len(name) > 60:
        return RedirectResponse(
            "/ai?error=Имя слишком длинное (>60 символов)", status_code=303,
        )
    ai_settings.set_assistant_name(name)
    log.info("AI_NAME_SAVED user=%s name=%r",
             request.session.get("user"), name)
    return RedirectResponse(
        f"/ai?message=Имя ассистента: «{name}»", status_code=303,
    )


@app.post("/ai/save_channel_prompts", response_class=HTMLResponse)
async def ai_save_channel_prompts(
    request: Request,
    telegram_extra: str = Form(""),
    webchat_extra: str = Form(""),
    reset: str = Form(""),
):
    """
    Сохраняет канал-специфичные блоки промпта для TG и веб-чата. [v3.5]
    Эти блоки AI получает дополнительно к основному промпту,
    в зависимости от того откуда пришёл клиент.
    """
    _require_auth(request)
    if reset:
        ai_settings.set_telegram_extra(None)  # → дефолт
        ai_settings.set_webchat_extra(None)
        log.info("AI_CHANNEL_PROMPTS_RESET user=%s",
                 request.session.get("user"))
        return RedirectResponse(
            "/ai?message=Канал-инструкции сброшены на дефолтные",
            status_code=303,
        )

    # Проверка длины
    if len(telegram_extra) > 3000 or len(webchat_extra) > 3000:
        return RedirectResponse(
            "/ai?error=Слишком длинный блок (>3000 символов)",
            status_code=303,
        )

    ai_settings.set_telegram_extra(telegram_extra)
    ai_settings.set_webchat_extra(webchat_extra)
    log.info(
        "AI_CHANNEL_PROMPTS_SAVED user=%s tg_len=%d web_len=%d",
        request.session.get("user"),
        len(telegram_extra), len(webchat_extra),
    )
    return RedirectResponse(
        "/ai?message=Канал-инструкции сохранены", status_code=303,
    )


# ============================================================
#  О БОТЕ
# ============================================================

@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    _require_auth(request)
    return templates.TemplateResponse(
        "about.html",
        {"request": request, "user": request.session.get("user")},
    )


@app.get("/install", response_class=HTMLResponse)
async def install_page(request: Request):
    """Подробная инструкция по установке и поддержке бота."""
    _require_auth(request)
    return templates.TemplateResponse(
        "install.html",
        {"request": request, "user": request.session.get("user")},
    )


# ============================================================
#  ОТПРАВИТЬ НОВОСТЬ (массовая рассылка)
# ============================================================

@app.get("/news", response_class=HTMLResponse)
async def news_page(
    request: Request,
    token: str | None = None,
    broadcast: str | None = None,
):
    _require_auth(request)

    recipients_count = await broadcast_manager.count_recipients()
    cooldown = broadcast_manager.cooldown_left()

    pending = None
    if token:
        pending = broadcast_manager.get_pending(token)

    return templates.TemplateResponse(
        "news.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "recipients_count": recipients_count,
            "cooldown_seconds": cooldown,
            "templates_": {
                "tech_start": broadcast_manager.get_template("tech_start"),
                "tech_end": broadcast_manager.get_template("tech_end"),
            },
            "pending_token": token,
            "pending": pending,
            "broadcast_id": broadcast,
            "error": None,
        },
    )


@app.post("/news/preview", response_class=HTMLResponse)
async def news_preview(
    request: Request,
    text: str = Form(...),
    photo: UploadFile | None = File(None),
):
    """Этап 1: подготовка — сохраняем текст, фото, выдаём ПИН."""
    _require_auth(request)

    photo_bytes = None
    photo_name = None
    if photo and photo.filename:
        try:
            photo_bytes = await photo.read()
            photo_name = photo.filename
        except Exception as e:
            log.exception("photo read failed")
            return await _news_render(request, error=f"Не удалось прочитать фото: {e}")

    ok, msg, token = await broadcast_manager.prepare_broadcast(
        text, photo_bytes, photo_name,
    )
    if not ok:
        return await _news_render(request, error=msg)

    log.info("BROADCAST prepared token=%s text_len=%d has_photo=%s",
             token[:8], len(text), bool(photo_bytes))
    return RedirectResponse(f"/news?token={token}", status_code=303)


@app.post("/news/send", response_class=HTMLResponse)
async def news_send(
    request: Request,
    token: str = Form(...),
    pin: str = Form(...),
):
    """Этап 2: подтверждение ПИНом → запуск рассылки в фоне."""
    _require_auth(request)
    user = request.session.get("user", "?")

    ok, msg, broadcast_id = await broadcast_manager.confirm_and_start(token, pin)
    log.warning("BROADCAST_START user=%s ok=%s msg=%s id=%s",
                user, ok, msg, broadcast_id)

    if not ok:
        return await _news_render(request, error=msg, pending_token=token)

    # Фоновая корутина для логирования завершения
    import asyncio as _asyncio
    async def _log_finish(bid: str):
        for _ in range(7200):  # макс 1 час ожидания
            await _asyncio.sleep(0.5)
            p = broadcast_manager.get_progress(bid)
            if not p:
                return
            if p["status"] in ("done", "error"):
                log.warning(
                    "BROADCAST_FINISH id=%s status=%s sent=%d blocked=%d failed=%d total=%d error=%s",
                    bid, p["status"], p["sent"], p["blocked"], p["failed"],
                    p["total"], p.get("error"),
                )
                return
    _asyncio.create_task(_log_finish(broadcast_id))

    return RedirectResponse(f"/news?broadcast={broadcast_id}", status_code=303)


@app.get("/news/progress/{broadcast_id}")
async def news_progress(request: Request, broadcast_id: str):
    """JSON-эндпоинт для опроса прогресса рассылки."""
    _require_auth(request)
    progress = broadcast_manager.get_progress(broadcast_id)
    if not progress:
        return {"error": "not found"}
    return {
        "sent": progress["sent"],
        "failed": progress["failed"],
        "blocked": progress["blocked"],
        "total": progress["total"],
        "status": progress["status"],
        "error": progress.get("error"),
    }


@app.post("/news/cancel", response_class=HTMLResponse)
async def news_cancel(request: Request, token: str = Form(...)):
    """Отменить отложенную рассылку (до ввода ПИНа)."""
    _require_auth(request)
    broadcast_manager.cancel(token)
    return RedirectResponse("/news", status_code=303)


async def _news_render(
    request: Request, *,
    error: str | None = None,
    pending_token: str | None = None,
):
    """Хелпер: рендер news.html с полным контекстом."""
    recipients_count = await broadcast_manager.count_recipients()
    cooldown = broadcast_manager.cooldown_left()
    pending = broadcast_manager.get_pending(pending_token) if pending_token else None

    return templates.TemplateResponse(
        "news.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "recipients_count": recipients_count,
            "cooldown_seconds": cooldown,
            "templates_": {
                "tech_start": broadcast_manager.get_template("tech_start"),
                "tech_end": broadcast_manager.get_template("tech_end"),
            },
            "pending_token": pending_token,
            "pending": pending,
            "broadcast_id": None,
            "error": error,
        },
        status_code=400 if error else 200,
    )


# ============================================================
#  РЕДАКТОР БОТА (хаб для Тексты + Кнопки + Авто-ответы)
# ============================================================

@app.get("/editor", response_class=HTMLResponse)
async def editor_hub(request: Request):
    _require_auth(request)
    return templates.TemplateResponse(
        "editor.html",
        {"request": request, "user": request.session.get("user")},
    )


# ============================================================
#  ВИДЖЕТ — установка, настройки, статистика
# ============================================================

def _detect_url_from_request(request: Request) -> str:
    """Определяет URL по заголовкам запроса (фолбэк)."""
    proto = request.headers.get("X-Forwarded-Proto", "https")
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", "")
    if not host:
        return ""
    return f"{proto}://{host}"


async def _get_public_url(request: Request) -> str:
    """
    Возвращает публичный URL админки. Сначала смотрит в настройках виджета,
    если там пусто — определяет из заголовков запроса.
    """
    from app import widget_settings as _ws
    settings = await _ws.get_settings()
    saved_url = (settings.get("public_url") or "").strip().rstrip("/")
    if saved_url:
        return saved_url
    return _detect_url_from_request(request)


def _widget_url(request: Request, public_url: str = "") -> str:
    """Полный URL widget.js."""
    base = public_url or _detect_url_from_request(request) or "https://your-domain.com"
    return f"{base.rstrip('/')}/api/chat/widget.js"


@app.get("/widget", response_class=HTMLResponse)
async def widget_page(
    request: Request,
    tab: str = "install",
    saved: str | None = None,
):
    _require_auth(request)
    if tab not in ("install", "settings", "stats"):
        tab = "install"

    from app import widget_settings as _ws
    settings = await _ws.get_settings()

    # Определяем URL: из настроек, фолбэк — из заголовков
    public_url = (settings.get("public_url") or "").strip().rstrip("/")
    auto_detected = _detect_url_from_request(request)
    effective_url = public_url or auto_detected

    # Статистика
    stats_data = {"total_visitors": 0, "active_today": 0,
                  "total_messages": 0, "identified": 0}
    if tab == "stats":
        try:
            import aiosqlite
            from app.database import DB_PATH
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT COUNT(*) FROM web_visitors")
                stats_data["total_visitors"] = (await cur.fetchone())[0]
                cur = await db.execute(
                    "SELECT COUNT(*) FROM web_visitors "
                    "WHERE datetime(last_active_at) > datetime('now', '-1 day')"
                )
                stats_data["active_today"] = (await cur.fetchone())[0]
                cur = await db.execute("SELECT COUNT(*) FROM web_messages")
                stats_data["total_messages"] = (await cur.fetchone())[0]
                cur = await db.execute(
                    "SELECT COUNT(*) FROM web_visitors WHERE user_id IS NOT NULL"
                )
                stats_data["identified"] = (await cur.fetchone())[0]
        except Exception as e:
            log.exception("widget stats: %s", e)

    return templates.TemplateResponse(
        "widget.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "tab": tab,
            "settings": settings,
            "stats": stats_data,
            "widget_url": _widget_url(request, effective_url),
            "public_url_set": public_url,
            "auto_detected_url": auto_detected,
            "effective_url": effective_url,
            "saved": saved,
            "error": None,
        },
    )


@app.post("/widget/save", response_class=HTMLResponse)
async def widget_save(
    request: Request,
    public_url: str = Form(""),
    primary_color: str = Form(...),
    position: str = Form(...),
    brand_name: str = Form(...),
    operator_label: str = Form("Оператор"),
    welcome_text: str = Form(...),
    placeholder: str = Form(...),
    send_button_text: str = Form(...),
    offline_text: str = Form(""),
    button_size: str = Form("M"),
    button_shape: str = Form("round"),
    button_icon: str = Form("💬"),
    button_label: str = Form("VPN Support"),
    window_size: str = Form("M"),
    window_shape: str = Form("default"),
    bubble_style: str = Form("rounded"),
    auto_open_seconds: int = Form(0),
    sound_enabled: str | None = Form(None),
    uploads_enabled: str | None = Form(None),
    operator_avatar: str = Form(""),
    always_online: str | None = Form(None),
    work_hours_from: str = Form(...),
    work_hours_to: str = Form(...),
    allowed_origins: str = Form(""),
    # === v3.2: кастомизация темы ===
    window_opacity: float = Form(1.0),
    header_color: str = Form(""),
    header_gradient: str | None = Form(None),
    header_color_to: str = Form(""),
    window_bg_color: str = Form(""),
    window_bg_gradient: str | None = Form(None),
    window_bg_color_to: str = Form(""),
    msg_in_color: str = Form(""),
    msg_in_gradient: str | None = Form(None),
    msg_in_color_to: str = Form(""),
    msg_out_color: str = Form(""),
    msg_out_gradient: str | None = Form(None),
    msg_out_color_to: str = Form(""),
    input_bg_color: str = Form(""),
):
    _require_auth(request)
    from app import widget_settings as _ws

    # Парсим origins
    origins = [
        line.strip() for line in allowed_origins.splitlines()
        if line.strip()
    ]
    if not origins:
        origins = ["*"]

    # Размер и стили — белый список
    if button_size not in ("S", "M", "L"):
        button_size = "M"
    # [v3.5] Форма кнопки: circle, square, squircle, pill
    # (старые "round" не разрешали другие значения и сбрасывали выбор)
    if button_shape not in ("circle", "square", "squircle", "pill"):
        button_shape = "circle"
    if window_size not in ("S", "M", "L"):
        window_size = "M"
    # [v3.5] Форма окна чата: default, wide, rounded
    if window_shape not in ("default", "wide", "rounded"):
        window_shape = "default"
    if bubble_style not in ("rounded", "square", "soft", "bordered", "minimal"):
        bubble_style = "rounded"

    try:
        auto_open = max(0, min(300, int(auto_open_seconds)))
    except (TypeError, ValueError):
        auto_open = 0

    data = {
        "public_url": public_url.strip().rstrip("/"),
        "primary_color": primary_color.strip(),
        "position": position,
        "brand_name": brand_name.strip(),
        "operator_label": (operator_label or "Оператор").strip()[:60],
        "welcome_text": welcome_text,
        "placeholder": placeholder.strip(),
        "send_button_text": send_button_text.strip(),
        "offline_text": offline_text,
        "button_size": button_size,
        "button_shape": button_shape,
        "button_icon": (button_icon or "💬").strip()[:8],
        "button_label": (button_label or "VPN Support").strip()[:40],
        "window_size": window_size,
        "window_shape": window_shape,
        "bubble_style": bubble_style,
        "auto_open_seconds": auto_open,
        "sound_enabled": sound_enabled is not None,
        "uploads_enabled": uploads_enabled is not None,
        "operator_avatar": (operator_avatar or "").strip()[:300],
        "always_online": always_online is not None,
        "work_hours_from": work_hours_from,
        "work_hours_to": work_hours_to,
        "work_timezone": "Europe/Moscow",
        "allowed_origins": origins,
        "allow_identify": True,

        # v3.2 — кастомизация темы
        "window_opacity": window_opacity,
        "header_color": header_color.strip(),
        "header_gradient": header_gradient is not None,
        "header_color_to": header_color_to.strip(),
        "window_bg_color": window_bg_color.strip(),
        "window_bg_gradient": window_bg_gradient is not None,
        "window_bg_color_to": window_bg_color_to.strip(),
        "msg_in_color": msg_in_color.strip(),
        "msg_in_gradient": msg_in_gradient is not None,
        "msg_in_color_to": msg_in_color_to.strip(),
        "msg_out_color": msg_out_color.strip(),
        "msg_out_gradient": msg_out_gradient is not None,
        "msg_out_color_to": msg_out_color_to.strip(),
        "input_bg_color": input_bg_color.strip(),
    }

    ok, msg = await _ws.save_settings(data)
    log.warning("WIDGET_SAVE ok=%s msg=%s", ok, msg)

    if ok:
        return RedirectResponse("/widget?tab=settings&saved=1", status_code=303)

    settings = await _ws.get_settings()
    auto_detected = _detect_url_from_request(request)
    effective_url = public_url.strip().rstrip("/") or auto_detected
    return templates.TemplateResponse(
        "widget.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "tab": "settings",
            "settings": settings,
            "stats": {},
            "widget_url": _widget_url(request, effective_url),
            "public_url_set": public_url.strip().rstrip("/"),
            "auto_detected_url": auto_detected,
            "effective_url": effective_url,
            "saved": None,
            "error": msg,
        },
        status_code=400,
    )


# ============================================================
#  ВЕБ-ЧАТЫ
# ============================================================

@app.get("/webchats", response_class=HTMLResponse)
async def webchats_list(request: Request):
    _require_auth(request)
    try:
        from app import web_chat_db
        visitors = await web_chat_db.list_visitors(limit=200)
    except Exception as e:
        log.exception("webchats list: %s", e)
        visitors = []

    return templates.TemplateResponse(
        "webchats.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "visitors": visitors,
        },
    )


@app.get("/webchats/{visitor_id}", response_class=HTMLResponse)
async def webchat_view(request: Request, visitor_id: str):
    _require_auth(request)
    from app import web_chat_db

    visitor = await web_chat_db.get_visitor(visitor_id)
    if not visitor:
        raise HTTPException(status_code=404, detail="Чат не найден")

    messages = await web_chat_db.get_all_messages(visitor_id, limit=500)

    # Список авто-ответов для дропдауна
    try:
        qa_list = qa_manager.list_answers()
    except Exception as e:
        log.warning("webchat_view: qa list failed: %s", e)
        qa_list = []

    # Инфа о подписке — если у visitor известен user_id
    subscription_html = ""
    if visitor.get("user_id"):
        try:
            from app import admin_panel
            subscription_html = await asyncio.wait_for(
                admin_panel.build_ticket_info_block(visitor["user_id"]),
                timeout=6.0,
            )
        except asyncio.TimeoutError:
            subscription_html = (
                "<i>⏱ Админка отвечает медленно — попробуй обновить страницу</i>"
            )
        except Exception as e:
            log.warning("webchat_view: subscription failed: %s", e)
            subscription_html = ""

    return templates.TemplateResponse(
        "webchat.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "visitor": visitor,
            "messages": messages,
            "qa_list": qa_list,
            "subscription_html": subscription_html,
        },
    )


@app.post("/webchats/{visitor_id}/reply", response_class=HTMLResponse)
async def webchat_reply(
    request: Request, visitor_id: str,
    text: str = Form(""),
    qa_key: str = Form(""),
    photo: UploadFile = File(None),
):
    """
    Ответ оператора в веб-чат.
    Принимает текст и/или фото и/или авто-ответ (qa_key).
    """
    _require_auth(request)

    # Авто-ответ — добавляем перед текстом
    qa_text = ""
    qa_photos: list[str] = []
    if qa_key:
        try:
            qa = qa_manager.get_answer(qa_key)
            if qa and qa.get("text"):
                qa_text = qa["text"]
            if qa and qa.get("photos"):
                qa_photos = list(qa["photos"])
        except Exception as e:
            log.warning("webchat_reply: qa lookup failed: %s", e)

    final_text = (text or "").strip()
    if qa_text:
        if final_text:
            final_text = qa_text + "\n\n" + final_text
        else:
            final_text = qa_text
    if len(final_text) > 2000:
        final_text = final_text[:2000]

    has_photo = bool(photo and getattr(photo, "filename", ""))
    has_qa_photo = bool(qa_photos) and not has_photo
    if not final_text and not has_photo and not has_qa_photo:
        return RedirectResponse(f"/webchats/{visitor_id}", status_code=303)

    user = request.session.get("user", "Оператор")

    from app import web_chat_db
    visitor = await web_chat_db.get_visitor(visitor_id)
    if not visitor:
        raise HTTPException(status_code=404, detail="Чат не найден")

    # 1) Сохраняем фото на диск в директорию визитёра.
    # photo_messages = список (url, bytes, filename) для:
    # - записи как отдельных сообщений в БД (виджет покажет)
    # - дублирования в TG-топик
    photo_messages: list[tuple[str, bytes, str]] = []
    if has_photo:
        try:
            import secrets
            from pathlib import Path
            uploads_dir = Path("/app/data/web_uploads") / visitor_id
            uploads_dir.mkdir(parents=True, exist_ok=True)
            ext = "jpg"
            if "." in photo.filename:
                ext = photo.filename.rsplit(".", 1)[-1].lower()[:5] or "jpg"
            fname = f"{secrets.token_hex(8)}.{ext}"
            fpath = uploads_dir / fname
            single_bytes = await photo.read()
            with open(fpath, "wb") as f:
                f.write(single_bytes)
            photo_messages.append((
                f"/api/chat/file/{visitor_id}/{fname}",
                single_bytes,
                photo.filename or "photo.jpg",
            ))
            log.info("WEBCHAT_REPLY photo saved: %s (%d bytes)",
                     fpath, len(single_bytes))
        except Exception as e:
            log.exception("webchat_reply: photo save failed: %s", e)
    elif has_qa_photo and qa_photos:
        # Копируем все фото из QA в директорию визитёра
        try:
            import secrets
            import shutil
            from pathlib import Path
            uploads_dir = Path("/app/data/web_uploads") / visitor_id
            uploads_dir.mkdir(parents=True, exist_ok=True)
            for qa_p in qa_photos:
                qa_full = qa_manager.get_photo_full_path(qa_p)
                if not qa_full or not qa_full.exists():
                    log.warning("webchat_reply: QA-фото %s не найдено", qa_p)
                    continue
                ext = qa_p.rsplit(".", 1)[-1].lower() if "." in qa_p else "jpg"
                fname = f"{secrets.token_hex(8)}.{ext}"
                fpath = uploads_dir / fname
                shutil.copyfile(str(qa_full), str(fpath))
                with open(fpath, "rb") as f:
                    photo_messages.append((
                        f"/api/chat/file/{visitor_id}/{fname}",
                        f.read(),
                        qa_p,
                    ))
            log.info("WEBCHAT_REPLY использую %d фото из QA %s",
                     len(photo_messages), qa_key)
        except Exception as e:
            log.exception("webchat_reply: qa photo copy failed: %s", e)

    # 2) Записываем сообщения в БД виджета.
    # Первое фото — с текстом-подписью (если есть).
    # Остальные фото — отдельными сообщениями без текста.
    # Если фото нет — текст одним сообщением.
    if photo_messages:
        first = True
        for purl, _pbytes, _pname in photo_messages:
            await web_chat_db.add_message(
                visitor_id, "out",
                final_text if first else "",
                sender=user,
                attachment_url=purl,
                attachment_kind="photo",
            )
            first = False
    elif final_text:
        await web_chat_db.add_message(visitor_id, "out", final_text, sender=user)

    # AI: помечаем что оператор подключился к этому визитёру.
    # После этого AI больше не отвечает в этом чате.
    try:
        await web_chat_db.mark_operator_joined(visitor_id)
    except Exception as e:
        log.warning("webchat_reply mark_operator_joined failed: %s", e)

    log.info("WEBCHAT_REPLY %s by %s (photos=%d qa=%s): %.40r",
             visitor_id, user, len(photo_messages), qa_key or "—", final_text)

    # 3) Дублируем в Telegram-топик
    if visitor.get("topic_id"):
        try:
            import aiohttp
            bot_token = os.getenv("BOT_TOKEN", "").strip()
            support_chat = int(os.getenv("SUPPORT_CHAT_ID", "0"))
            if bot_token and support_chat:
                async with aiohttp.ClientSession() as session:
                    topic_caption = f"👨‍💼 <b>{user} (из админки):</b>"
                    if final_text and len(final_text) < 900:
                        topic_caption += "\n" + final_text

                    # Несколько фото → sendMediaGroup
                    if len(photo_messages) >= 2:
                        photo_items = [(b, n) for _u, b, n in photo_messages]
                        await _send_media_group(
                            session, bot_token,
                            str(support_chat), visitor["topic_id"],
                            photo_items, topic_caption,
                        )
                    # Одно фото → sendPhoto
                    elif len(photo_messages) == 1:
                        _u, p_bytes, p_name = photo_messages[0]
                        url_p = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
                        form = aiohttp.FormData()
                        form.add_field("chat_id", str(support_chat))
                        form.add_field("message_thread_id", str(visitor["topic_id"]))
                        form.add_field("caption", topic_caption)
                        form.add_field("parse_mode", "HTML")
                        form.add_field("photo", p_bytes,
                                       filename=p_name,
                                       content_type="application/octet-stream")
                        await session.post(url_p, data=form)
                    # Только текст
                    elif final_text:
                        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                        await session.post(url, json={
                            "chat_id": support_chat,
                            "message_thread_id": visitor["topic_id"],
                            "text": f"👨‍💼 <b>{user} (из админки):</b>\n{final_text}",
                            "parse_mode": "HTML",
                        })
        except Exception as e:
            log.warning("webchat_reply telegram dup failed: %s", e)

    return RedirectResponse(f"/webchats/{visitor_id}", status_code=303)


# ============================================================
#  ВНУТРЕННИЙ API АДМИНКИ — polling сообщений веб-чата
# ============================================================

@app.get("/api/internal/webchat/{visitor_id}/poll")
async def webchat_internal_poll(
    request: Request, visitor_id: str, since: int = 0,
):
    """
    Polling для страницы чата в админке. Возвращает новые сообщения
    с id > since.
    """
    _require_auth(request)
    from app import web_chat_db
    try:
        messages = await web_chat_db.get_messages_since(visitor_id, since)
        visitor = await web_chat_db.get_visitor(visitor_id)
        return {
            "ok": True,
            "messages": messages,
            "last_active": visitor.get("last_active_at") if visitor else None,
        }
    except Exception as e:
        log.exception("webchat_internal_poll: %s", e)
        return {"ok": False, "messages": []}


# ============================================================
#  Разовая чистка: отвязать всех visitor от заданного user_id
#  Используется когда в HTML кабинета был хардкод user_id и в БД
#  накопилось много visitor с этой битой привязкой.
# ============================================================

@app.post("/webchats/cleanup-user-id")
async def webchats_cleanup_user_id(request: Request):
    """
    Принимает form data: user_id=123
    Отвязывает всех visitor с этим user_id (история сохраняется).
    Используется когда обнаружен хардкод user_id в HTML сайта.
    """
    _require_auth(request)
    from app import web_chat_db
    form = await request.form()
    try:
        user_id = int(form.get("user_id", "0"))
    except (TypeError, ValueError):
        return RedirectResponse("/webchats?error=invalid_user_id", status_code=303)
    if user_id <= 0:
        return RedirectResponse("/webchats?error=invalid_user_id", status_code=303)

    try:
        await web_chat_db.detach_all_visitors_by_user_id(user_id)
        log.info("Admin cleanup: отвязал всех visitor с user_id=%s", user_id)
        return RedirectResponse(
            f"/webchats?cleaned_user_id={user_id}", status_code=303,
        )
    except Exception as e:
        log.exception("cleanup-user-id failed: %s", e)
        return RedirectResponse(
            f"/webchats?error={str(e)[:80]}", status_code=303,
        )



# ============================================================
#  [v3.5] БЕЗОПАСНОСТЬ + ЛИМИТЫ + SLA
# ============================================================

@app.get("/limits", response_class=HTMLResponse)
async def limits_page(request: Request):
    """Страница настроек: rate-limit, SLA, защита от injection."""
    _require_auth(request)
    from app import security
    try:
        await security.init_db()
    except Exception as e:
        log.warning("limits_page: init_db failed: %s", e)
    settings = await security.get_all_settings()
    return templates.TemplateResponse(
        "limits.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "brand_short": BRAND_SHORT,
            "settings": settings,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/limits/save", response_class=HTMLResponse)
async def limits_save(request: Request):
    """Сохранение настроек безопасности + лимитов."""
    _require_auth(request)
    from app import security
    form = await request.form()

    # Валидация и сохранение
    fields = {
        "injection_protection_enabled": ("bool", None),
        "rl_messages_per_minute": ("int", (0, 1000)),
        "rl_messages_per_hour": ("int", (0, 100000)),
        "rl_uploads_per_minute": ("int", (0, 1000)),
        "sla_yellow_minutes": ("int", (1, 1440)),
        "sla_red_minutes": ("int", (1, 1440)),
    }

    errors = []
    for key, (kind, bounds) in fields.items():
        raw = form.get(key)
        if kind == "bool":
            value = "1" if raw in ("on", "1", "true", "True") else "0"
        elif kind == "int":
            try:
                ival = int(raw or "0")
            except (TypeError, ValueError):
                errors.append(f"{key}: не число")
                continue
            if bounds:
                lo, hi = bounds
                if not (lo <= ival <= hi):
                    errors.append(f"{key}: вне диапазона {lo}-{hi}")
                    continue
            value = str(ival)
        else:
            value = str(raw or "")

        try:
            await security.set_setting(key, value)
        except Exception as e:
            errors.append(f"{key}: {e}")

    if errors:
        from urllib.parse import quote
        return RedirectResponse(
            f"/limits?error={quote('; '.join(errors))}", status_code=303,
        )
    log.info("LIMITS_SAVED user=%s", request.session.get("user"))
    return RedirectResponse(
        "/limits?message=Настройки сохранены", status_code=303,
    )


# ============================================================
#  [v3.5] THE TOCHKA — заглушка раздела в разработке
# ============================================================

@app.get("/the-tochka", response_class=HTMLResponse)
async def the_tochka_page(request: Request):
    """Заглушка раздела The Tochka — функция в разработке."""
    _require_auth(request)
    return templates.TemplateResponse(
        "the_tochka.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "brand_short": BRAND_SHORT,
        },
    )


# ============================================================
#  [v3.5] МАССОВОЕ ЗАКРЫТИЕ ВСЕХ ОТКРЫТЫХ ТИКЕТОВ
# ============================================================

@app.post("/tickets/close-all", response_class=HTMLResponse)
async def tickets_close_all(
    request: Request,
    silent: str = Form(""),
):
    """
    [v3.5] Закрывает ВСЕ открытые тикеты:
      - TG-тикеты (tickets.status='open')
      - Веб-чаты (web_visitors.topic_id IS NOT NULL)

    Параметр silent=1 — без уведомлений клиентам (только пометка в группе).
    После завершения — редирект обратно на /tickets с counter.
    """
    _require_auth(request)
    is_silent = silent == "1"
    user = request.session.get("user", "Оператор")

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    support_chat = int(os.getenv("SUPPORT_CHAT_ID", "0"))

    closed_tg = 0
    closed_web = 0
    errors: list[str] = []

    import aiosqlite as _aiosqlite
    import aiohttp

    # ============================================================
    #  ШАГ 1: собираем список открытых TG-тикетов
    # ============================================================
    tg_tickets: list[dict] = []
    try:
        async with _aiosqlite.connect(DB_PATH) as db:
            db.row_factory = _aiosqlite.Row
            cur = await db.execute(
                "SELECT id, user_id, topic_id FROM tickets WHERE status='open'"
            )
            tg_tickets = [dict(r) for r in await cur.fetchall()]
    except Exception as e:
        errors.append(f"TG ticket list: {e}")
        log.exception("close_all: list TG tickets failed")

    # ============================================================
    #  ШАГ 2: собираем список открытых веб-чатов
    # ============================================================
    web_visitors: list[dict] = []
    try:
        async with _aiosqlite.connect(DB_PATH) as db:
            db.row_factory = _aiosqlite.Row
            # Проверяем что таблица web_visitors существует
            cur = await db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='web_visitors'"
            )
            if await cur.fetchone():
                # [v3.5] Открытые = со status='open' (эскалация была).
                # Fallback на topic_id если колонки status ещё нет.
                try:
                    cur = await db.execute(
                        "SELECT visitor_id, user_id, topic_id FROM web_visitors "
                        "WHERE COALESCE(status, 'closed') = 'open'"
                    )
                    web_visitors = [dict(r) for r in await cur.fetchall()]
                except Exception:
                    cur = await db.execute(
                        "SELECT visitor_id, user_id, topic_id FROM web_visitors "
                        "WHERE topic_id IS NOT NULL"
                    )
                    web_visitors = [dict(r) for r in await cur.fetchall()]
    except Exception as e:
        errors.append(f"Web visitors list: {e}")
        log.exception("close_all: list web visitors failed")

    log.info(
        "CLOSE_ALL by %s: %d TG, %d web (silent=%s)",
        user, len(tg_tickets), len(web_visitors), is_silent,
    )

    # ============================================================
    #  ШАГ 3: одной транзакцией обновляем все статусы
    # ============================================================
    try:
        async with _aiosqlite.connect(DB_PATH) as db:
            # TG-тикеты — status='closed'
            if tg_tickets:
                await db.execute(
                    "UPDATE tickets SET status='closed' WHERE status='open'"
                )
                closed_tg = len(tg_tickets)
            # [v3.5] Веб-чаты — status='closed', topic_id ОСТАЁТСЯ для истории
            if web_visitors:
                try:
                    await db.execute(
                        "UPDATE web_visitors SET status='closed' "
                        "WHERE COALESCE(status, 'closed') = 'open'"
                    )
                except Exception:
                    # Старая схема — fallback
                    await db.execute(
                        "UPDATE web_visitors SET last_topic_id = topic_id, "
                        "topic_id = NULL WHERE topic_id IS NOT NULL"
                    )
                closed_web = len(web_visitors)
            await db.commit()
    except Exception as e:
        errors.append(f"DB update: {e}")
        log.exception("close_all: db update failed")

    # ============================================================
    #  ШАГ 4: чистим AI-историю всех клиентов (TG + web)
    # ============================================================
    try:
        from app import ai_assistant
        cleared = 0
        for t in tg_tickets:
            uid = t.get("user_id")
            if uid:
                try:
                    await ai_assistant.clear_history(str(uid))
                    cleared += 1
                except Exception:
                    pass
        for v in web_visitors:
            vid = v.get("visitor_id")
            if vid:
                try:
                    await ai_assistant.clear_history(str(vid))
                    cleared += 1
                except Exception:
                    pass
        log.info("close_all: AI history cleared for %d clients", cleared)
    except Exception as e:
        errors.append(f"AI cleanup: {e}")

    # ============================================================
    #  ШАГ 5: уведомления в TG-группу и клиентам
    # ============================================================
    if bot_token and support_chat:
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                send_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                close_topic_url = (
                    f"https://api.telegram.org/bot{bot_token}/closeForumTopic"
                )
                silent_mark = " 🤫" if is_silent else ""

                # Обходим все TG-тикеты
                for t in tg_tickets:
                    topic_id = t.get("topic_id")
                    user_id = t.get("user_id")
                    # 1) Помечаем топик иконкой «закрыт» — без sendMessage.
                    # [v3.5] Сообщение в топик не шлём — это закрытие из админки.
                    if topic_id:
                        try:
                            await _set_topic_icon_via_api(
                                session, bot_token, support_chat,
                                topic_id, "closed",
                            )
                        except Exception:
                            pass
                    # 2) Клиенту — если не silent
                    if user_id and not is_silent:
                        try:
                            await session.post(send_url, json={
                                "chat_id": user_id,
                                "text": "🔒 <b>Ваш тикет закрыт</b>\n\n"
                                        "Если проблема появится снова — "
                                        "создайте новый.",
                                "parse_mode": "HTML",
                            })
                        except Exception:
                            pass

                # Обходим все веб-чаты
                for v in web_visitors:
                    topic_id = v.get("topic_id")
                    if topic_id:
                        try:
                            # [v3.5] Без sendMessage — только иконка + close
                            try:
                                await _set_topic_icon_via_api(
                                    session, bot_token, support_chat,
                                    topic_id, "closed",
                                )
                            except Exception:
                                pass
                            try:
                                await session.post(close_topic_url, json={
                                    "chat_id": support_chat,
                                    "message_thread_id": topic_id,
                                })
                            except Exception:
                                pass
                        except Exception:
                            pass
        except Exception as e:
            errors.append(f"TG notifications: {e}")
            log.warning("close_all: TG notify failed: %s", e)

    # ============================================================
    #  ШАГ 6: уведомление веб-клиентов в виджете
    # ============================================================
    try:
        from app import web_chat_db
        for v in web_visitors:
            vid = v.get("visitor_id")
            if not vid:
                continue
            try:
                # Сообщение от системы в виджет
                if not is_silent:
                    await web_chat_db.add_message(
                        vid, "out",
                        "🔒 Чат закрыт оператором. Если проблема появится "
                        "снова — напишите новое сообщение.",
                        sender="Система",
                    )
                # Сбрасываем флаг operator_joined чтобы AI снова включился
                try:
                    await web_chat_db.reset_operator_joined(vid)
                except (AttributeError, Exception):
                    pass
            except Exception:
                pass
    except Exception as e:
        errors.append(f"Web notifications: {e}")
        log.warning("close_all: web notify failed: %s", e)

    # ============================================================
    #  ШАГ 7: редирект на /tickets с результатом
    # ============================================================
    from urllib.parse import quote
    total = closed_tg + closed_web
    if errors:
        msg = (
            f"Закрыто: {closed_tg} TG, {closed_web} web. "
            f"С ошибками: {'; '.join(errors[:3])}"
        )
        return RedirectResponse(
            f"/tickets?error={quote(msg)}", status_code=303,
        )
    msg = (
        f"Закрыто {total} обращений ({closed_tg} TG, {closed_web} веб-чатов)"
    )
    return RedirectResponse(
        f"/tickets?message={quote(msg)}", status_code=303,
    )


# ============================================================
#  [v3.5] DEBUG: диагностика статусов веб-тикетов
# ============================================================

@app.get("/debug/web-status")
async def debug_web_status(request: Request):
    """Возвращает JSON с фактическим состоянием БД и админки —
    для отладки расхождений 'в БД closed, а админка показывает open'.
    """
    _require_auth(request)
    import aiosqlite as _aio
    out = {
        "db_path": DB_PATH,
        "sqlite_version": None,
        "columns_web_visitors": [],
        "has_status_column": False,
        "last_5_visitors": [],
        "admin_view_says": [],
    }
    try:
        import sqlite3 as _sq
        out["sqlite_version"] = _sq.sqlite_version
    except Exception:
        pass

    # 1) Что в БД
    try:
        async with _aio.connect(DB_PATH) as db:
            db.row_factory = _aio.Row
            cur = await db.execute("PRAGMA table_info(web_visitors)")
            rows = await cur.fetchall()
            out["columns_web_visitors"] = [r[1] for r in rows]
            out["has_status_column"] = "status" in out["columns_web_visitors"]

            # Последние 5 visitor'ов — всё что есть
            cur = await db.execute(
                "SELECT visitor_id, "
                "(SELECT 'yes' FROM pragma_table_info('web_visitors') "
                " WHERE name='status' LIMIT 1) AS has_st, "
                "topic_id, operator_joined, last_active_at "
                "FROM web_visitors ORDER BY rowid DESC LIMIT 5"
            )
            rows = await cur.fetchall()
            # Безопасный доступ к status через try
            for r in rows:
                vid = r["visitor_id"]
                try:
                    cur2 = await db.execute(
                        "SELECT status FROM web_visitors WHERE visitor_id=?",
                        (vid,),
                    )
                    st = await cur2.fetchone()
                    db_status = st[0] if st else None
                except Exception:
                    db_status = "(NO COLUMN)"
                out["last_5_visitors"].append({
                    "visitor_id": vid,
                    "db_status": db_status,
                    "topic_id": r["topic_id"],
                    "operator_joined": r["operator_joined"],
                    "last_active_at": r["last_active_at"],
                })
    except Exception as e:
        out["db_error"] = str(e)

    # 2) Что админка возвращает через tickets_view
    try:
        from admin_web import tickets_view
        result = await tickets_view.list_tickets(
            source="web", limit=5, offset=0,
        )
        for t in result["tickets"]:
            out["admin_view_says"].append({
                "visitor_id": t["id"],
                "status_from_admin": t["status"],
                "topic_id": t.get("topic_id"),
            })
    except Exception as e:
        out["admin_view_error"] = str(e)

    return out