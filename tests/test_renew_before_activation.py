"""Продление до включения роутера: денег не берём, дни не теряем.

Срок подписки начинает идти при первом выходе роутера на связь. До этого
прибавить оплаченные дни некуда — при активации срок считается по тарифу
самой подписки. Счёт на такое продление выставлялся, деньги списывались,
а дни пропадали: клиент платил девятьсот рублей и получал прежние тридцать
дней. Комментарий в коде при этом обещал, что периоды складываются.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes import catalog_api
from core.enums import SubscriptionStatus
from core.models import Plan, Subscription, User
from core.models.base import Base
from core.services import subscriptions as subscription_service


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


def _plan(title: str, months: int) -> Plan:
    return Plan(
        slug=f"p{months}", title=title, months=months, extra_days=0,
        price=Decimal("300.00"), is_active=True,
    )


async def _world():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[User.__table__, Plan.__table__, Subscription.__table__]
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_no_invoice_while_the_term_has_not_started():
    engine, factory = await _world()
    try:
        async with factory() as session:
            user = User(tg_id=7, username="waiting")
            month = _plan("30 дней", 1)
            session.add_all([user, month])
            await session.flush()
            session.add(
                Subscription(
                    user_id=user.id, plan_id=month.id, status=SubscriptionStatus.PENDING,
                    pending_expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=180),
                )
            )
            await session.commit()

            answer = await catalog_api.renew_start(
                payload={"tg_id": 7, "plan_id": month.id}, session=session
            )

        assert answer["ok"] is False
        assert "не идёт" in answer["error"]
        # Ссылки на оплату быть не должно: счёт не выставляется вовсе.
        assert "pay_url" not in answer
    finally:
        await engine.dispose()


def test_days_paid_before_activation_are_not_silently_swallowed():
    """Счёт, выставленный до запрета, всё же могут оплатить.

    Дни к такой подписке не прибавляются — прибавлять некуда, — но след
    остаётся: и в журнале подписки, и предупреждением в логе. Раньше
    запись бодро сообщала «оплачен ещё один период», как будто он засчитан.
    """
    subscription = Subscription(
        user_id=1, plan_id=1, status=SubscriptionStatus.PENDING,
        pending_expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=10),
    )
    quarter = _plan("90 дней", 3)

    subscription_service.extend(subscription, plan=quarter, payment_id=42)

    (event,) = subscription.events
    assert "не начислены" in (event.comment or "")
    # Предел активации отодвинут: роутер ещё можно включить.
    assert subscription.pending_expires_at > dt.datetime.now(dt.UTC) + dt.timedelta(days=100)
    # Срок при этом не появился — он начнётся при активации.
    assert subscription.expires_at is None
