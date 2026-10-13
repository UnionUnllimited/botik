"""Роутеры в рабочем чате: карточка, кнопки, одна тревога на повод и «вернулся».

Заказы уже работают в топиках рабочего чата. Роутеры теперь там же: топик
«Роутеры», карточка, когда нужна рука, и короткое сообщение, когда повод
прошёл. Здесь стерегутся вещи, на которых такой топик умирает:

* молчащему роутеру не предлагают действий через туннель — с телефона отказ
  читается как поломка;
* о поводе говорят один раз, а не каждый круг — иначе топик перестают читать;
* топик заводится один, даже если поводов сразу несколько;
* «Попробовать активировать» отвечает причиной, а не молчаливым «нет».
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core.enums import OrderStatus
from core.models import Device, Notification, Order, Plan, Subscription, User
from core.models.base import Base
from core.services import activation, router_topics
from worker.tasks import router_alerts

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.UTC)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_for_sqlite(_type, _compiler, **_kwargs) -> str:
    # SQLite нумерует сам только INTEGER PRIMARY KEY, а у очереди сообщений
    # ключ BIGINT. Целые в SQLite и так 64-битные — смысл тот же.
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
                    Order.__table__,
                    Device.__table__,
                    Subscription.__table__,
                    Notification.__table__,
                ],
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _order(user_id: int, status: OrderStatus, **kwargs) -> Order:
    return Order(
        public_number="R-1",
        user_id=user_id,
        status=status,
        subtotal=Decimal("6900"),
        discount_total=Decimal("0"),
        delivery_price=Decimal("0"),
        total=Decimal("6900"),
        customer_name="",
        customer_phone="",
        customer_city="",
        **kwargs,
    )


# ── Карточка ─────────────────────────────────────────────────────────────────


class TestHeadline:
    def test_silent(self):
        device = Device(mac="AA:BB:CC:DD:EE:FF", frp_online=False,
                        frp_last_seen_at=NOW - dt.timedelta(hours=30))
        text, kind = router_topics.headline(device, None, NOW)
        assert kind == router_topics.SILENT
        assert "молчит 1 дн 6 ч" in text

    def test_stuck(self):
        device = Device(mac="AA:BB:CC:DD:EE:FF", frp_online=True, activated_at=None)
        order = _order(1, OrderStatus.SHIPPED)
        _, kind = router_topics.headline(device, order, NOW)
        assert kind == router_topics.STUCK

    def test_fine(self):
        device = Device(mac="AA:BB:CC:DD:EE:FF", frp_online=True, activated_at=NOW)
        text, kind = router_topics.headline(device, None, NOW)
        assert kind == ""
        assert "всё в порядке" in text


@pytest.mark.asyncio
async def test_silent_router_gets_no_tunnel_actions():
    """Туннель молчащему не откроется — «Перезагрузить» ответило бы отказом."""
    engine, factory = await _world()
    try:
        async with factory() as session:
            user = User(tg_id=500, username="kelvin")
            session.add(user)
            await session.flush()
            device = Device(mac="AA:BB:CC:DD:EE:01", user_id=user.id, frp_online=False,
                            activated_at=NOW - dt.timedelta(days=30))
            session.add(device)
            await session.flush()
            actions = [b["data"].split(":")[2] for b in await router_topics.card_buttons(session, device)]
        assert "reboot" not in actions
        assert "act" not in actions
        assert actions == ["dm", "card"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stuck_router_can_be_activated_and_rebooted():
    engine, factory = await _world()
    try:
        async with factory() as session:
            user = User(tg_id=501, username="petrov")
            session.add(user)
            await session.flush()
            order = _order(user.id, OrderStatus.SHIPPED, shipped_at=NOW)
            session.add(order)
            await session.flush()
            device = Device(mac="AA:BB:CC:DD:EE:02", user_id=user.id, order_id=order.id, frp_online=True)
            session.add(device)
            await session.flush()
            actions = [b["data"].split(":")[2] for b in await router_topics.card_buttons(session, device)]
        assert actions[0] == "act", "главное действие — первым"
        assert "reboot" in actions
    finally:
        await engine.dispose()


def test_callbacks_fit_telegram_limit():
    assert len(router_topics.callback(2_000_000_000, "reboot").encode()) <= 64


# ── Топик ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_topic_is_created_once(monkeypatch):
    """Две тревоги до появления номера топика завели бы два топика."""

    async def _chat(_session):
        return -100123

    async def _no_thread(_session):
        return 0

    monkeypatch.setattr(router_topics.order_topics, "chat_id", _chat)
    monkeypatch.setattr(router_topics, "thread_id", _no_thread)
    engine, factory = await _world()
    try:
        async with factory() as session:
            first = Device(mac="AA:BB:CC:DD:EE:03")
            second = Device(mac="AA:BB:CC:DD:EE:04")
            session.add_all([first, second])
            await session.flush()
            assert await router_topics.push(session, first) is None
            await session.flush()
            assert await router_topics.push(session, second) is None
            await session.flush()
            queued = list(await session.scalars(select(Notification)))
        assert len(queued) == 1
        assert queued[0].topic_title == router_topics.TOPIC_TITLE
    finally:
        await engine.dispose()


# ── Сторож ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_alert_per_reason_and_a_word_when_back(monkeypatch):
    async def _chat(_session):
        return -100123

    async def _thread(_session):
        return 77

    monkeypatch.setattr(router_topics.order_topics, "chat_id", _chat)
    monkeypatch.setattr(router_topics, "thread_id", _thread)
    engine, factory = await _world()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _scope():
        async with factory() as session:
            yield session
            await session.commit()

    monkeypatch.setattr(router_alerts, "session_scope", _scope)
    try:
        async with factory() as session:
            user = User(tg_id=502, username="ivanova")
            session.add(user)
            await session.flush()
            session.add(Device(
                mac="AA:BB:CC:DD:EE:05", user_id=user.id, frp_online=False,
                activated_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=40),
                frp_last_seen_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=30),
            ))
            await session.commit()

        assert await router_alerts.watch_router_alerts() == 1
        assert await router_alerts.watch_router_alerts() == 0, "второй круг не повторяет тревогу"

        async with factory() as session:
            device = await session.scalar(select(Device))
            assert device.chat_alert_kind == router_topics.SILENT
            device.frp_online = True
            device.frp_last_seen_at = dt.datetime.now(dt.UTC)
            await session.commit()

        assert await router_alerts.watch_router_alerts() == 1
        async with factory() as session:
            device = await session.scalar(select(Device))
            texts = [n.text for n in await session.scalars(select(Notification).order_by(Notification.id))]
        assert device.chat_alert_kind is None
        assert "снова на связи" in texts[-1]
        assert all(n for n in texts)
    finally:
        await engine.dispose()


# ── Попробовать активировать ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_activation_explains_itself(monkeypatch):
    async def _off(_session, _key):
        return False

    monkeypatch.setattr(activation.settings_service, "get_bool", _off)
    device = Device(mac="AA:BB:CC:DD:EE:06", activated_at=None)

    ok, reason = await activation.try_activate_shipped(None, device)
    assert not ok and reason == "Автоактивация выключена."

    async def _no_order(_session, _device):
        return None

    monkeypatch.setattr(activation, "_order_awaiting_router", _no_order)
    # Нажал человек — переключатель обхода ему не указ, дальше уже про заказ.
    ok, reason = await activation.try_activate_shipped(None, device, respect_toggle=False)
    assert not ok and "нет отгруженного заказа" in reason


@pytest.mark.asyncio
async def test_already_activated_is_said_so():
    ok, reason = await activation.try_activate_shipped(None, Device(mac="AA", activated_at=NOW))
    assert not ok and reason == "Роутер уже активирован."


# ── Бот ──────────────────────────────────────────────────────────────────────


class TestBotWiring:
    BOT = ROOT / "bot"

    def test_router_is_registered(self):
        main = (self.BOT / "main.py").read_text(encoding="utf-8-sig")
        assert "from src.router_topics import router as router_topics_router" in main
        assert "dp.include_router(router_topics_router)" in main

    def test_reply_to_client_goes_through_the_one_group_handler(self):
        """Второй обработчик сообщений группы бот бы не вызвал."""
        orders = (self.BOT / "src" / "order_topics.py").read_text(encoding="utf-8")
        assert 'if action == "rdm":' in orders
        assert "router_topics.send_to_client" in orders

    def test_reboot_is_confirmed(self):
        source = (self.BOT / "src" / "router_topics.py").read_text(encoding="utf-8")
        branch = source[source.index('if action == "reboot":') : source.index('if action == "rbok":')]
        assert "Да, перезагрузить" in branch
        assert "router_reboot" not in branch, "первое нажатие только спрашивает"

    def test_long_actions_get_a_long_timeout(self):
        api = (self.BOT / "src" / "shop_api.py").read_text(encoding="utf-8")
        head = api.index("async def router_retry_activation")
        assert "timeout=ROUTER_ACTION_TIMEOUT_SEC" in api[head : head + 400]
