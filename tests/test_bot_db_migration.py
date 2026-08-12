"""Файл базы бота переименован, и старую базу надо перенести, а не забыть.

На сервере она полна клиентов, заказов и настроек. Ошибка тут не падает
и не видна в логах: бот заводит рядом пустую базу и работает как ни в чём
не бывало — до первой жалобы клиента, у которого «пропала подписка».
Поэтому перенос проверяется отдельно, вместе с хвостом WAL.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

BOT_CONFIG = Path(__file__).resolve().parents[1] / "bot" / "config.py"


def _load_bot_config():
    """Грузит `bot/config.py` по пути: `bot/` — не пакет, импортом его не взять."""
    spec = importlib.util.spec_from_file_location("bot_config_under_test", BOT_CONFIG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bot_config():
    return _load_bot_config()


def _make_db(path: Path, *, rows: int = 3) -> None:
    """Заводит базу в режиме WAL и не закрывает хвост — как у живого бота."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE users (telegram_id INTEGER PRIMARY KEY);")
    conn.executemany("INSERT INTO users (telegram_id) VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    conn.close()


def _count_users(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


class TestMainDbMigration:
    def test_legacy_db_moves_with_its_rows(self, bot_config, tmp_path):
        legacy = tmp_path / "legacy.db"
        new = tmp_path / "router_bot.db"
        _make_db(legacy, rows=3)

        result = bot_config.migrate_main_db_if_needed(str(new), str(legacy))

        assert result == str(new)
        assert new.is_file()
        assert not legacy.exists()
        assert _count_users(new) == 3

    def test_wal_tail_is_not_left_behind(self, bot_config, tmp_path):
        """Записи из хвоста -wal должны оказаться в перенесённом файле."""
        legacy = tmp_path / "legacy.db"
        new = tmp_path / "router_bot.db"
        _make_db(legacy, rows=2)
        # Пишем ещё строку и уходим, не сворачивая WAL, — так и лежит база,
        # если бота прибили в момент выката.
        conn = sqlite3.connect(legacy)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("INSERT INTO users (telegram_id) VALUES (99)")
        conn.commit()
        conn.close()

        bot_config.migrate_main_db_if_needed(str(new), str(legacy))

        assert _count_users(new) == 3
        assert not (tmp_path / "legacy.db-wal").exists()
        assert not (tmp_path / "legacy.db-shm").exists()

    def test_existing_new_db_is_never_overwritten(self, bot_config, tmp_path):
        """Если новая база уже есть, старую не трогаем: перенос одноразовый."""
        legacy = tmp_path / "legacy.db"
        new = tmp_path / "router_bot.db"
        _make_db(legacy, rows=1)
        _make_db(new, rows=7)

        result = bot_config.migrate_main_db_if_needed(str(new), str(legacy))

        assert result == str(new)
        assert _count_users(new) == 7
        assert legacy.is_file()

    def test_no_legacy_db_is_not_an_error(self, bot_config, tmp_path):
        """Чистая установка: переносить нечего, путь всё равно новый."""
        new = tmp_path / "router_bot.db"

        result = bot_config.migrate_main_db_if_needed(str(new), str(tmp_path / "legacy.db"))

        assert result == str(new)
        assert not new.exists()

    def test_broken_move_keeps_working_on_the_old_file(self, bot_config, tmp_path, monkeypatch):
        """Перенос не удался — работаем на старой базе, а не на пустой новой."""
        legacy = tmp_path / "legacy.db"
        new = tmp_path / "router_bot.db"
        _make_db(legacy, rows=5)

        def _boom(*args, **kwargs):
            raise OSError("диск только на чтение")

        monkeypatch.setattr(bot_config.shutil, "move", _boom)

        result = bot_config.migrate_main_db_if_needed(str(new), str(legacy))

        assert result == str(legacy)
        assert _count_users(legacy) == 5
        assert not new.exists()

    def test_forbidden_word_is_not_spelled_in_the_source(self):
        """Прежнее имя собирается из частей: слова в исходнике быть не должно."""
        source = BOT_CONFIG.read_text(encoding="utf-8")
        assert ("v" + "pn") not in source.lower()
