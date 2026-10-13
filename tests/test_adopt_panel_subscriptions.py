"""Подхват из панели сроков у роутеров, включённых до 27 августа 2026.

Тогда ручная активация заводила одну учётку в панели, а у нас — ничего.
Такие роутеры работают, но в парке у них «подписка: нет», клиенту не уходят
напоминания, а доступ панель отключит в свою дату молча. Скрипт заводит им
подписку на срок из панели. Здесь стерегутся две вещи: что он трогает
только живые сроки и что в истории подписки он не выдаёт себя за кнопку.
"""

from __future__ import annotations

import datetime as dt

import pytest

from core.enums import SubscriptionStatus
from core.services import subscriptions as service
from scripts.adopt_panel_subscriptions import ADOPT, EXPIRED, NO_ACCOUNT, NO_DATE, classify

NOW = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.UTC)


class TestClassify:
    def test_live_date_is_adopted(self):
        assert classify(NOW + dt.timedelta(days=68), has_account=True, now=NOW) == ADOPT

    def test_expired_is_left_to_a_human(self):
        """Заведи её — клиенту уйдёт «подписка закончилась» о доступе,
        который кончился давно и, может быть, по договорённости."""
        assert classify(NOW - dt.timedelta(minutes=1), has_account=True, now=NOW) == EXPIRED

    def test_no_account(self):
        assert classify(None, has_account=False, now=NOW) == NO_ACCOUNT

    def test_account_without_date(self):
        assert classify(None, has_account=True, now=NOW) == NO_DATE


class _Session:
    def __init__(self):
        self.added: list = []

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_adopted_subscription_says_where_it_came_from(monkeypatch):
    session = _Session()
    recorded: dict = {}

    async def _nothing(_session, _device_id):
        return None

    def _record(subscription, event, **kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr(service, "device_subscription", _nothing)
    monkeypatch.setattr(service, "_record", _record)

    until = NOW + dt.timedelta(days=68, hours=5)
    activated = NOW - dt.timedelta(days=60)
    result = await service.grant_manual(
        session,
        user_id=3,
        device_id=9,
        days=68,
        until=until,
        now=activated,
        source="panel",
        comment="подхвачено из панели",
    )

    assert session.added == [result]
    assert result.source == "panel"
    assert result.status is SubscriptionStatus.ACTIVE
    # Срок — ровно тот, что в панели, а не пересчитанный днями: иначе наша
    # дата отстаёт от панельной на часы, и доступ отключается не тогда.
    assert result.expires_at == until
    assert result.started_at == activated
    assert recorded["comment"] == "подхвачено из панели"


@pytest.mark.asyncio
async def test_button_activation_is_still_called_so(monkeypatch):
    session = _Session()
    recorded: dict = {}

    async def _nothing(_session, _device_id):
        return None

    monkeypatch.setattr(service, "device_subscription", _nothing)
    monkeypatch.setattr(service, "_record", lambda _s, _e, **kw: recorded.update(kw))

    result = await service.grant_manual(session, user_id=3, device_id=9, days=30, now=NOW)

    assert result.source == "manual"
    assert recorded["comment"] == "Ручная активация на 30 дн."


@pytest.mark.parametrize("argv", [[], ["--apply"]])
def test_the_script_starts(monkeypatch, argv):
    """Запуск целиком, до первого похода в базу.

    Логика выше проверялась, а сам `main()` — нет, и первый же запуск на
    сервере упал на `configure_logging()` без имени службы."""
    import sys

    from scripts import adopt_panel_subscriptions as script

    seen = {}

    async def _run(apply):
        seen["apply"] = apply
        return 0

    async def _dispose():
        seen["disposed"] = True

    monkeypatch.setattr(script, "run", _run)
    monkeypatch.setattr(script, "dispose_engine", _dispose)
    monkeypatch.setattr(sys, "argv", ["adopt_panel_subscriptions", *argv])
    script.main()
    assert seen == {"apply": argv == ["--apply"], "disposed": True}


@pytest.mark.asyncio
async def test_collect_on_a_real_database(monkeypatch):
    """Сбор целиком: база в памяти, панель подменена. Роутер со своей
    подпиской пропускается, с живой учёткой — подхватывается, без учётки —
    так и называется."""
    from sqlalchemy import BigInteger
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.ext.compiler import compiles

    from core.models import Device, Plan, Subscription, User
    from core.models.base import Base
    from core.services.remnawave import RemnaUser
    from scripts import adopt_panel_subscriptions as script

    @compiles(JSONB, "sqlite")
    def _jsonb(_type, _compiler, **_kwargs):
        return "JSON"

    @compiles(BigInteger, "sqlite")
    def _bigint(_type, _compiler, **_kwargs):
        return "INTEGER"

    class _Panel:
        async def users(self):
            return [RemnaUser(uid="1", username="tg7_d40dab2ba4ee", expire_at="2026-12-21T00:00:00Z")]

    monkeypatch.setattr(script.remnawave, "client", _Panel)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[User.__table__, Plan.__table__, Device.__table__, Subscription.__table__]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            user = User(tg_id=7, username="kelvin")
            session.add(user)
            await session.flush()
            paid = Device(mac="D4:0D:AB:03:5F:A8", user_id=user.id)
            panel_only = Device(mac="D4:0D:AB:2B:A4:EE", user_id=user.id)
            nothing = Device(mac="D4:0D:AB:28:32:18", user_id=user.id)
            session.add_all([paid, panel_only, nothing])
            await session.flush()
            session.add(Subscription(
                user_id=user.id, device_id=paid.id, status=SubscriptionStatus.ACTIVE,
                expires_at=NOW + dt.timedelta(days=60),
            ))
            await session.flush()
            findings = await script.collect(session, NOW)
        verdicts = {item.device.mac: item.verdict for item in findings}
        assert verdicts == {"D4:0D:AB:2B:A4:EE": ADOPT, "D4:0D:AB:28:32:18": NO_ACCOUNT}
    finally:
        await engine.dispose()
