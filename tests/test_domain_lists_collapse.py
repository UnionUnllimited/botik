"""Обвалившийся список доменов не уезжает на роутеры.

Источников больше двадцати, и половина из них — чужой GitHub, который
отдаёт 429 целыми пачками. Сборка складывалась из тех, кто ответил, и
результат публиковался как есть: список короче в разы, и половина сайтов
у всех клиентов сразу идёт мимо туннеля. Снаружи это выглядит как
«перестало работать», и причину ищут в роутере.

Сторожим связку «источники отпали И список обвалился». Если ответили все,
сокращение настоящее — сократили сам список, — и держать старый нельзя:
иначе проверка заперла бы сборку навсегда.
"""

from __future__ import annotations

from decimal import Decimal  # noqa: F401  — таблицы моделей тянут MONEY

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core.config import settings
from core.models import (
    DomainBuild,
    DomainSource,
    ListKind,
    ManualList,
    ManualListRevision,
    Notification,
    Setting,
)
from core.models.base import Base
from core.services import domain_lists


@compiles(JSONB, "sqlite")
def _jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "INTEGER"


TABLES = [
    DomainSource.__table__,
    DomainBuild.__table__,
    ManualList.__table__,
    ManualListRevision.__table__,
    Notification.__table__,
    Setting.__table__,
]


async def _world():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _domains(count: int, *, prefix: str = "d") -> str:
    return "\n".join(f"{prefix}{number}.example.com" for number in range(count))


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Списки пишем в свой каталог, наружу ничего не отдаём."""
    monkeypatch.setattr(settings.app, "media_dir", str(tmp_path))
    monkeypatch.setattr(settings.bot, "alerts_chat_id", -100500)

    async def _no_upload(*_args, **_kwargs):
        return False

    async def _no_config(*_args, **_kwargs):
        # Настройки выгрузки лежат за кэшем в Redis, которого в тестах нет:
        # каждое чтение ждёт таймаут и складывается в минуты.
        return {}

    monkeypatch.setattr(domain_lists, "upload", _no_upload)
    monkeypatch.setattr(domain_lists, "config", _no_config)
    monkeypatch.setattr(domain_lists, "publish_local", lambda *_a, **_k: False)


async def _sources(session, count: int) -> None:
    for number in range(count):
        session.add(
            DomainSource(
                title=f"Источник {number}",
                url=f"https://lists.example/{number}.lst",
                kind=ListKind.PROXY_DOMAIN,
                is_enabled=True,
                sort_order=number,
            )
        )
    await session.flush()


async def _previous(session, domains: int) -> DomainBuild:
    record = DomainBuild(domains=domains, ips=0, skipped=False, manual_hash="")
    session.add(record)
    await session.flush()
    return record


def _answers(monkeypatch, *, alive: int, each: int):
    """Первые `alive` источников отвечают, остальные — отказом."""
    seen: list[str] = []

    async def _fetch(_client, url, etag=""):
        seen.append(url)
        number = int(url.rsplit("/", 1)[1].split(".")[0])
        if number < alive:
            return _domains(each, prefix=f"s{number}x"), f"etag{number}", ""
        return "", "", "429 Too Many Requests"

    monkeypatch.setattr(domain_lists, "fetch", _fetch)
    return seen


@pytest.mark.asyncio
async def test_a_collapse_keeps_the_old_list(monkeypatch):
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _sources(session, 10)
            await _previous(session, domains=1000)
            await session.commit()

            # Ответили двое из десяти: вместо тысячи строк соберётся двадцать.
            _answers(monkeypatch, alive=2, each=10)
            record = await domain_lists.build(session, force=True)
            await session.commit()

        assert record.skipped is True
        assert record.domains == 1000, "в записи должно остаться прежнее число"
        assert "обвалился" in record.error

        async with factory() as session:
            alarms = list(await session.scalars(select(Notification)))
        assert len(alarms) == 1
        assert "не обновлены" in alarms[0].text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_full_build_publishes_even_if_it_shrank(monkeypatch):
    """Ответили все — значит список правда сократили, и старый держать нельзя."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _sources(session, 4)
            await _previous(session, domains=1000)
            await session.commit()

            _answers(monkeypatch, alive=4, each=5)
            record = await domain_lists.build(session, force=True)
            await session.commit()

        assert record.skipped is False
        assert record.domains == 20
        assert record.failed_sources == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_small_dip_with_a_failed_source_still_goes(monkeypatch):
    """Один источник отпал, список просел чуть-чуть — это не обвал."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _sources(session, 10)
            await _previous(session, domains=100)
            await session.commit()

            # Девять из десяти по десять строк — девяносто против ста.
            _answers(monkeypatch, alive=9, each=10)
            record = await domain_lists.build(session, force=True)
            await session.commit()

        assert record.skipped is False
        assert record.domains == 90
        assert record.failed_sources == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_very_first_build_has_nothing_to_compare_with(monkeypatch):
    """Сравнивать не с чем — публикуем, иначе первая сборка не пройдёт никогда."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _sources(session, 4)
            await session.commit()

            _answers(monkeypatch, alive=1, each=3)
            record = await domain_lists.build(session, force=True)
            await session.commit()

        assert record.skipped is False
        assert record.domains == 3
    finally:
        await engine.dispose()
