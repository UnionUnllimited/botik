"""Миграции не должны отставать от моделей.

Тест не поднимает Postgres: он сверяет состав таблиц и колонок в моделях
с тем, что реально создаётся миграциями. Забыли `make migration` — тест упадёт.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.models import Base

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"

_CREATE_TABLE_RE = re.compile(r"op\.create_table\(\s*[\"'](?P<table>\w+)[\"'](?P<body>.*?)\n    \)", re.S)
_COLUMN_RE = re.compile(r"sa\.Column\(\s*[\"'](?P<column>\w+)[\"']")
_ADD_COLUMN_RE = re.compile(
    r"op\.add_column\(\s*[\"'](?P<table>\w+)[\"'],\s*sa\.Column\(\s*[\"'](?P<column>\w+)[\"']", re.S
)
_DROP_COLUMN_RE = re.compile(r"op\.drop_column\(\s*[\"'](?P<table>\w+)[\"'],\s*[\"'](?P<column>\w+)[\"']")
_DROP_TABLE_RE = re.compile(r"op\.drop_table\(\s*[\"'](?P<table>\w+)[\"']")

# Колонку добавляют и голым SQL: `ADD COLUMN IF NOT EXISTS` нужен там, где
# ревизия уже применена на сервере и `op.add_column` упал бы на дубликате.
# Такую миграцию тест обязан видеть — иначе он объявляет расхождение там,
# где колонка на самом деле создаётся.
_SQL_ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+(?P<table>\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<column>\w+)",
    re.I,
)
_SQL_DROP_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+(?P<table>\w+)\s+DROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?(?P<column>\w+)",
    re.I,
)


def _migration_sources() -> list[str]:
    files = sorted(p for p in MIGRATIONS_DIR.glob("*.py") if not p.name.startswith("_"))
    assert files, "Не найдено ни одной миграции"
    return [path.read_text(encoding="utf-8") for path in files]


def _schema_from_migrations() -> dict[str, set[str]]:
    """Проигрывает upgrade()-части миграций «на бумаге» и собирает итоговую схему."""
    schema: dict[str, set[str]] = {}
    for source in _migration_sources():
        upgrade = source.split("def upgrade()", 1)[-1].split("def downgrade()", 1)[0]
        for match in _CREATE_TABLE_RE.finditer(upgrade):
            table = match.group("table")
            schema[table] = set(_COLUMN_RE.findall(match.group("body")))
        for match in _ADD_COLUMN_RE.finditer(upgrade):
            schema.setdefault(match.group("table"), set()).add(match.group("column"))
        for match in _SQL_ADD_COLUMN_RE.finditer(upgrade):
            schema.setdefault(match.group("table"), set()).add(match.group("column"))
        for match in _DROP_COLUMN_RE.finditer(upgrade):
            schema.get(match.group("table"), set()).discard(match.group("column"))
        for match in _SQL_DROP_COLUMN_RE.finditer(upgrade):
            schema.get(match.group("table"), set()).discard(match.group("column"))
        for match in _DROP_TABLE_RE.finditer(upgrade):
            schema.pop(match.group("table"), None)
    return schema


def test_every_model_table_is_created_by_migrations():
    migrated = _schema_from_migrations()
    missing = set(Base.metadata.tables) - set(migrated)
    assert not missing, f"Нет миграции для таблиц: {sorted(missing)}"


def test_no_orphan_tables_in_migrations():
    migrated = _schema_from_migrations()
    extra = set(migrated) - set(Base.metadata.tables)
    assert not extra, f"Таблицы есть в миграциях, но не в моделях: {sorted(extra)}"


@pytest.mark.parametrize("table_name", sorted(Base.metadata.tables))
def test_columns_match(table_name: str):
    migrated = _schema_from_migrations()
    expected = {column.name for column in Base.metadata.tables[table_name].columns}
    assert expected == migrated[table_name], (
        f"Расхождение колонок в {table_name}: "
        f"нет в миграции {sorted(expected - migrated[table_name])}, "
        f"лишние в миграции {sorted(migrated[table_name] - expected)}"
    )


def test_downgrade_is_implemented():
    for source in _migration_sources():
        downgrade = source.split("def downgrade()", 1)[-1]
        assert "op." in downgrade, "downgrade() пустой — откат миграции невозможен"
