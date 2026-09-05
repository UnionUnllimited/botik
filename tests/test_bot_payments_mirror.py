"""Зеркало платежей магазина в таблице бота.

Проверяется на настоящем SQLite с их схемой и включёнными внешними ключами:
именно они и делают зеркало хрупким — платёж клиента, которого нет в `users`,
роняет вставку, а с ним и всю пачку. И курсор: смена статуса «ожидает →
оплачен» обязана доехать, а история при первом круге — не разбудить
push-рассылку админке.

Модуль загружается с подменёнными `db_helpers` и `shop_api`: настоящий
`db_helpers` при импорте тянет `config`, а тот прогоняет миграцию боевой
базы — в тест такое не берут.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import pytest

SHOP_SYNC = Path(__file__).resolve().parents[1] / "bot" / "src" / "shop_sync.py"

SCHEMA = """
CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, username TEXT);
CREATE TABLE payments (
    payment_id TEXT PRIMARY KEY, telegram_id INTEGER, amount REAL, currency TEXT,
    status TEXT DEFAULT 'pending', created_at TEXT, metadata_json TEXT,
    pwa_notified INTEGER DEFAULT 0,
    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, description TEXT);
INSERT INTO users VALUES (8152081864, 'kelvin');
"""


def _row(payment_id, tg_id, status="succeeded", amount="122.99"):
    return {
        "payment_id": payment_id,
        "tg_id": tg_id,
        "amount": amount,
        "currency": "RUB",
        "status": status,
        "created_at": "2026-08-30T20:37:00+00:00",
        "metadata": {"payment_type": "router_order", "order_number": "R-260830-0012"},
    }


class FakeShop:
    """Снимок платежей постранично, как отдаёт основное приложение."""

    def __init__(self, pages: list[list[dict]], limit: int = 500, error: str = ""):
        self.pages = pages
        self.limit = limit
        self.error = error
        self.calls: list[str] = []

    async def payments_snapshot(self, since: str = "", limit: int = 500):
        self.calls.append(since)
        if self.error:
            return {}, self.error
        page = self.pages.pop(0) if self.pages else []
        return {
            "payments": page,
            "limit": self.limit,
            "next_since": f"cursor-{len(self.calls)}" if page else since,
        }, ""


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    """Модуль зеркала на временной базе с их схемой."""
    db_file = tmp_path / "bot.sqlite3"

    @asynccontextmanager
    async def connection():
        conn = await aiosqlite.connect(db_file)
        try:
            await conn.execute("PRAGMA foreign_keys=ON;")
            yield conn
        finally:
            await conn.close()

    async def get_setting_by_key(key: str, default: str = "") -> str:
        async with connection() as db:
            async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
                row = await cur.fetchone()
                return str(row[0]) if row and row[0] is not None else default

    fake_db = types.ModuleType("db_helpers")
    fake_db.get_db_connection_safe = connection
    fake_db.get_setting_by_key = get_setting_by_key

    shop = FakeShop(pages=[])
    fake_src = types.ModuleType("src")
    fake_src.shop_api = shop

    # Журнал бота — его зависимость, в нашем окружении её нет; пишет он
    # в никуда, проверяем не его.
    fake_loguru = types.ModuleType("loguru")
    fake_loguru.logger = types.SimpleNamespace(
        info=lambda *a, **k: None, debug=lambda *a, **k: None,
        warning=lambda *a, **k: None, error=lambda *a, **k: None,
    )

    monkeypatch.setitem(sys.modules, "db_helpers", fake_db)
    monkeypatch.setitem(sys.modules, "src", fake_src)
    monkeypatch.setitem(sys.modules, "loguru", fake_loguru)

    spec = importlib.util.spec_from_file_location("shop_sync_under_test", SHOP_SYNC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def prepare():
        async with aiosqlite.connect(db_file) as db:
            await db.executescript(SCHEMA)

    async def rows(sql, args=()):
        async with aiosqlite.connect(db_file) as db:
            async with db.execute(sql, args) as cur:
                return await cur.fetchall()

    return types.SimpleNamespace(module=module, shop=shop, prepare=prepare, rows=rows)


@pytest.mark.asyncio
async def test_first_run_brings_history_quietly(mirror):
    """Первый круг переносит всё, но не будит push-рассылку.

    Иначе админке прилетели бы уведомления обо всех старых оплатах разом —
    и оператор отключил бы рассылку, а с ней и уведомления о новых.
    """
    await mirror.prepare()
    mirror.shop.pages = [[_row("SHOP_1", 8152081864)]]

    written = await mirror.module.sync_payments()

    assert written == 1
    stored = await mirror.rows("SELECT payment_id, telegram_id, amount, status, pwa_notified FROM payments")
    assert stored == [("SHOP_1", 8152081864, 122.99, "succeeded", 1)]
    cursor = await mirror.rows("SELECT value FROM settings WHERE key = ?",
                               (mirror.module.PAYMENTS_CURSOR_KEY,))
    assert cursor == [("cursor-1",)]


@pytest.mark.asyncio
async def test_client_unknown_to_the_bot_is_skipped_not_fatal(mirror):
    """Внешние ключи у них включены: чужой telegram_id уронил бы вставку.

    Пропускаем строку, а не пачку: остальные платежи в ней ни при чём.
    """
    await mirror.prepare()
    mirror.shop.pages = [[_row("SHOP_1", 999), _row("SHOP_2", 8152081864)]]

    written = await mirror.module.sync_payments()

    assert written == 1
    stored = await mirror.rows("SELECT payment_id FROM payments")
    assert stored == [("SHOP_2",)]


@pytest.mark.asyncio
async def test_status_change_arrives_and_keeps_the_notified_flag(mirror):
    """Смена «ожидает → оплачен» доезжает, а флаг «оператору сообщено» не сбрасывается."""
    await mirror.prepare()
    mirror.shop.pages = [[_row("SHOP_1", 8152081864, status="pending")]]
    await mirror.module.sync_payments()

    mirror.shop.pages = [[_row("SHOP_1", 8152081864, status="succeeded"),
                          _row("SHOP_2", 8152081864)]]
    await mirror.module.sync_payments()

    stored = await mirror.rows("SELECT payment_id, status, pwa_notified FROM payments ORDER BY payment_id")
    # Старая строка: статус обновился, флаг первого круга остался.
    # Новая строка второго круга — свежая оплата, о ней оператору сообщат.
    assert stored == [("SHOP_1", "succeeded", 1), ("SHOP_2", "succeeded", 0)]
    assert mirror.shop.calls[1] == "cursor-1"


@pytest.mark.asyncio
async def test_pages_are_read_until_the_short_one(mirror):
    """Снимок отдаётся страницами; зеркало не должно останавливаться на первой."""
    await mirror.prepare()
    mirror.shop.limit = 2
    mirror.shop.pages = [
        [_row("SHOP_1", 8152081864), _row("SHOP_2", 8152081864)],
        [_row("SHOP_3", 8152081864)],
    ]

    written = await mirror.module.sync_payments()

    assert written == 3
    assert mirror.shop.calls == ["", "cursor-1"]


@pytest.mark.asyncio
async def test_unreachable_shop_changes_nothing(mirror):
    """Соседний сервис молчит — круг переживает это, курсор не двигается."""
    await mirror.prepare()
    mirror.shop.error = "connection refused"

    written = await mirror.module.sync_payments()

    assert written == 0
    assert await mirror.rows("SELECT 1 FROM settings") == []
