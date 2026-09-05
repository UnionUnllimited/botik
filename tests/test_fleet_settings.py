"""Контакт поддержки правится из админки парка.

Он живёт в наших настройках, и до сих пор писать в них было неоткуда:
кнопка «написать в поддержку» в приложении, строка «остались вопросы»
в каталоге и ссылка в подвале сайта молчали все разом, а оператор не мог
этого изменить иначе как запросом в базу.
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes import fleet_api
from core.models import Setting
from core.models.base import Base


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=[Setting.__table__])
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_read_returns_the_contact_next_to_activation():
    engine, factory = await _session_factory()
    try:
        async with factory() as session:
            session.add(Setting(key="support.contact", value={"value": "@titan_support"}))
            await session.commit()

            answer = await fleet_api.fleet_settings_read(session=session)

        assert answer["support_contact"] == "@titan_support"
        assert "auto_enabled" in answer
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_save_writes_the_contact_trimmed(monkeypatch):
    written: dict = {}

    async def remember(session, key, value, **kwargs):
        written[key] = value

    monkeypatch.setattr(fleet_api.settings_service, "set_setting", remember)

    await fleet_api.fleet_settings_save(
        payload={"auto_enabled": True, "support_contact": "  @titan_support "}, session=None
    )

    assert written["support.contact"] == "@titan_support"
    assert written["activation.auto_enabled"] is True


@pytest.mark.asyncio
async def test_save_without_the_field_leaves_the_contact_alone(monkeypatch):
    """Старая форма шлёт только автоактивацию — контакт от этого стираться не должен."""
    written: dict = {}

    async def remember(session, key, value, **kwargs):
        written[key] = value

    monkeypatch.setattr(fleet_api.settings_service, "set_setting", remember)

    await fleet_api.fleet_settings_save(payload={"auto_enabled": False}, session=None)

    assert "support.contact" not in written
