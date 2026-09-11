"""Экран и напоминание называют одно и то же число дней.

Сколько осталось, считалось дважды: воркер — полными сутками, приложение —
округлением вверх. Подписка, до конца которой три с половиной дня, на экране
была «4 дня», а сообщение в тот же час говорило «через 3 дня». Клиент видит
два ответа на один вопрос и не знает, какому верить.

Теперь число считает сервер и отдаёт готовым — тем же `days_left`, по
которому уходят напоминания.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes import catalog_api
from core.dates import days_left
from core.enums import SubscriptionStatus
from core.models import Plan, Subscription, User
from core.models.base import Base

APP_JS = (
    Path(__file__).resolve().parents[1] / "api" / "static" / "miniapp" / "app.js"
).read_text(encoding="utf-8")


@compiles(JSONB, "sqlite")
def _jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "INTEGER"


async def _world():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[User.__table__, Plan.__table__, Subscription.__table__]
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _client(session, *, hours_left: float | None):
    user = User(tg_id=7, username="buyer")
    plan = Plan(slug="m1", title="30 дней", months=1, extra_days=0, price=Decimal("300.00"))
    session.add_all([user, plan])
    await session.flush()
    ends = (
        dt.datetime.now(dt.UTC) + dt.timedelta(hours=hours_left)
        if hours_left is not None
        else None
    )
    session.add(
        Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status=(
                SubscriptionStatus.ACTIVE if hours_left is not None else SubscriptionStatus.PENDING
            ),
            started_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=10),
            expires_at=ends,
            pending_expires_at=None if hours_left is not None else dt.datetime.now(dt.UTC),
        )
    )
    await session.flush()
    return ends


@pytest.mark.asyncio
async def test_the_screen_gets_the_same_number_as_the_reminder():
    """Три с половиной дня — это три полных, а не четыре."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            ends = await _client(session, hours_left=84)  # 3.5 суток
            await session.commit()

            state = await catalog_api.renew_state(tg_id=7, session=session)

        assert state["subscription"]["left"] == 3
        assert state["subscription"]["left"] == days_left(ends)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_last_day_is_zero_not_one():
    """Осталось шесть часов — суток не осталось ни одних."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _client(session, hours_left=6)
            await session.commit()

            state = await catalog_api.renew_state(tg_id=7, session=session)

        assert state["subscription"]["left"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_subscription_without_a_term_reports_nothing():
    """Ожидающая активации срока не имеет — и числа у неё быть не должно."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _client(session, hours_left=None)
            await session.commit()

            state = await catalog_api.renew_state(tg_id=7, session=session)

        assert state["subscription"]["left"] is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_stranger_gets_no_number_either():
    engine, factory = await _world()
    try:
        async with factory() as session:
            state = await catalog_api.renew_state(tg_id=999, session=session)

        assert state["has_client"] is False
        assert state["subscription"]["left"] is None
    finally:
        await engine.dispose()


class TestWhatTheAppDoesWithIt:
    def test_it_prefers_the_number_from_the_server(self):
        head = APP_JS.index("function termLeft(sub)")
        body = APP_JS[head : APP_JS.index("\n  //", head)]
        assert "typeof sub.left === 'number'" in body

    def test_its_own_count_is_by_full_days_too(self):
        """Запасной путь не должен разойтись с сервером по-своему."""
        head = APP_JS.index("function termLeft(sub)")
        body = APP_JS[head : APP_JS.index("\n  //", head)]
        assert "Math.floor((until.getTime() - Date.now()) / 86400000)" in body
        assert "Math.ceil((until.getTime()" not in body

    def test_zero_days_reads_as_the_last_day(self):
        """«0 дней осталось» читается как поломка счётчика."""
        head = APP_JS.index("function leftWord(n)")
        body = APP_JS[head : APP_JS.index("\n  function daysWord", head)]
        assert "последний день" in body
