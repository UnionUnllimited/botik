"""Суммы заказа, промокоды, переходы статусов и валидация ввода."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from bot.utils import validators
from core.enums import OrderStatus, PromoDiscountType
from core.models import Order, PromoCode
from core.services import orders as order_service
from core.services import promo as promo_service


class TestPromoDiscount:
    def test_percent_discount(self):
        promo = PromoCode(code="SALE10", discount_type=PromoDiscountType.PERCENT, value=Decimal("10"))
        assert promo_service.calculate_discount(promo, Decimal("6900.00")) == Decimal("690.00")

    def test_fixed_discount(self):
        promo = PromoCode(code="MINUS500", discount_type=PromoDiscountType.FIXED, value=Decimal("500"))
        assert promo_service.calculate_discount(promo, Decimal("6900.00")) == Decimal("500.00")

    def test_discount_never_exceeds_order(self):
        promo = PromoCode(code="HUGE", discount_type=PromoDiscountType.FIXED, value=Decimal("99999"))
        assert promo_service.calculate_discount(promo, Decimal("399.00")) == Decimal("399.00")

    def test_percent_rounds_to_kopecks(self):
        promo = PromoCode(code="P33", discount_type=PromoDiscountType.PERCENT, value=Decimal("33"))
        discount = promo_service.calculate_discount(promo, Decimal("1090.00"))
        assert discount == Decimal("359.70")
        assert discount.as_tuple().exponent == -2

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("sale10", "SALE10"), (" sale 10 ", "SALE10"), ("Sale10", "SALE10")],
    )
    def test_code_normalization(self, raw, expected):
        assert promo_service.normalize_code(raw) == expected


class TestOrderNumber:
    def test_format(self):
        created = dt.datetime(2026, 8, 3, 9, tzinfo=dt.UTC)
        assert order_service.public_number(42, created) == "R-260803-0042"

    def test_uses_moscow_date(self):
        """21:30 UTC — это уже следующий день в Москве, номер должен совпадать с чеком."""
        created = dt.datetime(2026, 8, 3, 21, 30, tzinfo=dt.UTC)
        assert order_service.public_number(7, created) == "R-260804-0007"


class TestStatusTransitions:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (OrderStatus.NEW, OrderStatus.AWAITING_PAYMENT),
            (OrderStatus.NEW, OrderStatus.PACKING),
            (OrderStatus.AWAITING_PAYMENT, OrderStatus.PAID),
            (OrderStatus.PAID, OrderStatus.PACKING),
            (OrderStatus.PACKING, OrderStatus.SHIPPED),
            (OrderStatus.SHIPPED, OrderStatus.DELIVERED),
            (OrderStatus.DELIVERED, OrderStatus.DONE),
        ],
    )
    def test_allowed(self, current, target):
        assert order_service.can_transition(current, target) is True

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (OrderStatus.NEW, OrderStatus.SHIPPED),
            (OrderStatus.NEW, OrderStatus.DONE),
            (OrderStatus.CANCELLED, OrderStatus.PAID),
            (OrderStatus.REFUNDED, OrderStatus.PAID),
            (OrderStatus.SHIPPED, OrderStatus.PACKING),
            (OrderStatus.DONE, OrderStatus.SHIPPED),
        ],
    )
    def test_forbidden(self, current, target):
        assert order_service.can_transition(current, target) is False

    def test_set_status_marks_timestamps(self):
        order = Order(id=1, public_number="R-260803-0001", user_id=1, status=OrderStatus.PAID)
        order_service.set_status(order, OrderStatus.PACKING)
        order_service.set_status(order, OrderStatus.SHIPPED)
        assert order.status is OrderStatus.SHIPPED
        assert order.shipped_at is not None

    def test_invalid_transition_raises(self):
        order = Order(id=1, public_number="R-260803-0001", user_id=1, status=OrderStatus.NEW)
        with pytest.raises(order_service.OrderError):
            order_service.set_status(order, OrderStatus.DONE)

    def test_cancel_keeps_reason(self):
        order = Order(id=1, public_number="R-260803-0001", user_id=1, status=OrderStatus.PAID)
        order_service.set_status(order, OrderStatus.CANCELLED, reason="Клиент передумал")
        assert order.cancel_reason == "Клиент передумал"
        assert order.cancelled_at is not None


class TestValidators:
    @pytest.mark.parametrize(
        "raw",
        ["+7 900 123-45-67", "89001234567", "8 (900) 123-45-67", "+79001234567"],
    )
    def test_valid_russian_phones(self, raw):
        assert validators.clean_phone(raw) == "+79001234567"

    @pytest.mark.parametrize("raw", ["123", "", "+1 202 555 0143", "не телефон", "+7 900 123"])
    def test_invalid_phones(self, raw):
        assert validators.clean_phone(raw) == ""

    def test_phone_display_format(self):
        assert validators.format_phone("+79001234567") == "+7 900 123-45-67"

    @pytest.mark.parametrize("raw", ["Иванов Иван Иванович", "Петрова  Анна", "О'Коннор Джон"])
    def test_valid_names(self, raw):
        assert validators.clean_full_name(raw) != ""

    @pytest.mark.parametrize("raw", ["Иван", "", "12345 678", "<script>alert(1)</script>"])
    def test_invalid_names(self, raw):
        assert validators.clean_full_name(raw) == ""

    @pytest.mark.parametrize("raw", ["Москва", "Нижний Новгород", "Ростов-на-Дону"])
    def test_valid_cities(self, raw):
        assert validators.clean_city(raw) != ""

    @pytest.mark.parametrize("raw", ["", "1", "77777"])
    def test_invalid_cities(self, raw):
        assert validators.clean_city(raw) == ""

    def test_address_requires_house_number(self):
        assert validators.clean_address("улица Ленина дом 5 квартира 12") != ""
        assert validators.clean_address("улица Ленина") == ""
