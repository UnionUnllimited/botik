"""
Хранилище редактируемого контента в БД.

Тексты, кнопки и авто-ответы переехали из Python-файлов в SQLite,
чтобы переживать обновления кода через /service.

Схема:
  content_texts (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP)
  content_buttons (menu_name TEXT, position INTEGER,
                   text TEXT, kind TEXT, value TEXT,
                   PRIMARY KEY(menu_name, position))
  content_qa (key TEXT PRIMARY KEY, label TEXT, text TEXT, position INTEGER,
              updated_at TIMESTAMP)

При первом запуске бот заливает значения по умолчанию из texts.py/keyboards.py
(только те, которых ещё нет в БД).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import aiosqlite

from app.database import DB_PATH


# ============================================================
#  СХЕМА
# ============================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS content_texts (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS content_buttons (
    menu_name TEXT NOT NULL,
    position INTEGER NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,         -- 'callback' | 'url'
    value TEXT NOT NULL,        -- callback_data ИЛИ url
    hidden INTEGER DEFAULT 0,   -- [v3.5] 1 = скрыта от клиента (но не удалена)
    response_text TEXT,         -- [v3.5] для динамических simple-text кнопок
    submenu_name TEXT,          -- [v3.5] для динамических submenu кнопок
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (menu_name, position)
);

CREATE TABLE IF NOT EXISTS content_qa (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    text TEXT NOT NULL,
    position INTEGER NOT NULL,
    photo_path TEXT,
    hidden INTEGER DEFAULT 0,   -- [v3.5] 1 = ответ выключен в боте, но не удалён
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- "Надгробия" — отметки о том что админ намеренно удалил какой-то ключ
-- из БД через UI. Миграция (migrate_from_files) не восстанавливает то,
-- что лежит здесь. Иначе при каждом рестарте бота все удалённые меню/тексты
-- возвращались бы обратно из дефолтов keyboards.py / texts.py.
CREATE TABLE IF NOT EXISTS content_tombstones (
    kind TEXT NOT NULL,    -- 'text' | 'menu' | 'qa'
    key  TEXT NOT NULL,    -- имя ключа/меню
    deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (kind, key)
);

-- [v3.5] Скрытие кнопок ВИРТУАЛЬНЫХ меню (mykey_menu и пр.).
-- Эти меню формируются в коде бота, в content_buttons их нет.
-- Чтобы оператор мог скрыть отдельные кнопки динамической клавиатуры
-- через UI редактора, храним записи здесь. Бот при генерации виртуальной
-- клавиатуры проверяет эту таблицу и пропускает скрытые.
CREATE TABLE IF NOT EXISTS virtual_button_hidden (
    menu_name    TEXT NOT NULL,   -- имя виртуального меню (например 'mykey_menu')
    button_value TEXT NOT NULL,   -- value кнопки (например 'mk_reset')
    hidden_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (menu_name, button_value)
);

CREATE INDEX IF NOT EXISTS idx_content_buttons_menu
ON content_buttons(menu_name, position);

CREATE INDEX IF NOT EXISTS idx_content_qa_position
ON content_qa(position);
"""


async def init_content_db() -> None:
    """Создаёт таблицы. Безопасно вызывать многократно."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        # Миграция: добавить photo_path в content_qa если БД старая
        try:
            await db.execute(
                "ALTER TABLE content_qa ADD COLUMN photo_path TEXT"
            )
        except Exception:
            pass  # колонка уже есть
        # [v3.5] Миграция: добавить hidden в content_buttons и content_qa
        for table in ("content_buttons", "content_qa"):
            try:
                await db.execute(
                    f"ALTER TABLE {table} ADD COLUMN hidden INTEGER DEFAULT 0"
                )
            except Exception:
                pass
        # [v3.5] Динамические кнопки: текст-ответ и подменю прямо в БД.
        # Бот ловит callback и берёт из этих колонок что делать.
        for ddl in (
            "ALTER TABLE content_buttons ADD COLUMN response_text TEXT",
            "ALTER TABLE content_buttons ADD COLUMN submenu_name TEXT",
        ):
            try:
                await db.execute(ddl)
            except Exception:
                pass
        await db.commit()


# ============================================================
#  ТЕКСТЫ
# ============================================================

async def get_all_texts() -> dict[str, str]:
    """Возвращает {key: value} всех текстов из БД."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT key, value FROM content_texts")
        return {row[0]: row[1] for row in await cur.fetchall()}


async def set_text(key: str, value: str) -> None:
    """Создаёт или обновляет текст."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO content_texts (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )
        await db.commit()


async def text_exists(key: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM content_texts WHERE key = ? LIMIT 1", (key,),
        )
        return (await cur.fetchone()) is not None


# ============================================================
#  КНОПКИ
# ============================================================

async def get_menu_buttons(menu_name: str,
                           include_hidden: bool = False) -> list[dict]:
    """
    Возвращает кнопки меню в порядке position.

    [v3.5] По умолчанию скрытые кнопки (hidden=1) не возвращаются —
    бот их не показывает клиенту. Для админки: include_hidden=True.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = """
            SELECT position, text, kind, value,
                   COALESCE(hidden, 0) AS hidden
            FROM content_buttons
            WHERE menu_name = ?
        """
        if not include_hidden:
            sql += " AND COALESCE(hidden, 0) = 0"
        sql += " ORDER BY position"
        cur = await db.execute(sql, (menu_name,))
        return [dict(r) for r in await cur.fetchall()]


