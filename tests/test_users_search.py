"""Поиск клиентов в их админке не должен падать ни на какой схеме.

Строка поиска отвечала «Internal Server Error» на любое слово: запрос брал
колонки напрямую, а схему этой базы наращивали миграциями годами — какие
из них прошли на конкретной установке, заранее не известно. Поиск по
Telegram ID при этом работал: он берёт меньше колонок, поэтому промах
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

NOW = "2026-09-10T00:00:00+00:00"


def _build():
    body = re.search(r"^def _users_search_query\(.*?\n(?=\S)", SOURCE, re.S | re.M)
    assert body, "функция поиска не найдена — тест потерял цель"
    namespace: dict = {}
    exec(compile(body.group(0), "users.py", "exec"), namespace)  # noqa: S102
    return namespace["_users_search_query"]


ALL_COLUMNS = (
    "username TEXT", "real_username TEXT", "email TEXT", "xui_client_uuid TEXT",
    "xui_client_email TEXT", "subscription_end_date TEXT", "is_trial_used INTEGER",
    "current_server_id INTEGER", "limit_ip INTEGER", "is_blocked INTEGER",
    "user_tag TEXT", "created_at TEXT", "registration_type TEXT",
)


def _db(*, without: tuple[str, ...] = ()) -> sqlite3.Connection:
    """База с их схемой, из которой убраны названные колонки."""
    kept = [c for c in ALL_COLUMNS if c.split()[0] not in without]
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(f"CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, {', '.join(kept)})")
    values = {"telegram_id": 1}
    if "username" not in without:
        values["username"] = "dsfghj"
    if "created_at" not in without:
        values["created_at"] = "2026-09-01"
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    db.execute(f"INSERT INTO users ({cols}) VALUES ({marks})", tuple(values.values()))  # noqa: S608
    db.commit()
    return db


def _columns(db: sqlite3.Connection) -> list[str]:
    return [row[1] for row in db.execute("PRAGMA table_info(users)")]


def _search(db: sqlite3.Connection, *, email=None, username=None, rw_plain=""):
    return _build()(
        _columns(db), search_email=email, search_username=username,
        rw_plain=rw_plain, now_utc_str=NOW,
    )


# Каждая необязательная колонка по очереди — и все сразу. Раньше выборка
# брала их на веру, и любой такой базы хватало, чтобы уронить страницу.
@pytest.mark.parametrize(
    "without",
    [
        (),
        ("email",),
        ("real_username",),
        ("email", "real_username"),
        ("registration_type",),
        ("user_tag",),
        ("xui_client_uuid",),
        ("is_blocked",),
        ("limit_ip",),
        ("created_at",),
        ("subscription_end_date",),
        tuple(c.split()[0] for c in ALL_COLUMNS if c.split()[0] != "username"),
    ],
    ids=lambda w: "без " + (", ".join(w) if w else "ничего"),
)
def test_search_by_name_survives_any_missing_column(without):
    db = _db(without=without)
    select_sql, head, count_sql, count_params = _search(db, username="dsf")

    total = db.execute(count_sql, count_params).fetchone()["cnt"]
    rows = db.execute(select_sql, (*head, 15, 0)).fetchall()

    assert total == 1
    assert len(rows) == 1
    assert rows[0]["telegram_id"] == 1
    # Признак активной подписки страница читает всегда — даже там, где
    # колонки срока нет и считать его не из чего.
    assert "is_active" in dict(rows[0])


def test_email_search_uses_the_email_column():
    db = _db()
    db.execute("UPDATE users SET email = 'a@b.c' WHERE telegram_id = 1")
    db.commit()

    select_sql, head, _count_sql, _params = _search(db, email="A@B.C")
    rows = db.execute(select_sql, (*head, 15, 0)).fetchall()

    assert len(rows) == 1


def test_email_search_without_the_column_finds_nothing_instead_of_falling():
    assert _search(_db(without=("email",)), email="a@b.c") is None


def test_nothing_to_search_by_is_not_a_crash():
    """Ни имени, ни настоящего имени — искать не по чему, но и падать не за что."""
    assert _search(_db(without=("username", "real_username")), username="dsf") is None


def test_traffic_columns_are_appended_when_present():
    """Колонки трафика подставляет вызывающий; выборка обязана их донести."""
    db = _db()
    db.execute("ALTER TABLE users ADD COLUMN total_bytes INTEGER DEFAULT 0")
    db.commit()

    select_sql, head, _c, _p = _search(
        db, username="dsf", rw_plain=", COALESCE(total_bytes, 0) AS total_bytes"
    )
    rows = db.execute(select_sql, (*head, 15, 0)).fetchall()

    assert rows[0]["total_bytes"] == 0


def test_search_value_never_reaches_the_query_text():
    """Значение уходит параметром: в тексте запроса — только имена колонок."""
    db = _db()
    select_sql, head, count_sql, _p = _search(db, username="'; DROP TABLE users; --")

    assert "DROP TABLE" not in select_sql
    assert "DROP TABLE" not in count_sql
    assert any("DROP TABLE" in str(value) for value in head)
