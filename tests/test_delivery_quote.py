"""Доставка: клиент выбирает скорость, цену называет оператор.

Тарифные зоны прожили неделю и оказались не тем: цену по ним всё равно
перебивали руками, а город, которого в зонах не нашлось, останавливал
оформление у живого клиента. Решение заказчика от 21 августа 2026 — считать
доставку вручную по каждому заказу.

Опасных мест здесь два. Первое: ноль в цене — законная цена (доставку можно
подарить), и отличить его от «ещё не считали» можно только по отметке.
Второе: доставка оплачивается вторым платежом по тому же заказу, и если
пустить его общим путём, клиент получит вторую подписку за оплату перевозки.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from core import texts
from core.enums import DeliverySpeed, PaymentPurpose
from core.models import Delivery
from core.services import delivery as delivery_service

ROOT = Path(__file__).resolve().parents[1]


class TestAwaitingQuote:
    def test_new_delivery_waits_for_a_price(self):
        assert delivery_service.awaiting_quote(Delivery()) is True

    def test_no_delivery_waits_for_nothing(self):
        """Заказ без доставки не должен вечно висеть в «ждёт цены»."""
        assert delivery_service.awaiting_quote(None) is False

    def test_free_delivery_is_quoted(self):
        """Ноль — «бесплатно», а не «не считали». Разница в отметке."""
        delivery = Delivery(price=Decimal("0.00"))
        delivery_service.set_quote(delivery, Decimal("0.00"))
        assert delivery_service.awaiting_quote(delivery) is False

    def test_quote_records_price_and_moment(self):
        delivery = Delivery()
        delivery_service.set_quote(delivery, Decimal("450.00"))
        assert delivery.price == Decimal("450.00")
        assert delivery.quoted_at is not None

    def test_price_alone_does_not_count_as_quoted(self):
        """Если бы смотрели на цену, доставка за ноль ушла бы в сборку
        неоплаченной, а клиент так и не получил бы счёт."""
        assert delivery_service.awaiting_quote(Delivery(price=Decimal("0.00"))) is True


class TestSpeedChoice:
    @pytest.mark.parametrize(
        ("written", "expected"),
        [("fast", DeliverySpeed.FAST), ("weekly", DeliverySpeed.WEEKLY)],
    )
    def test_known_speed(self, written, expected):
        assert delivery_service.parse_speed(written) is expected

    @pytest.mark.parametrize("written", ["", "teleport", "FAST ", None])
    def test_unknown_speed_is_none(self, written):
        """Опечатка в чужом запросе оформит заказ без доставки, но не уронит его."""
        assert delivery_service.parse_speed(written) is None

    def test_both_options_are_offered(self):
        assert set(delivery_service.DEFAULT_SPEED_TITLES) == {
            DeliverySpeed.FAST,
            DeliverySpeed.WEEKLY,
        }

    def test_weekly_says_when_it_leaves(self):
        """«Дешевле» без «ждать до понедельника» — обещание, которое не сдержим."""
        assert "понедельник" in delivery_service.DEFAULT_SPEED_DESCRIPTIONS[DeliverySpeed.WEEKLY]


class TestQuoteMessage:
    def test_invoice_names_the_price(self):
        filled = texts.DELIVERY_QUOTE.format(number="R-1", price="450", days="3–5 дней")
        assert "450" in filled and "R-1" in filled

    def test_invoice_says_the_goods_are_already_paid(self):
        """Иначе второй счёт читается как «с меня требуют дважды»."""
        filled = texts.DELIVERY_QUOTE.format(number="R-1", price="450", days="")
        assert "уже оплачен" in filled

    def test_free_delivery_asks_for_nothing(self):
        filled = texts.DELIVERY_FREE.format(number="R-1")
        assert "Платить ничего не нужно" in filled


class TestSecondPaymentIsSeparate:
    """Второй платёж по заказу не должен пройти общим путём."""

    def _source(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_delivery_purpose_exists(self):
        assert PaymentPurpose.DELIVERY.value == "delivery"

    def test_success_handler_branches_before_the_plan_lookup(self):
        """Ниже по общему пути тариф ищется по составу заказа — и клиент
        получил бы вторую подписку за оплату перевозки."""
        source = self._source("core/services/payments.py")
        body = source[source.index("async def _apply_success") :]
        body = body[: body.index("async def _resolve_plan")]
        branch = body.index("PaymentPurpose.DELIVERY")
        plan_lookup = body.index("_resolve_plan")
        assert branch < plan_lookup, "ветка доставки должна стоять до поиска тарифа"
        assert "return" in body[branch:plan_lookup], "и выходить из обработчика, а не проваливаться дальше"

    def test_delivery_payment_marks_the_delivery_paid(self):
        source = self._source("core/services/payments.py")
        assert "order.delivery.paid_at = payment.paid_at" in source


class TestOrderTotalsExcludeDelivery:
    """Сумма заказа — только роутер и подписка: доставку оплатят отдельно."""

    def _source(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_totals_do_not_call_a_price_calculator(self):
        source = self._source("core/services/orders.py")
        assert "calculate_price" not in source

    def test_no_delivery_line_in_the_order(self):
        """Строка на ноль рублей читалась бы как «доставка бесплатная»."""
        source = self._source("core/services/orders.py")
        assert "OrderItemType.DELIVERY" not in source

    def test_delivery_is_attached_with_zero_price(self):
        source = self._source("core/services/delivery.py")
        body = source[source.index("def attach_delivery") :]
        assert 'price=Decimal("0.00")' in body


class TestZonesAreGone:
    def test_no_zone_code_left(self):
        for relative in (
            "core/services/delivery.py",
            "core/services/orders.py",
            "api/routes/catalog_api.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            assert "DeliveryZone" not in source
            assert "UnknownCity" not in source

    def test_migration_drops_the_tables(self):
        source = (ROOT / "migrations/versions/0013_delivery_speed.py").read_text(encoding="utf-8")
        for table in ("delivery_zones", "delivery_zone_prices", "delivery_unknown_cities"):
            assert f'drop_table("{table}")' in source

    def test_paid_deliveries_are_not_billed_again(self):
        """У заказов, оформленных при зонах, доставка уже в оплаченной сумме."""
        source = (ROOT / "migrations/versions/0013_delivery_speed.py").read_text(encoding="utf-8")
        assert "quoted_at = created_at, paid_at = created_at WHERE price > 0" in source


class TestDeliverySummary:
    def test_speed_comes_first(self):
        from core.enums import DeliveryMethod
        from core.services.orders import delivery_summary

        delivery = Delivery(
            method=DeliveryMethod.CDEK,
            speed=DeliverySpeed.WEEKLY,
            city="Самара",
            address="Ленина 1",
        )
        assert delivery_summary(delivery).startswith("по понедельникам")

    def test_no_delivery_reads_as_a_dash(self):
        from core.services.orders import delivery_summary

        assert delivery_summary(None) == "—"


def test_quote_sets_the_moment_it_was_given():
    delivery = Delivery()
    before = dt.datetime.now(dt.UTC)
    delivery_service.set_quote(delivery, Decimal("100"))
    assert delivery.quoted_at >= before


class TestDeliveryState:
    """Состояние доставки — отдельная величина, а не статус заказа.

    Одним статусом это не выражается: заказ к этому моменту «Оплачен» —
    роутер и подписку клиент купил, — и при этом ждёт денег за перевозку.
    """

    def test_no_delivery(self):
        assert delivery_service.state(None) == delivery_service.NO_DELIVERY

    def test_fresh_delivery_is_not_quoted(self):
        assert delivery_service.state(Delivery()) == delivery_service.NOT_QUOTED

    def test_quoted_delivery_awaits_payment(self):
        delivery = Delivery()
        delivery_service.set_quote(delivery, Decimal("450.00"))
        assert delivery_service.state(delivery) == delivery_service.AWAITING_PAYMENT

    def test_paid_delivery_is_paid(self):
        delivery = Delivery()
        delivery_service.set_quote(delivery, Decimal("450.00"))
        delivery.paid_at = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)
        assert delivery_service.state(delivery) == delivery_service.PAID

    def test_free_delivery_never_waits_for_money(self):
        """Дарёную доставку платить не за что: иначе заказ завис бы навсегда."""
        delivery = Delivery()
        delivery_service.set_quote(delivery, Decimal("0.00"))
        delivery.paid_at = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)
        assert delivery_service.state(delivery) == delivery_service.PAID


class TestDeliveryFilter:
    """Отбор «кто не оплатил перевозку» идёт тем же полем, что и статус."""

    def test_awaiting_payment_filter_is_recognised(self):
        from api.routes.catalog_api import DELIVERY_FILTER_PREFIX, _delivery_condition

        condition = _delivery_condition(f"{DELIVERY_FILTER_PREFIX}awaiting_payment")
        assert condition is not None

    def test_not_quoted_filter_is_recognised(self):
        from api.routes.catalog_api import DELIVERY_FILTER_PREFIX, _delivery_condition

        assert _delivery_condition(f"{DELIVERY_FILTER_PREFIX}not_quoted") is not None

    def test_order_status_is_left_alone(self):
        from api.routes.catalog_api import _delivery_condition

        assert _delivery_condition("paid") is None

    def test_unknown_delivery_state_shows_nothing(self):
        """Иначе опечатка в адресе показала бы все заказы подряд как отбор."""
        from api.routes.catalog_api import DELIVERY_FILTER_PREFIX, _status_condition

        condition = _status_condition(f"{DELIVERY_FILTER_PREFIX}teleported")
        assert condition is not None
        assert "IS NULL" in str(condition.compile(compile_kwargs={"literal_binds": True}))

    def test_empty_filter_means_everything(self):
        from api.routes.catalog_api import _status_condition

        assert _status_condition("") is None


class TestCarrierIsChosenInTheOrder:
    """Перевозчика ставит оператор в заказе: клиент выбирал только скорость."""

    def _source(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_quote_accepts_the_carrier(self):
        source = self._source("api/routes/catalog_api.py")
        body = source[source.index("async def manage_delivery_quote") :]
        body = body[: body.index("delivery_service.set_quote")]
        assert 'payload.get("method"' in body
        assert "OFFERED_DELIVERY_METHODS" in body

    def test_retired_carrier_is_refused(self):
        """Самовывоз и Boxberry остались ради старых заказов, а не для новых."""
        source = self._source("api/routes/catalog_api.py")
        body = source[source.index("async def manage_delivery_quote") :]
        body = body[: body.index("delivery_service.set_quote")]
        assert "not in OFFERED_DELIVERY_METHODS" in body

    def test_delivery_page_is_gone(self):
        """Страница с прейскурантом падала пятисоткой: считавшую её функцию
        удалили вместе с зонами, а ручку оставили."""
        source = self._source("api/routes/catalog_api.py")
        assert '"/manage/delivery"' not in source
        assert not (ROOT / "bot/web_admin/templates/catalog_delivery.html").exists()

    def test_carrier_names_are_human(self):
        from core.enums import DeliveryMethod
        from core.services.orders import delivery_summary

        delivery = Delivery(
            method=DeliveryMethod.CDEK, speed=DeliverySpeed.FAST, city="Самара"
        )
        assert "СДЭК" in delivery_summary(delivery)
        assert "CDEK" not in delivery_summary(delivery)