async def list_menus() -> list[str]:
    """Список всех меню в БД."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT DISTINCT menu_name FROM content_buttons ORDER BY menu_name"
        )
        return [r[0] for r in await cur.fetchall()]


async def replace_menu(menu_name: str, buttons: list[dict]) -> None:
    """
    Полностью заменяет содержимое меню: удаляет старые, вставляет новые.
    buttons: [{"text": ..., "kind": ..., "value": ...}, ...]
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM content_buttons WHERE menu_name = ?", (menu_name,),
        )
        for pos, btn in enumerate(buttons):
            await db.execute(
                """
                INSERT INTO content_buttons
                  (menu_name, position, text, kind, value, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (menu_name, pos, btn["text"], btn["kind"], btn["value"]),
            )
        await db.commit()


async def menu_exists(menu_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM content_buttons WHERE menu_name = ? LIMIT 1",
            (menu_name,),
        )
        return (await cur.fetchone()) is not None


# ============================================================
#  АВТО-ОТВЕТЫ
# ============================================================

async def list_qa(include_hidden: bool = False) -> list[dict]:
    """
    Все авто-ответы в порядке position.

    [v3.5] По умолчанию скрытые ответы (hidden=1) не возвращаются —
    бот их не показывает клиентам. Для админки: include_hidden=True.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = """
            SELECT key, label, text, position, photo_path,
                   COALESCE(hidden, 0) AS hidden, updated_at
            FROM content_qa
        """
        if not include_hidden:
            sql += " WHERE COALESCE(hidden, 0) = 0"
        sql += " ORDER BY position, key"
        cur = await db.execute(sql)
        return [dict(r) for r in await cur.fetchall()]


async def get_qa(key: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT key, label, text, position, photo_path, "
            "COALESCE(hidden, 0) AS hidden "
            "FROM content_qa WHERE key = ?",
            (key,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


# ============================================================
#  [v3.5] СКРЫТИЕ КНОПОК И QA (вместо удаления)
# ============================================================

async def set_button_hidden(menu_name: str, position: int,
                            hidden: bool) -> bool:
    """
    Скрывает или показывает кнопку. Возвращает True если запись была обновлена.
    Кнопка остаётся в БД, бот её не показывает когда hidden=1.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE content_buttons SET hidden = ? "
            "WHERE menu_name = ? AND position = ?",
            (1 if hidden else 0, menu_name, position),
        )
        await db.commit()
        return cur.rowcount > 0


# [v3.5] ВИРТУАЛЬНЫЕ КНОПКИ — для меню которых нет в content_buttons
# (например mykey_menu). Бот генерит их клавиатуры в коде; чтобы оператор
# мог скрывать отдельные кнопки через UI, храним hidden-override здесь.

async def set_virtual_button_hidden(menu_name: str, button_value: str,
                                    hidden: bool) -> None:
    """Скрывает/показывает кнопку виртуального меню (mykey_menu и т.п.)."""
    async with aiosqlite.connect(DB_PATH) as db:
        if hidden:
            await db.execute(
                "INSERT OR IGNORE INTO virtual_button_hidden "
                "(menu_name, button_value) VALUES (?, ?)",
                (menu_name, button_value),
            )
        else:
            await db.execute(
                "DELETE FROM virtual_button_hidden "
                "WHERE menu_name = ? AND button_value = ?",
                (menu_name, button_value),
            )
        await db.commit()


async def get_virtual_hidden_set(menu_name: str) -> set[str]:
    """Возвращает множество button_value которые скрыты в виртуальном меню."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT button_value FROM virtual_button_hidden WHERE menu_name = ?",
            (menu_name,),
        )
        rows = await cur.fetchall()
    return {r[0] for r in rows}


async def is_virtual_button_hidden(menu_name: str, button_value: str) -> bool:
    """Скрыта ли конкретная кнопка виртуального меню."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM virtual_button_hidden "
            "WHERE menu_name = ? AND button_value = ?",
            (menu_name, button_value),
        )
        return await cur.fetchone() is not None


async def set_qa_hidden(key: str, hidden: bool) -> bool:
    """Скрывает или показывает авто-ответ."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE content_qa SET hidden = ? WHERE key = ?",
            (1 if hidden else 0, key),
        )
        await db.commit()
        return cur.rowcount > 0


async def set_qa(key: str, label: str, text: str,
                 position: int | None = None) -> None:
    """Создаёт или обновляет авто-ответ."""
    async with aiosqlite.connect(DB_PATH) as db:
        if position is None:
            # ставим в конец
            cur = await db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM content_qa")
            row = await cur.fetchone()
            position = row[0]

        await db.execute(
            """
            INSERT INTO content_qa (key, label, text, position, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                label = excluded.label,
                text = excluded.text,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, label, text, position),
        )
        await db.commit()


async def delete_qa(key: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM content_qa WHERE key = ?", (key,),
        )
        await db.commit()
        return cur.rowcount > 0


async def qa_exists(key: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM content_qa WHERE key = ? LIMIT 1", (key,),
        )
        return (await cur.fetchone()) is not None


# ============================================================
#  TOMBSTONES — отметки об удалённых ключах
# ============================================================

async def add_tombstone(kind: str, key: str) -> None:
    """
    Записывает что админ намеренно удалил ключ. Миграция игнорирует
    такие записи и не восстанавливает их из дефолтов.
    kind: 'text' | 'menu' | 'qa'
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO content_tombstones(kind, key) VALUES (?, ?)",
            (kind, key),
        )
        await db.commit()


async def remove_tombstone(kind: str, key: str) -> None:
    """Снимает отметку об удалении (когда админ создаёт ключ заново)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM content_tombstones WHERE kind=? AND key=?",
            (kind, key),
        )
        await db.commit()


async def has_tombstone(kind: str, key: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM content_tombstones WHERE kind=? AND key=? LIMIT 1",
            (kind, key),
        )
        return (await cur.fetchone()) is not None


# ============================================================
#  МИГРАЦИЯ ИЗ ФАЙЛОВ
# ============================================================

async def migrate_from_files() -> dict:
    """
    Заливает в БД дефолтные значения из texts.py и keyboards.py —
    но ТОЛЬКО для тех ключей, которых в БД ещё нет.

    Существующие записи в БД не трогаются — это и есть «правки пережили обновление».

    Возвращает: {texts_added, buttons_added_menus, qa_added}
    """
    from app import texts as default_texts
    from app import keyboards as default_keyboards

    stats = {"texts_added": 0, "buttons_added_menus": 0, "qa_added": 0}

    # 1) Тексты
    for name in dir(default_texts):
        if name.startswith("_"):
            continue
        if not name.isupper():  # все ключи в TEXTS_PY в верхнем регистре
            continue
        value = getattr(default_texts, name)
        if not isinstance(value, str):
            continue
        # Не воскрешаем то что админ удалил намеренно
        if await has_tombstone("text", name):
            continue
        if not await text_exists(name):
            await set_text(name, value)
            stats["texts_added"] += 1

    # 2) Кнопки — переносим каждое меню если в БД его ещё нет
    # Каждое меню — это функция в keyboards.py, возвращающая InlineKeyboardMarkup
    MENU_FUNCS = [
        "main_menu", "pay_menu", "bot_menu", "vpn_menu", "shop_menu",
        "community_menu", "mykey_back_keyboard", "mykey_reset_confirm_keyboard",
    ]
    for menu_name in MENU_FUNCS:
        # Не воскрешаем удалённые меню
        if await has_tombstone("menu", menu_name):
            continue
        if await menu_exists(menu_name):
            continue
        func = getattr(default_keyboards, menu_name, None)
        if not callable(func):
            continue
        try:
            kb = func()
        except Exception:
            continue
        buttons = _extract_buttons_from_markup(kb)
        if buttons:
            await replace_menu(menu_name, buttons)
            stats["buttons_added_menus"] += 1

    # 3) Авто-ответы — из словаря ADMIN_ANSWERS + admin_quick_keyboard
    admin_answers = getattr(default_keyboards, "ADMIN_ANSWERS", {})
    quick_kb_func = getattr(default_keyboards, "admin_quick_keyboard", None)

    labels: dict[str, str] = {}
    if callable(quick_kb_func):
        try:
            kb = quick_kb_func()
            for row in kb.inline_keyboard:
                for btn in row:
                    if btn.callback_data and btn.callback_data.startswith("qa_"):
                        labels[btn.callback_data] = btn.text
        except Exception:
            pass

    # Сначала собираем порядок из admin_quick_keyboard, потом — остальные ключи
    ordered_keys: list[str] = list(labels.keys())
    for key in admin_answers:
        if key not in ordered_keys:
            ordered_keys.append(key)

    for pos, key in enumerate(ordered_keys):
        # Не воскрешаем удалённые админом авто-ответы
        if await has_tombstone("qa", key):
            continue
        if await qa_exists(key):
            continue
        label = labels.get(key, key)
        text = admin_answers.get(key, "")
        if not text:
            continue
        await set_qa(key, label, text, position=pos)
        stats["qa_added"] += 1

    return stats


def _extract_buttons_from_markup(markup) -> list[dict]:
    """Превращает InlineKeyboardMarkup в список словарей."""
    buttons = []
    for row in markup.inline_keyboard:
        for btn in row:
            kind = "callback" if btn.callback_data else "url"
            value = btn.callback_data or btn.url or ""
            buttons.append({
                "text": btn.text,
                "kind": kind,
                "value": value,
            })
    return buttons
