"""Окружение Alembic.

URL по умолчанию берётся из настроек приложения (POSTGRES_*).
Переопределить можно так:
    alembic -x url=postgresql+asyncpg://... upgrade head
    ALEMBIC_URL=sqlite:///./tmp.db alembic revision --autogenerate -m "..."
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from core.config import settings
from core.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    return x_args.get("url") or os.getenv("ALEMBIC_URL") or settings.db.async_dsn


def is_async_url(url: str) -> bool:
    return "+asyncpg" in url or "+aiosqlite" in url


def _configure(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
        transaction_per_migration=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations(url: str) -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_sync_migrations(url: str) -> None:
    connectable = engine_from_config(
        {"sqlalchemy.url": url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        do_run_migrations(connection)
    connectable.dispose()


def run_migrations_online() -> None:
    url = get_url()
    if is_async_url(url):
        asyncio.run(run_async_migrations(url))
    else:
        run_sync_migrations(url)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
