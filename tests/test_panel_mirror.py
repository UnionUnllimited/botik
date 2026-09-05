"""Учётка панели в зеркале подписок.

Шапка карточки клиента у бота и вкладка «Ключи» смотрят не на срок, а на
ключ учётки в его строке клиента. Учётку роутера заводим мы, и без ключа
в снимке у клиента с работающим роутером стояло «Без ключа».

Проверяется отбор: чья учётка, какая из нескольких, и что панель, которой
нет или которая молчит, не ломает зеркало срока — оно старше и важнее.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes import catalog_api
from core.enums import SubscriptionStatus
from core.models import Subscription, User
from core.models.base import Base
from core.services.remnawave import RemnaUser


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


def _account(tg_id, username, short="", expire="2026-12-01T00:00:00.000Z"):
    return RemnaUser(
        uid=f"uuid-{username}", username=username, expire_at=expire,
        telegram_id=tg_id, short_uuid=short,
    )


class FakePanel:
    def __init__(self):
        self.accounts: list[RemnaUser] = []
        self.configured = True
        self.fail = False

    @property
    def is_configured(self) -> bool:
        return self.configured

    async def users(self):
        if self.fail:
            raise RuntimeError("panel down")
        return list(self.accounts)


@pytest.fixture
def panel(monkeypatch):
    fake = FakePanel()
    monkeypatch.setattr(catalog_api.remnawave, "client", lambda: fake)
    return fake


class TestWhichAccountIsTheClients:
    @pytest.mark.asyncio
    async def test_router_account_is_matched_by_telegram_id(self, panel):
        panel.accounts = [_account(614685408, "tg614685408_d4-0d-ab-2b-a4-ee", short="k1")]

        found = await catalog_api._router_accounts_by_client()

        assert found[614685408].short_uuid == "k1"

    @pytest.mark.asyncio
    async def test_phone_subscription_is_left_to_the_bot(self, panel):
        """`tg{id}` без MAC — подписка для телефона. Её бот завёл сам и держит
        в собственных полях; подсунуть её же второй раз значит показать
        оператору один ключ дважды под разными именами."""
        panel.accounts = [_account(614685408, "tg614685408", short="phone")]

        assert await catalog_api._router_accounts_by_client() == {}

    @pytest.mark.asyncio
    async def test_account_without_key_is_useless_to_the_card(self, panel):
        panel.accounts = [_account(614685408, "tg614685408_aa-bb", short="")]

        assert await catalog_api._router_accounts_by_client() == {}

    @pytest.mark.asyncio
    async def test_farthest_expiry_wins_when_client_has_two_routers(self, panel):
        """В их строке ключ один; берём тот, что проживёт дольше."""
        panel.accounts = [
            _account(1, "tg1_aa-aa", short="old", expire="2026-10-01T00:00:00.000Z"),
            _account(1, "tg1_bb-bb", short="new", expire="2027-01-01T00:00:00.000Z"),
        ]

        found = await catalog_api._router_accounts_by_client()

        assert found[1].short_uuid == "new"


class TestPanelTroubleDoesNotBreakTheMirror:
    @pytest.mark.asyncio
    async def test_unconfigured_panel(self, panel):
        panel.configured = False
        panel.accounts = [_account(1, "tg1_aa-aa", short="k")]

        assert await catalog_api._router_accounts_by_client() == {}

    @pytest.mark.asyncio
    async def test_silent_panel(self, panel):
        panel.fail = True

        assert await catalog_api._router_accounts_by_client() == {}


@pytest.mark.asyncio
async def test_snapshot_rows_carry_the_key(panel):
    """Срок и ключ едут одной строкой: бот пишет их в одну строку клиента."""
    panel.accounts = [_account(614685408, "tg614685408_d4-0d-ab-2b-a4-ee", short="k1")]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[User.__table__, Subscription.__table__]
                )
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add_all([User(id=1, tg_id=614685408), User(id=2, tg_id=777)])
            until = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
            session.add_all([
                Subscription(id=1, user_id=1, status=SubscriptionStatus.ACTIVE, expires_at=until),
                Subscription(id=2, user_id=2, status=SubscriptionStatus.ACTIVE, expires_at=until),
            ])
            await session.commit()

            rows = {r["tg_id"]: r for r in (await catalog_api.subscriptions_snapshot(session))["subscriptions"]}

        assert rows[614685408]["panel_short_uuid"] == "k1"
        assert rows[614685408]["panel_username"] == "tg614685408_d4-0d-ab-2b-a4-ee"
        # Клиент без учётки в панели: срок есть, ключа нет — и это не ошибка.
        assert rows[777]["panel_short_uuid"] == ""
        assert rows[777]["until"] is not None
    finally:
        await engine.dispose()


def test_parse_reads_the_short_key():
    """Панель зовёт его shortUuid; без разбора этого поля ключ не доедет никуда."""
    assert RemnaUser.parse({"uuid": "u", "username": "x", "shortUuid": "abc"}).short_uuid == "abc"
