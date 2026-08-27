"""Очередь сообщений, снимок подписок и погашение висящих счетов.

Три места, которые ломаются тихо и не сразу: зеркало подписок отдаёт боту
не то, очередь сообщений растёт вечно, а погашение просроченных ссылок
ходит к провайдеру по всему накопленному хвосту разом.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core.enums import (
    PaymentProviderName,
    PaymentPurpose,
    PaymentStatus,
    SubscriptionStatus,
)
from core.models import Notification, Payment, Subscription, User
from core.models.base import Base

NOW = dt.datetime(2026, 8, 28, 12, tzinfo=dt.UTC)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(_type, _compiler, **_kwargs) -> str:
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_as_integer(_type, _compiler, **_kwargs) -> str:
    """SQLite нумерует сама только INTEGER PRIMARY KEY."""
    return "INTEGER"


async def _factory(tables):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(sync_connection, tables=tables)
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class TestSubscriptionsSnapshot:
    """Снимок для базы бота: у клиента подписок много, поле там одно.

    Дашборд, фильтры и рассылки читают одно `subscription_end_date`. Пока
    в снимок попадала последняя по номеру, купленный второй роутер (подписка
    ждёт активации, срока у неё нет) затирал действующую — и зеркало
    переставало обновляться вовсе.
    """

    @pytest.mark.asyncio
    async def test_pending_does_not_hide_the_running_one(self):
        from api.routes import catalog_api

        engine, factory = await _factory([User.__table__, Subscription.__table__])
        try:
            async with factory() as session:
                session.add(User(id=1, tg_id=614685408))
                session.add(
                    Subscription(
                        id=1,
                        user_id=1,
                        status=SubscriptionStatus.ACTIVE,
                        expires_at=NOW + dt.timedelta(days=30),
                    )
                )
                # Куплен второй роутер: подписка ждёт его и срока не имеет.
                session.add(
                    Subscription(id=2, user_id=1, status=SubscriptionStatus.PENDING)
                )
                await session.commit()

                snapshot = await catalog_api.subscriptions_snapshot(session)
                rows = snapshot["subscriptions"]

                assert len(rows) == 1
                assert rows[0]["until"] is not None, (
                    "в зеркало ушла подписка без срока — бот такие пропускает, "
                    "и дата у клиента застывает навсегда"
                )
                assert rows[0]["until"].startswith("2026-09-27")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_farthest_date_wins(self):
        """Продлили — в зеркало идёт новый срок, а не первый попавшийся."""
        from api.routes import catalog_api

        engine, factory = await _factory([User.__table__, Subscription.__table__])
        try:
            async with factory() as session:
                session.add(User(id=1, tg_id=614685408))
                session.add_all(
                    [
                        Subscription(
                            id=1,
                            user_id=1,
                            status=SubscriptionStatus.ACTIVE,
                            expires_at=NOW + dt.timedelta(days=300),
                        ),
                        Subscription(
                            id=2,
                            user_id=1,
                            status=SubscriptionStatus.ACTIVE,
                            expires_at=NOW + dt.timedelta(days=10),
                        ),
                    ]
                )
                await session.commit()

                rows = (await catalog_api.subscriptions_snapshot(session))["subscriptions"]
                assert rows[0]["until"].startswith("2027-06-24")
        finally:
            await engine.dispose()


class TestNotificationsAreCleanedUp:
    """Очередь сообщений не должна расти вечно.

    В неё попадает каждое напоминание, каждое подтверждение оплаты и каждое
    изменение карточки заказа в рабочем чате. Чистки не было вовсе.
    """

    @pytest.mark.asyncio
    async def test_old_sent_messages_are_removed(self, monkeypatch):
        from worker.tasks import maintenance

        engine, factory = await _factory([Notification.__table__])
        try:
            old = dt.datetime.now(dt.UTC) - dt.timedelta(days=120)
            async with factory() as session:
                session.add_all(
                    [
                        Notification(tg_id=1, text="старое", kind="reminder", sent_at=old, created_at=old),
                        Notification(
                            tg_id=2,
                            text="свежее",
                            kind="reminder",
                            sent_at=dt.datetime.now(dt.UTC),
                        ),
                        # Неотправленное не трогаем, сколько бы ни лежало:
                        # это ещё не доставленное сообщение клиенту.
                        Notification(tg_id=3, text="ждёт отправки", kind="reminder", created_at=old),
                    ]
                )
                await session.commit()

            monkeypatch.setattr(maintenance, "session_scope", factory.begin)
            removed = await maintenance.cleanup_notifications()

            async with factory() as session:
                left = list(await session.scalars(select(Notification)))

            assert removed == 1
            assert {item.text for item in left} == {"свежее", "ждёт отправки"}
        finally:
            await engine.dispose()


class TestExpiryRunIsBounded:
    """Погашение просроченных ссылок ходит к провайдеру по каждой из них.

    Без предела один круг на накопленном хвосте — это длинная транзакция
    и держащиеся блокировки. У соседней задачи предел есть с самого начала.
    """

    @pytest.mark.asyncio
    async def test_one_run_takes_a_batch(self):
        from core.services import payments as payment_service

        engine, factory = await _factory([User.__table__, Payment.__table__])
        try:
            long_ago = dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)
            async with factory() as session:
                session.add(User(id=1, tg_id=614685408))
                for number in range(payment_service.EXPIRE_BATCH + 10):
                    session.add(
                        Payment(
                            user_id=1,
                            provider=PaymentProviderName.PLATEGA,
                            purpose=PaymentPurpose.ORDER,
                            status=PaymentStatus.PENDING,
                            idempotency_key=f"key-{number}",
                            amount=Decimal("100.00"),
                            currency="RUB",
                            expires_at=long_ago,
                        )
                    )
                await session.commit()

                closed = await payment_service.expire_stale_payments(session)
                await session.commit()

                assert closed == payment_service.EXPIRE_BATCH, (
                    "круг гасит всё разом: на накопленном хвосте это долгая транзакция"
                )
                left = await session.scalars(
                    select(Payment).where(Payment.status == PaymentStatus.PENDING)
                )
                assert len(list(left)) == 10, "остальные подождут следующего круга"
        finally:
            await engine.dispose()
