"""Карточка клиента в админке бота: роутеры, их подписки, заказы и действия.

Раньше блок показывал роутеры и одну «подписку клиента» на всех. У владельца
двух роутеров она говорила «активна» про оба, хотя доступ был только на одном.
Теперь у каждого роутера своя подписка, рядом — заказы и контакты, а кнопки
«Опросить», «Перезагрузить», «Продлить» видит только тот, кому роутеры выданы.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes import fleet_api
from core.enums import OrderStatus, SubscriptionStatus
from core.models import Device, Order, Plan, Subscription, User
from core.models.base import Base

BASE = (Path(__file__).resolve().parents[1] / "bot" / "web_admin" / "templates" / "base.html").read_text(
    encoding="utf-8"
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


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
                ],
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_each_router_has_its_own_subscription(monkeypatch):
    monkeypatch.setattr(fleet_api.settings.remnawave, "base_url", "", raising=False)
    engine, factory = await _world()
    now = dt.datetime.now(dt.UTC)
    try:
        async with factory() as session:
            user = User(tg_id=614685408, username="union_unlimited", full_name="Перт", phone="+79001234567")
            session.add(user)
            await session.flush()
            paid = Device(mac="D4:0D:AB:03:5F:A8", user_id=user.id)
            second = Device(mac="F8:5E:3C:92:C0:22", user_id=user.id)
            session.add_all([paid, second])
            await session.flush()
            session.add(
                Subscription(
                    user_id=user.id,
                    device_id=paid.id,
                    status=SubscriptionStatus.ACTIVE,
                    expires_at=now + dt.timedelta(days=68, hours=1),
                )
            )
            session.add(
                Order(
                    public_number="R-260811-0002",
                    user_id=user.id,
                    status=OrderStatus.SHIPPED,
                    subtotal=Decimal("6900"),
                    discount_total=Decimal("0"),
                    delivery_price=Decimal("0"),
                    total=Decimal("6900"),
                    customer_name="",
                    customer_phone="",
                    customer_city="",
                    paid_at=now,
                )
            )
            await session.commit()

            data = await fleet_api.client_routers(614685408, session)

        by_mac = {row["mac"]: row for row in data["routers"]}
        assert by_mac["D4:0D:AB:03:5F:A8"]["subscription"]["status"] == "active"
        assert by_mac["D4:0D:AB:03:5F:A8"]["subscription"]["days_left"] == 68
        # Второй роутер того же клиента — без подписки, а не «активна».
        assert by_mac["F8:5E:3C:92:C0:22"]["subscription"]["status"] == ""

        assert [order["number"] for order in data["orders"]] == ["R-260811-0002"]
        assert data["orders"][0]["paid"] is True
        assert data["orders"][0]["status_label"] == "Отправлен"
        assert data["client"]["username"] == "union_unlimited"
        assert data["client"]["phone"] == "+79001234567"
    finally:
        await engine.dispose()


class TestCardScript:
    BLOCK = BASE[
        BASE.index("window.umLoadRouters") : BASE.index("document.addEventListener('click', function (e) {")
    ]

    def test_subscription_is_read_per_router(self):
        assert "item.subscription" in self.BLOCK

    def test_actions_are_the_router_card_routes(self):
        for path in ("/poll'", "/quick/reboot'", "/extend'"):
            assert path in self.BLOCK, path

    def test_moderator_does_not_get_buttons_into_refusals(self):
        assert "moderator_can_see('devices')" in self.BLOCK
        assert "!canFleet ? ''" in self.BLOCK

    def test_reboot_asks_first(self):
        head = self.BLOCK.index("/quick/reboot'")
        assert "Клиент потеряет связь" in self.BLOCK[head : head + 300]
