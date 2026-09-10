"""Поиск клиентов в их админке не должен падать на нецифровом вводе.

Строка поиска отвечала «Internal Server Error» на любое слово: запрос брал
`email` и `real_username` напрямую, а их добавляют миграции. Поиск по
Telegram ID при этом работал — он этих колонок не касается, поэтому промах
и дожил до боевого сервера.

Функция чистая, поэтому берётся из исходника и выполняется отдельно:
импортировать модуль целиком нельзя — он тянет `config`, а тот прогоняет
миграцию боевой базы.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1] / "bot" / "web_admin" / "routes" / "users.py"
).read_text(encoding="utf-8")


def _build():
    body = re.search(r"^def _users_search_query\(.*?\n(?=\S)", SOURCE, re.S | re.M)
    assert body, "функция поиска не найдена — тест потерял цель"
    namespace: dict = {}
    exec(compile(body.group(0), "users.py", "exec"), namespace)  # noqa: S102
    return namespace["_users_search_query"]


FULL = """
CREATE TABLE users (
    telegram_id INTEGER PRIMARY KEY, username TEXT, real_username TEXT, email TEXT,
    xui_client_uuid TEXT, xui_client_email TEXT, subscription_end_date TEXT,
    is_trial_used INTEGER, current_server_id INTEGER, limit_ip INTEGER,
    is_blocked INTEGER, user_tag TEXT, created_at TEXT, registration_type TEXT
)
"""

# База, где миграции `email` и `real_username` не прошли.
BARE = """
CREATE TABLE users (
    telegram_id INTEGER PRIMARY KEY, username TEXT,
    xui_client_uuid TEXT, xui_client_email TEXT, subscription_end_date TEXT,
    is_trial_used INTEGER, current_server_id INTEGER, limit_ip INTEGER,
    is_blocked INTEGER, user_tag TEXT, created_at TEXT, registration_type TEXT
)
"""


def _db(schema: str) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(schema)
    db.execute(
        "INSERT INTO users (telegram_id, username, created_at) VALUES (1, 'dsfghj', '2026-09-01')"
    )
    db.commit()
    return db


def _columns(db: sqlite3.Connection) -> list[str]:
    return [row[1] for row in db.execute("PRAGMA table_info(users)")]


def _run(db: sqlite3.Connection, built) -> list[sqlite3.Row]:
    select_cols, where, params, order = built
    # Подставляются только имена колонок, которые собрал сам обработчик;
    # искомое значение уходит параметром — как и в бою.
    sql = f"SELECT {select_cols} FROM users {where} {order} LIMIT ? OFFSET ?"  # noqa: S608
    return db.execute(sql, ("2026-09-01T00:00:00+00:00", *params, 15, 0)).fetchall()


@pytest.mark.parametrize("schema", [FULL, BARE], ids=["полная схема", "без миграций"])
def test_search_by_name_works_on_any_schema(schema):
    db = _db(schema)
    built = _build()(
        _columns(db), search_email=None, search_username="dsf", rw_plain=""
    )
    rows = _run(db, built)

    assert len(rows) == 1
    assert rows[0]["telegram_id"] == 1


@pytest.mark.parametrize("schema", [FULL, BARE], ids=["полная схема", "без миграций"])
def test_count_uses_the_same_parameters(schema):
    """Счётчик и выборка ходят с одним набором параметров.

    Раньше они собирались порознь, и разойдись они — страница показала бы
    одно число, а вывела другое.
    """
    db = _db(schema)
    _select, where, params, _order = _build()(
        _columns(db), search_email=None, search_username="dsf", rw_plain=""
    )
    total = db.execute(
        f"SELECT COUNT(*) as cnt FROM users {where}", params  # noqa: S608
    ).fetchone()["cnt"]

    assert total == 1


def test_email_search_without_the_column_finds_nothing_instead_of_falling():
    db = _db(BARE)

    assert _build()(_columns(db), search_email="a@b.c", search_username=None, rw_plain="") is None


def test_email_search_works_where_the_column_exists():
    db = _db(FULL)
    db.execute("UPDATE users SET email = 'a@b.c' WHERE telegram_id = 1")
    db.commit()

    rows = _run(db, _build()(_columns(db), search_email="A@B.C", search_username=None, rw_plain=""))

    assert len(rows) == 1


def test_traffic_columns_are_appended_when_present():
    """Колонки трафика подставляет вызывающий; выборка обязана их донести."""
    db = _db(FULL)
    db.execute("ALTER TABLE users ADD COLUMN total_bytes INTEGER DEFAULT 0")
    db.commit()

    rows = _run(
        db,
        _build()(
            _columns(db),
            search_email=None,
            search_username="dsf",
            rw_plain=", COALESCE(total_bytes, 0) AS total_bytes",
        ),
    )

    assert rows[0]["total_bytes"] == 0
