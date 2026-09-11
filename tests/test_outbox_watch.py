"""Вставшая очередь сообщений перестала быть незаметной.

Своего бота у нас нет: мы кладём текст в очередь, а забирает и отправляет
его бот стороннего продукта. Перестанет забирать — встанет вся переписка
разом: напоминания о сроке, статусы заказов, счета на доставку. Заказы при
этом оформляются, деньги приходят, и снаружи ничего не видно. Узнать об
этом можно было только от клиента, который не дождался ответа.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core.config import settings
from core.metrics import outbox_oldest_seconds, outbox_pending
from core.models import Notification
from core.models.base import Base
from core.notifications import OUTBOX_MAX_ATTEMPTS
from worker.tasks import outbox_watch as task


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_as_integer(_type, _compiler, **_kwargs) -> str:
    return "INTEGER"


async def _world():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=[Notification.__table__])
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _queued(minutes_ago: int, *, attempts: int = 0, kind: str = "reminder") -> Notification:
    return Notification(
        tg_id=777,
        text="Подписка заканчивается через 3 дня.",
        buttons=[],
        kind=kind,
        attempts=attempts,
        created_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago),
    )


def _run(monkeypatch, factory) -> None:
    monkeypatch.setattr(task, "session_scope", factory.begin)
    # Без адресата тревога не ставится вовсе, а проверяем мы именно её.
    monkeypatch.setattr(settings.bot, "alerts_chat_id", -100500)


async def _alerts(factory) -> list[Notification]:
    async with factory() as session:
        rows = await session.scalars(
            select(Notification).where(Notification.kind == task.ALERT_KIND)
        )
        return list(rows)


@pytest.mark.asyncio
async def test_a_moving_queue_raises_nothing(monkeypatch):
    engine, factory = await _world()
    try:
        async with factory() as session:
            session.add(_queued(minutes_ago=1))
            await session.commit()

        _run(monkeypatch, factory)
        age = await task.watch_outbox()

        assert age < task.STUCK_AFTER_MIN * 60
        assert await _alerts(factory) == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_empty_queue_is_not_a_standstill(monkeypatch):
    """Пусто — значит бот всё разобрал, а не значит, что он умер."""
    engine, factory = await _world()
    try:
        _run(monkeypatch, factory)
        assert await task.watch_outbox() == 0
        assert await _alerts(factory) == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_standstill_reaches_the_operator(monkeypatch):
    engine, factory = await _world()
    try:
        async with factory() as session:
            session.add(_queued(minutes_ago=95))
            session.add(_queued(minutes_ago=40))
            await session.commit()

        _run(monkeypatch, factory)
        age = await task.watch_outbox()

        assert age >= 95 * 60
        (alert,) = await _alerts(factory)
        # Возраст пишем по самому старому: оператору важно, сколько молчим.
        assert "1 ч 35 мин" in alert.text
        assert "В очереди: 2" in alert.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_alarm_does_not_pile_up_in_the_stuck_queue(monkeypatch):
    """Сторож ходит по кругу, а очередь стоит.

    Без этого вернувшийся бот вывалил бы оператору десяток одинаковых
    сообщений подряд — по одному за каждую четверть часа простоя.
    """
    engine, factory = await _world()
    try:
        async with factory() as session:
            session.add(_queued(minutes_ago=95))
            await session.commit()

        _run(monkeypatch, factory)
        await task.watch_outbox()
        await task.watch_outbox()
        await task.watch_outbox()

        assert len(await _alerts(factory)) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_client_who_blocked_the_bot_is_not_a_standstill(monkeypatch):
    """Исчерпавшее попытки сообщение ждёт не бота, а человека.

    Оно лежит в очереди вечно и не уйдёт никогда. Считать его простоем
    значит будить оператора из-за клиента, закрывшегося полгода назад.
    """
    engine, factory = await _world()
    try:
        async with factory() as session:
            session.add(_queued(minutes_ago=60 * 24 * 180, attempts=OUTBOX_MAX_ATTEMPTS))
            await session.commit()

        _run(monkeypatch, factory)

        assert await task.watch_outbox() == 0
        assert await _alerts(factory) == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_numbers_go_outside_where_the_bot_cannot_reach(monkeypatch):
    """Метрики — единственный канал, не зависящий от вставшего бота."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            session.add(_queued(minutes_ago=30))
            session.add(_queued(minutes_ago=5))
            await session.commit()

        _run(monkeypatch, factory)
        await task.watch_outbox()

        assert outbox_pending._value.get() == 2
        assert outbox_oldest_seconds._value.get() >= 30 * 60
    finally:
        await engine.dispose()


class TestHowLongWeHaveBeenSilent:
    def test_minutes_stay_minutes(self):
        assert task._phrase(36) == "36 мин"

    def test_hours_read_faster(self):
        assert task._phrase(135) == "2 ч 15 мин"

    def test_a_round_hour_keeps_both_digits(self):
        """«3 ч 0 мин» читается как оборванная строка."""
        assert task._phrase(180) == "3 ч 00 мин"
