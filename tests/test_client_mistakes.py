"""Что будет, если клиент нажмёт не то, дважды или пришлёт мусор.

Проверяется не «функция работает», а поведение на неверных действиях:
двойное нажатие, чужой номер заказа, битый ввод, мёртвая ссылка. Именно
здесь стоят деньги: лишний счёт, второй заказ или чужой адрес доставки
дороже любой опечатки в тексте.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core import validators
from core.enums import OrderItemType, OrderStatus
from core.models import Order, OrderItem, Product, User
from core.models.base import Base

ROOT = Path(__file__).resolve().parents[1]

BOT_CATALOG = (ROOT / "bot/src/router_catalog.py").read_text(encoding="utf-8")
BOT_MAIN = (ROOT / "bot/main.py").read_text(encoding="utf-8")
CATALOG_API = (ROOT / "api/routes/catalog_api.py").read_text(encoding="utf-8")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


async def _session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _order_with_product(session, *, minutes_ago: int = 0):
    """Клиент, товар и заказ на него — общая заготовка проверок про повторы."""
    user = User(tg_id=42)
    product = Product(slug="r1", title="Роутер", price=Decimal("6900.00"), stock=5)
    session.add_all([user, product])
    await session.flush()

    order = Order(
        public_number="R-1",
        user_id=user.id,
        status=OrderStatus.NEW,
        subtotal=Decimal("6900.00"),
        discount_total=Decimal("0.00"),
        delivery_price=Decimal("0.00"),
        total=Decimal("6900.00"),
        customer_name="Иванов Иван",
        customer_phone="+79001234567",
        customer_city="Москва",
        created_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago),
    )
    order.items.append(
        OrderItem(
            item_type=OrderItemType.PRODUCT,
            product_id=product.id,
            title="Роутер",
            quantity=1,
            unit_price=Decimal("6900.00"),
            total_price=Decimal("6900.00"),
        )
    )
    session.add(order)
    await session.commit()
    return user, product, order


def _between(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end)]


class TestClientInput:
    """Поля заказа: что человек пишет на самом деле."""

    @pytest.mark.parametrize(
        "raw",
        ["89001234567", "+7 900 123-45-67", "8 (900) 123 45 67"],
    )
    def test_phone_is_accepted_however_it_is_written(self, raw):
        assert validators.clean_phone(raw) == "+79001234567"

    @pytest.mark.parametrize("raw", ["123", "+7900", "не помню", "", "+1 202 555 0173"])
    def test_junk_phone_is_refused(self, raw):
        """Номер, на который не дозвонится курьер, дороже лишнего шага."""
        assert validators.clean_phone(raw) == ""

    @pytest.mark.parametrize("raw", ["Иванов", "И", "12345", "<b>Иванов</b> Иван"])
    def test_broken_name_is_refused(self, raw):
        assert validators.clean_full_name(raw) == ""

    def test_name_survives_double_spaces(self):
        assert validators.clean_full_name("  Иванов   Иван  ") == "Иванов Иван"

    def test_address_needs_a_house_number(self):
        """«Улица Ленина» без дома — посылка не доедет."""
        assert validators.clean_address("Улица Ленина") == ""
        assert validators.clean_address("Улица Ленина, дом 5, кв 12") != ""

    def test_city_refuses_markup(self):
        assert validators.clean_city("<script>alert(1)</script>") == ""

    def test_long_input_is_cut(self):
        """Мегабайтная строка не должна доехать до базы целиком."""
        assert len(validators.clean_address("Ленина 5 " + "я" * 5000)) <= 500

    def test_promo_code_is_normalised_and_cut(self):
        """Клиент шлёт код как получится — с пробелами, в нижнем регистре.
        Длина обрезается: в поиск по базе не должна уезжать простыня."""
        from core.services.promo import normalize_code

        assert normalize_code("  router 10  ") == "ROUTER10"
        assert len(normalize_code("к" * 500)) <= 32


class TestDoubleTap:
    """Двойное нажатие — самое частое неверное действие клиента."""

    def _confirm(self) -> str:
        return _between(BOT_CATALOG, "async def cq_confirm", "async def cq_step_back")

    def test_order_draft_is_taken_before_the_request(self):
        """Оформление идёт до провайдера и занимает секунды: за это время
        клиент успевает нажать второй раз."""
        body = self._confirm()
        assert body.index("state.update_data(product_id=None)") < body.index(
            "shop_api.create_order"
        ), "занимать черновик надо до запроса, а не после"

    def test_draft_returns_on_failure(self):
        """Иначе неудачная попытка запирает клиента в «уже оформлен»."""
        assert 'state.update_data(product_id=data.get("product_id"))' in self._confirm()

    @pytest.mark.asyncio
    async def test_same_order_twice_returns_the_first(self):
        from api.routes.catalog_api import _recent_twin
        from core.services.orders import OrderDraft

        engine, factory = await _session()
        try:
            async with factory() as session:
                user, product, order = await _order_with_product(session)
                twin = await _recent_twin(
                    session, user=user, draft=OrderDraft(product_id=product.id)
                )
                assert twin is not None
                assert twin.id == order.id
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_old_order_is_not_a_twin(self):
        """Через час это уже второй роутер, а не повтор нажатия."""
        from api.routes.catalog_api import _recent_twin
        from core.services.orders import OrderDraft

        engine, factory = await _session()
        try:
            async with factory() as session:
                user, product, _ = await _order_with_product(session, minutes_ago=60)
                twin = await _recent_twin(
                    session, user=user, draft=OrderDraft(product_id=product.id)
                )
                assert twin is None
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_another_model_is_not_a_twin(self):
        """Купить второй роутер другой модели никто не запрещает."""
        from api.routes.catalog_api import _recent_twin
        from core.services.orders import OrderDraft

        engine, factory = await _session()
        try:
            async with factory() as session:
                user, product, _ = await _order_with_product(session)
                twin = await _recent_twin(
                    session, user=user, draft=OrderDraft(product_id=product.id + 100)
                )
                assert twin is None
        finally:
            await engine.dispose()


class TestDeliveryPaymentIsNotDoubled:
    """Кнопку «Оплатить доставку» жмут по нескольку раз."""

    def _handler(self) -> str:
        return _between(
            CATALOG_API, "async def delivery_payment_link", "async def cancel_order"
        )

    def test_live_invoice_is_reused(self):
        body = self._handler()
        assert "_alive_payment(" in body
        assert "catalog.delivery_link_reused" in body

    def test_expired_invoice_is_not_reused(self):
        """Мёртвая ссылка хуже отсутствия: клиент нажмёт и упрётся в отказ.

        Условие живёт в общем помощнике: искать живой счёт двумя способами
        значит однажды поправить только один из них.
        """
        helper = _between(CATALOG_API, "async def _alive_payment", "@router.post(\"/renew\")")
        assert "Payment.expires_at > utcnow()" in helper
        assert "PaymentStatus.PENDING" in helper

    def test_renewal_reuses_it_too(self):
        """Продление жмут так же дважды, как и оплату доставки."""
        body = _between(CATALOG_API, "async def renew_start", "def tg_id_of")
        assert "_alive_payment(" in body
        assert "catalog.renew_link_reused" in body

    def test_paid_delivery_is_refused(self):
        assert "уже оплачена" in self._handler()

    def test_unquoted_delivery_is_refused(self):
        """Пока цену не назвали, платить не за что."""
        assert "ещё не посчитана" in self._handler()


class TestSomebodyElsesOrder:
    """Номер заказа — не пароль: он виден в переписке и в чеке."""

    @pytest.mark.parametrize(
        "handler",
        ["async def order_card", "async def delivery_payment_link", "async def cancel_order"],
    )
    def test_every_client_handler_checks_the_owner(self, handler):
        body = CATALOG_API[CATALOG_API.index(handler) :][:1600]
        assert "user.tg_id != tg_id" in body, handler

    def test_paid_order_is_not_cancelled_by_the_client(self):
        """Отмена оплаченного — это возврат денег, его делает поддержка."""
        from api.routes.catalog_api import _CANCELLABLE

        assert OrderStatus.PAID not in _CANCELLABLE
        assert OrderStatus.SHIPPED not in _CANCELLABLE

    def test_router_screen_picks_from_own_devices(self):
        """`device_id` из чужой ссылки не должен открывать чужой роутер."""
        body = _between(CATALOG_API, "async def my_router", "async def subscriptions_snapshot")
        assert "Device.user_id == user.id" in body
        assert "next((d for d in devices if d.id == device_id), devices[0])" in body


class TestSomebodyElsesRouter:
    """MAC написан на корпусе — его видно всем, кто держал коробку."""

    SOURCE = (ROOT / "core/services/activation.py").read_text(encoding="utf-8")

    def test_foreign_router_is_refused(self):
        body = _between(self.SOURCE, "async def _resolve_device", "async def _pending_subscription")
        assert "device.user_id != user.id" in body

    def test_blocked_router_is_refused(self):
        body = _between(self.SOURCE, "async def _resolve_device", "async def _pending_subscription")
        assert "DeviceStatus.BLOCKED" in body

    def test_guessing_is_rate_limited(self):
        """Иначе MAC подбирается перебором: 12 знаков и известный префикс."""
        assert "_check_rate_limit" in self.SOURCE


class TestBrokenLinksAndScreens:
    """Клиент приходит по ссылке из браузера — она может быть какой угодно."""

    def test_buy_link_takes_digits_only(self):
        body = BOT_MAIN[BOT_MAIN.index("wanted_product_id = None") :][:600]
        assert "arg[4:].isdigit()" in body

    def test_missing_product_does_not_break_start(self):
        """Ссылка на снятую с продажи модель не должна ломать вход в бота."""
        body = _between(BOT_MAIN, "async def show_wanted_product", "# --- Логирование входа")
        assert "except Exception" in body

    def test_empty_pay_url_gives_no_dead_button(self):
        body = _between(BOT_CATALOG, "def created_keyboard", "# --- Экран «Мой роутер»")
        assert "if pay_url:" in body

    def test_catalog_failure_shows_a_screen_not_silence(self):
        """Каталог не ответил — клиент видит объяснение и кнопку назад."""
        body = _between(BOT_CATALOG, "async def show_error", "async def show_catalog")
        assert "text_catalog_unavailable" in body
        assert "btn_back_to_main" in body

    def test_out_of_stock_is_refused_at_the_last_step(self):
        """Между открытием карточки и нажатием «Заказать» товар мог кончиться."""
        body = _between(BOT_CATALOG, "async def cq_buy", "async def cq_plan")
        assert 'product.get("in_stock")' in body


class TestCancelledOrderIsNotPaidByAccident:
    """Клиент отменил заказ, а ссылка на оплату у него в переписке осталась.

    Гасить её у провайдера нечем — метода отмены у него нет, есть только
    возврат. Значит, во-первых, свой счёт мы закрываем сами (иначе его
    переиспользует кнопка «Оплатить заказ» и опрашивает воркер), а во-вторых,
    пришедшую по нему оплату не превращаем в подписку молча: заказ отменён,
    роутер никто не повезёт, и решать тут человеку.
    """

    @staticmethod
    async def _paid_cancelled_order(session):
        from core.enums import PaymentProviderName, PaymentPurpose, PaymentStatus
        from core.models import Payment

        user, _product, order = await _order_with_product(session)
        order.status = OrderStatus.CANCELLED
        order.cancelled_at = dt.datetime.now(dt.UTC)
        payment = Payment(
            user_id=user.id,
            order_id=order.id,
            provider=PaymentProviderName.PLATEGA,
            purpose=PaymentPurpose.ORDER,
            status=PaymentStatus.PENDING,
            idempotency_key="key-cancelled",
            amount=order.total,
            currency="RUB",
        )
        session.add(payment)
        await session.commit()
        return user, order, payment

    @pytest.mark.asyncio
    async def test_cancelling_closes_the_invoice(self):
        """Свой счёт закрываем сразу: иначе он живой для кнопки и для опроса."""
        from api.routes import catalog_api
        from core.enums import PaymentStatus

        engine, factory = await _session()
        try:
            async with factory() as session:
                user, _product, order = await _order_with_product(session)
                from core.enums import PaymentProviderName, PaymentPurpose
                from core.models import Payment

                session.add(
                    Payment(
                        user_id=user.id,
                        order_id=order.id,
                        provider=PaymentProviderName.PLATEGA,
                        purpose=PaymentPurpose.ORDER,
                        status=PaymentStatus.PENDING,
                        idempotency_key="key-1",
                        amount=order.total,
                        currency="RUB",
                        confirmation_url="https://pay.example/1",
                        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10),
                    )
                )
                await session.commit()

                result = await catalog_api.cancel_order(
                    order.id, {"tg_id": user.tg_id}, session=session
                )
                await session.commit()

                assert result == {"ok": True}
                left = await catalog_api._alive_payment(
                    session,
                    purpose=PaymentPurpose.ORDER,
                    amount=order.total,
                    order_id=order.id,
                )
                assert left is None, "счёт отменённого заказа остался живым"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_payment_of_a_cancelled_order_grants_nothing(self, monkeypatch):
        """Оплата дошла — подписки быть не должно: заказ отменён."""
        from sqlalchemy import select

        from core.enums import PaymentStatus
        from core.models import Subscription
        from core.services import payments as payment_service

        engine, factory = await _session()
        try:
            async with factory() as session:
                _user, order, payment = await self._paid_cancelled_order(session)

                told: list = []

                async def _alert(text, *, session=None, reply_markup=None):
                    told.append(text)

                monkeypatch.setattr(payment_service, "notify_admins", _alert, raising=False)

                await payment_service.apply_status(
                    session, payment, status=PaymentStatus.SUCCEEDED
                )
                await session.commit()

                granted = list(
                    await session.scalars(
                        select(Subscription).where(Subscription.order_id == order.id)
                    )
                )
                assert granted == [], "за отменённый заказ выдали подписку"
                assert payment.status is PaymentStatus.SUCCEEDED, (
                    "деньги пришли — платёж обязан остаться в сверке"
                )
                assert told, "оператору не сказали, что оплатили отменённый заказ"
        finally:
            await engine.dispose()


class TestRenewalInvoiceMatchesTheChosenPeriod:
    """Живой счёт переиспользуется по сумме — и по сроку тоже.

    Сроки приезжают зеркалом из чужой админки, и два разных срока с одной
    ценой там обычное дело. Клиент, выбравший второй, получал ссылку
    на первый и оплачивал не то, что выбрал.
    """

    def test_alive_invoice_checks_the_plan(self):
        helper = _between(CATALOG_API, "async def _alive_payment", '@router.post("/renew")')
        assert "Payment.plan_id == plan_id" in helper, (
            "счёт продления обязан совпадать не только ценой, но и сроком"
        )
