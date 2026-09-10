"""Оплаченная подписка перестала сгорать молча.

Пока роутер ни разу не вышел на связь, срок не идёт — и подписка не
попадает в обычные напоминания: там всё считается от даты окончания,
а её ещё нет. Выходило, что человек платил за роутер с подпиской,
откладывал коробку на потом — и через полгода терял оплаченное, ни разу
об этом не услышав. Настройка при этом обещала «напоминаем заранее».
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core.config import settings
from core.enums import SubscriptionStatus
from core.models import Notification, Plan, Subscription, User
from core.models.base import Base
from worker.tasks import subscriptions as task


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_as_integer(_type, _compiler, **_kwargs) -> str:
    """SQLite нумерует сама только INTEGER PRIMARY KEY."""
    return "INTEGER"


async def _world():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[
                    User.__table__,
                    Plan.__table__,
                    Subscription.__table__,
                    Notification.__table__,
                ],
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _waiting(session, *, days_to_burn: int, tg_id: int = 501, username: str = "sleeper"):
    """Клиент оплатил, роутер не включил, до сгорания столько-то дней."""
    user = User(tg_id=tg_id, username=username)
    plan = Plan(slug="m1", title="30 дней", months=1, extra_days=0, price=Decimal("300.00"))
    session.add_all([user, plan])
    await session.flush()
    subscription = Subscription(
        user_id=user.id,
        plan_id=plan.id,
        status=SubscriptionStatus.PENDING,
        # Полдня сверху: иначе `days_left` округляет ровную границу вниз
        # и попадание в отметку зависит от секунд между двумя вызовами.
        pending_expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=days_to_burn, hours=12),
    )
    session.add(subscription)
    await session.flush()
    return user, subscription


async def _queued(factory, kind: str) -> list[Notification]:
    async with factory() as session:
        rows = await session.scalars(select(Notification).where(Notification.kind == kind))
        return list(rows)


@pytest.mark.asyncio
async def test_client_hears_about_the_deadline_before_it_arrives(monkeypatch):
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _waiting(session, days_to_burn=30)
            await session.commit()

        monkeypatch.setattr(task, "session_scope", factory.begin)
        sent = await task.remind_unactivated()

        assert sent == 1
        (letter,) = await _queued(factory, "activation_reminder")
        assert "30 дней" in letter.text
        # Это не «продлите подписку»: продлевать нечего, нужно включить роутер.
        assert "роутер" in letter.text.lower()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_same_day_does_not_bring_a_second_letter(monkeypatch):
    """Круг ходит по расписанию, но перезапуск воркера не должен дублить."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _waiting(session, days_to_burn=7)
            await session.commit()

        monkeypatch.setattr(task, "session_scope", factory.begin)
        first = await task.remind_unactivated()
        second = await task.remind_unactivated()

        assert (first, second) == (1, 0)
        assert len(await _queued(factory, "activation_reminder")) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_deadline_half_a_year_away_is_left_alone(monkeypatch):
    """Письмо «осталось 170 дней» — это спам, а не забота."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _waiting(session, days_to_burn=170)
            await session.commit()

        monkeypatch.setattr(task, "session_scope", factory.begin)

        assert await task.remind_unactivated() == 0
        assert await _queued(factory, "activation_reminder") == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_active_subscription_is_not_this_task_business(monkeypatch):
    """У действующей срок идёт, и о её конце напоминает соседний круг."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            _, subscription = await _waiting(session, days_to_burn=7)
            subscription.status = SubscriptionStatus.ACTIVE
            subscription.expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(days=7)
            subscription.pending_expires_at = None
            await session.commit()

        monkeypatch.setattr(task, "session_scope", factory.begin)

        assert await task.remind_unactivated() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_burning_tells_the_client_and_the_operator(monkeypatch):
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _waiting(session, days_to_burn=-2, username="lost")
            await session.commit()

        monkeypatch.setattr(task, "session_scope", factory.begin)
        monkeypatch.setattr(settings.bot, "alerts_chat_id", -100500)
        burned = await task.expire_unactivated()

        assert burned == 1
        (letter,) = await _queued(factory, "activation_expired")
        assert "поддержку" in letter.text
        (alert,) = await _queued(factory, "admin")
        # Оператору нужно, кому звонить: по одному номеру подписки клиента
        # в базе не найти, а разговор начинать ему.
        assert "@lost" in alert.text and "501" in alert.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_blocked_client_is_not_written_to_but_is_still_burned(monkeypatch):
    """Заблокированному боту писать некуда, а подписка всё равно кончилась."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            user, _ = await _waiting(session, days_to_burn=-1)
            user.bot_blocked = True
            await session.commit()

        monkeypatch.setattr(task, "session_scope", factory.begin)
        monkeypatch.setattr(settings.bot, "alerts_chat_id", -100500)

        assert await task.expire_unactivated() == 1
        assert await _queued(factory, "activation_expired") == []
        # Оператору сказать всё равно нужно: молчащий клиент — тем более повод.
        assert len(await _queued(factory, "admin")) == 1
    finally:
        await engine.dispose()


def test_the_setting_no_longer_promises_what_nobody_did():
    """Комментарий «напоминаем заранее» стоял в конфиге и был враньём."""
    assert settings.subscription.activation_reminder_days
    assert max(settings.subscription.activation_reminder_days) < (
        settings.subscription.activation_deadline_days
    ), "напоминание позже срока сгорания не уйдёт никогда"
