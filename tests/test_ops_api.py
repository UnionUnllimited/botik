"""Главная, состояние системы и общий поиск — ручки для админки бота.

* Главная — список дел. Считает тот же `monitoring.collect`, что и утренняя
  сводка, только оплаченные заказы берёт все, а не опаздывающие.
* Поиск — одна строка на всё: номер заказа, кусок MAC, @логин, телефон
  в любом написании. Проверяется на настоящей базе: ошибиться тут проще
  всего в самом запросе.
* Состояние — каждая проверка со своим пределом: зависшее звено даёт
  красную строку, а не зависшую страницу.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from decimal import Decimal

import pytest
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core.enums import OrderStatus
from core.models import Delivery, Device, Order, Subscription, User
from core.models.base import Base
from core.services import monitoring, search, system_health, today

NOW = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.UTC)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


async def _world():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _regexp(connection, _record):
        # В Postgres она есть, в SQLite — нет: без неё поиск по телефону
        # здесь не проверить вовсе.
        connection.create_function(
            "regexp_replace", 4, lambda value, pattern, repl, _flags: re.sub(pattern, repl, value or "")
        )

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[
                    User.__table__,
                    Order.__table__,
                    Delivery.__table__,
                    Device.__table__,
                    Subscription.__table__,
                ],
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _order(number: str, **kwargs) -> Order:
    values = {
        "public_number": number,
        "status": OrderStatus.PAID,
        "subtotal": Decimal("8900.00"),
        "discount_total": Decimal("0.00"),
        "delivery_price": Decimal("0.00"),
        "total": Decimal("8900.00"),
        "customer_name": "",
        "customer_phone": "",
        "customer_city": "",
    }
    values.update(kwargs)
    return Order(**values)


# ── Главная ──────────────────────────────────────────────────────────────────


class TestTodayGroups:
    def test_empty_groups_are_not_shown(self):
        """Заголовок «0» на главной — шум, который приучает не смотреть."""
        assert today.groups(monitoring.Digest(), now=NOW) == []

    def test_paid_order_turns_red_after_a_day(self):
        fresh = _order("R-1", paid_at=NOW - dt.timedelta(hours=3))
        fresh.id = 1
        old = _order("R-2", paid_at=NOW - dt.timedelta(hours=30))
        old.id = 2
        digest = monitoring.Digest(unshipped=[(old, "ждёт отгрузки"), (fresh, "цена доставки не назначена")])
        [group] = today.groups(digest, now=NOW)
        assert group["key"] == "unshipped"
        # Старший первым: он горит.
        assert [item["title"] for item in group["items"]] == ["R-2", "R-1"]
        assert [item["late"] for item in group["items"]] == [True, False]
        assert group["items"][1]["sub"].startswith("цена доставки не назначена")
        assert group["items"][0]["link"] == {"kind": "order", "id": 2}

    def test_expiring_links_to_the_router(self):
        user = User(tg_id=500, username="kelvin")
        subscription = Subscription(user_id=1, device_id=9, expires_at=NOW + dt.timedelta(days=2))
        [group] = today.groups(monitoring.Digest(expiring=[(subscription, user)]), now=NOW)
        assert group["items"][0]["link"] == {"kind": "router", "id": 9}
        assert group["items"][0]["title"] == "@kelvin"

    @pytest.mark.parametrize(
        ("delta", "text"),
        [
            (dt.timedelta(minutes=10), "10 мин"),
            (dt.timedelta(hours=5), "5 ч"),
            (dt.timedelta(days=3), "3 дн"),
        ],
    )
    def test_ago(self, delta, text):
        assert today.ago(NOW - delta, NOW) == text


@pytest.mark.asyncio
async def test_fresh_paid_order_is_on_the_page_but_not_in_the_digest():
    """Сводке нужны опаздывающие, главной — вся работа."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            user = User(tg_id=1, username="fresh")
            session.add(user)
            await session.flush()
            session.add(
                _order("R-NEW", user_id=user.id, paid_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2))
            )
            await session.commit()
            digest = await monitoring.collect(session)
            page = await monitoring.collect(session, unshipped_hours=0)
        assert digest.unshipped == []
        assert [order.public_number for order, _ in page.unshipped] == ["R-NEW"]
    finally:
        await engine.dispose()


# ── Поиск ────────────────────────────────────────────────────────────────────


async def _shop(session):
    user = User(tg_id=614685408, username="union_unlimited", full_name="Перт Перри", phone="+7 900 123-45-67")
    session.add(user)
    await session.flush()
    session.add(_order("R-260811-0002", user_id=user.id, customer_name="Иванов Иван",
                       customer_phone="89001234567", customer_city="Москва"))
    session.add(Device(mac="D4:0D:AB:03:5F:A8", model="cudy,wr3000s-v1", user_id=user.id))
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "kind", "title"),
    [
        ("5F:A8", "router", "D4:0D:AB:03:5F:A8"),
        ("d40dab035fa8", "router", "D4:0D:AB:03:5F:A8"),
        ("wr3000s", "router", "D4:0D:AB:03:5F:A8"),
        ("0002", "order", "R-260811-0002"),
        ("Иванов", "order", "R-260811-0002"),
        ("@union_unlimited", "client", "@union_unlimited"),
        ("614685408", "client", "@union_unlimited"),
        ("Перри", "client", "@union_unlimited"),
    ],
)
async def test_search_finds(query, kind, title):
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _shop(session)
            results = await search.find(session, query)
        assert any(r["kind"] == kind and r["title"] == title for r in results), results
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["+7 (900) 123-45-67", "89001234567", "9001234567"])
async def test_phone_in_any_spelling(query):
    """Номер хранится то с «+7», то с «8», то с пробелами — человек один."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _shop(session)
            kinds = {r["kind"] for r in await search.find(session, query)}
        assert {"order", "client"} <= kinds
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_too_short_query_finds_nothing():
    engine, factory = await _world()
    try:
        async with factory() as session:
            await _shop(session)
            assert await search.find(session, "5") == []
    finally:
        await engine.dispose()


# ── Состояние ────────────────────────────────────────────────────────────────


class TestHealth:
    def test_overall_is_the_worst(self):
        assert system_health.overall([{"status": "ok"}, {"status": "off"}]) == "ok"
        assert system_health.overall([{"status": "ok"}, {"status": "warn"}]) == "warn"
        assert system_health.overall([{"status": "warn"}, {"status": "fail"}]) == "fail"

    @pytest.mark.asyncio
    async def test_a_hanging_check_is_a_red_line_not_a_hanging_page(self, monkeypatch):
        monkeypatch.setattr(system_health, "CHECK_TIMEOUT_SEC", 0.05)

        async def _hangs():
            await asyncio.sleep(5)

        check = await system_health._guard("panel", "Панель", _hangs)
        assert check.status == "fail"
        assert "не ответило" in check.detail

    @pytest.mark.asyncio
    async def test_a_crashing_check_is_a_red_line_too(self):
        async def _crashes():
            raise RuntimeError("boom")

        check = await system_health._guard("frps", "frps", _crashes)
        assert check.status == "fail"
        assert "boom" in check.detail
