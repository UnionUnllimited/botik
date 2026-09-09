"""Ключи роутерного клиента — ссылки подписки его роутеров из панели.

Вкладка «Ключи» в их админке просила ключи у службы xuiweb, которой на
сервере нет, и падала пятисоткой на каждом клиенте. У роутерного клиента
ключ — ссылка подписки учётки в панели, и её знает основное приложение.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes import fleet_api
from core.models import Device, User
from core.models.base import Base

KELVIN = 8152081864


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


async def _engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=[User.__table__, Device.__table__])
        )
    return engine


@pytest.fixture
def panel(monkeypatch):
    """Панель отвечает учёткой с ссылкой; прикрытие подменяет хост."""
    monkeypatch.setattr(type(fleet_api.settings.remnawave), "is_configured", property(lambda self: True))
    monkeypatch.setattr(fleet_api.settings.remnawave, "sub_public_host", "sub.example.com")
    until = dt.datetime(2026, 10, 3, 14, 6, tzinfo=dt.UTC)

    async def account_of(device):
        return SimpleNamespace(
            username=f"tg{KELVIN}_{device.mac.lower().replace(':', '-')}",
            subscription_url="https://panel.internal/api/sub/abc123",
            expire_at=until.isoformat(),
        )

    monkeypatch.setattr(fleet_api.activation, "panel_account_of", account_of)
    monkeypatch.setattr(fleet_api.activation, "panel_expiry_of", lambda account: until if account else None)
    return until


@pytest.mark.asyncio
async def test_keys_are_public_subscription_links_per_router(panel):
    engine = await _engine()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            kelvin = User(tg_id=KELVIN, username="kelvin")
            session.add(kelvin)
            await session.flush()
            session.add(Device(mac="D4:0D:AB:2B:A4:EE", user_id=kelvin.id, board="cudy,wr3000s-v1"))
            await session.commit()

            data = await fleet_api.client_keys(tg_id=KELVIN, session=session)

        assert data["has_client"] is True
        (key,) = data["keys"]
        assert key["mac"] == "D4:0D:AB:2B:A4:EE"
        # Хост подменён на прикрытие, путь и токен — панельные.
        assert key["subscription_url"] == "https://sub.example.com/api/sub/abc123"
        assert key["username"].startswith(f"tg{KELVIN}_")
        assert key["active"] is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stranger_has_no_keys(panel):
    engine = await _engine()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            data = await fleet_api.client_keys(tg_id=1, session=session)
        assert data == {"has_client": False, "keys": []}
    finally:
        await engine.dispose()


def test_their_tabs_ask_us_for_router_clients():
    """Обе вкладки в их админке для роутерного клиента идут к нам, а не в xuiweb."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "bot" / "web_admin" / "routes" / "api.py").read_text(
        encoding="utf-8"
    )
    keys = source[source.index("async def api_user_keys(") :]
    keys = keys[: keys.index("\nasync def ", 1)]
    assert "/api/v1/fleet/clients/{telegram_id}/keys" in keys

    security = source[source.index("async def api_user_security(") :]
    security = security[: security.index("\nasync def ", 1)]
    assert "shop_api.client_routers(telegram_id)" in security
