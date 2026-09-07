"""Профиль отдаёт ссылку «порекомендовать» и контакт поддержки.

Ссылка — их реферальная, `?start=<tg_id>`: бот записывает её в invited_by,
и приглашение засчитывается его механикой. Собирать здесь свою значило бы
завести второй учёт приглашений рядом с работающим.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from api.routes import miniapp
from core.config import settings
from tests.test_miniapp_auth import TOKEN, make_init_data

MINE = 614685408


@pytest.fixture
def quiet_catalog(monkeypatch):
    async def renew_state(*, tg_id, session):
        return {"has_client": True, "subscription": {"status": "active"}}

    async def my_router_available(*, tg_id, session):
        return {"show": True}

    async def list_orders(*, tg_id, limit, session):
        return {"orders": []}

    async def get_str(session, key):
        return "@TitanVPSHelp_bot" if key == "support.contact" else ""

    monkeypatch.setattr(miniapp.catalog_api, "renew_state", renew_state)
    monkeypatch.setattr(miniapp.catalog_api, "my_router_available", my_router_available)
    monkeypatch.setattr(miniapp.catalog_api, "list_orders", list_orders)
    monkeypatch.setattr(miniapp.settings_service, "get_str", get_str)
    monkeypatch.setattr(settings.miniapp, "bot_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings.miniapp, "allowed_tg_ids", [MINE])


@pytest.mark.asyncio
async def test_share_link_is_the_bots_referral_link(quiet_catalog, monkeypatch):
    monkeypatch.setattr(settings.app, "bot_username", "titan_routers_bot")

    user = await miniapp.current_user(init_data=make_init_data())
    data = await miniapp.home(user=user, session=None)

    assert data["share"]["url"] == f"https://t.me/titan_routers_bot?start={MINE}"
    assert data["share"]["text"] == miniapp.SHARE_TEXT
    assert data["support"] == "@TitanVPSHelp_bot"


@pytest.mark.asyncio
async def test_no_bot_name_means_no_link(quiet_catalog, monkeypatch):
    """Без имени бота ссылку собрать не из чего — и кнопки не будет."""
    monkeypatch.setattr(settings.app, "bot_username", "")

    user = await miniapp.current_user(init_data=make_init_data())
    data = await miniapp.home(user=user, session=None)

    assert data["share"]["url"] == ""


def test_share_text_has_no_forbidden_word():
    """Пересланное сообщение живёт в чужих чатах годами — слову там не место."""
    lowered = miniapp.SHARE_TEXT.lower()
    assert "vpn" not in lowered
    assert "впн" not in lowered
