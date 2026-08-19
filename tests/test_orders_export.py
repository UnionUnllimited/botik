"""Выгрузка заказов в CSV.

Выгрузку открывают в таблице и по ней сверяют отгрузки с деньгами. Съехавшая
на одну колонка тут хуже пустой строки: телефон окажется в графе города,
и ошибку заметят не сразу, а после того, как по нему позвонят.

Файл собирается на нашей стороне и отдаётся браузеру байт в байт: разбери его
админка бота по дороге — сломались бы и BOM, на который смотрит Excel,
и переносы строк внутри адресов.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal
from pathlib import Path

import pytest

from api.routes.catalog_api import ORDERS_CSV_COLUMNS, _csv_moment, _order_csv_row
from core.enums import DeliveryMethod, OrderItemType, OrderStatus
from core.models import Delivery, Order, OrderItem

CARRIERS = {DeliveryMethod.CDEK: "СДЭК", DeliveryMethod.YANDEX: "Яндекс Go"}


def make_order(**overrides) -> Order:
    fields = {
        "public_number": "R-260819-0001",
        "status": OrderStatus.PAID,
        "customer_name": "Иванов Иван",
        "customer_phone": "+79001234567",
        "customer_city": "Самара",
        "subtotal": Decimal("6900.00"),
        "discount_total": Decimal("0.00"),
        "delivery_price": Decimal("350.00"),
        "total": Decimal("7250.00"),
        "created_at": dt.datetime(2026, 8, 19, 7, 0, tzinfo=dt.UTC),
    }
    fields.update(overrides)
    order = Order(**fields)
    order.items = [
        OrderItem(
            title="Роутер AX3000",
            quantity=1,
            item_type=OrderItemType.PRODUCT,
            total_price=Decimal("6900.00"),
        ),
        OrderItem(
            title="Доставка",
            quantity=1,
            item_type=OrderItemType.DELIVERY,
            total_price=Decimal("350.00"),
        ),
    ]
    order.delivery = Delivery(
        method=DeliveryMethod.CDEK,
        city="Самара",
        address="Ленина 1",
        recipient_name="Иванов Иван",
        recipient_phone="+79001234567",
        price=Decimal("350.00"),
    )
    return order


def row_as_dict(order: Order) -> dict[str, str]:
    return dict(zip(ORDERS_CSV_COLUMNS, _order_csv_row(order, CARRIERS), strict=True))


class TestRowMatchesHeader:
    def test_every_column_is_filled(self):
        """`strict=True` упадёт, если строка и шапка разной длины."""
        assert len(row_as_dict(make_order())) == len(ORDERS_CSV_COLUMNS)

    def test_values_land_in_their_own_columns(self):
        row = row_as_dict(make_order())
        assert row["Номер"] == "R-260819-0001"
        assert row["Телефон"] == "+79001234567"
        assert row["Город"] == "Самара"
        assert row["Итого, ₽"] == "7250.00"

    def test_status_is_a_word_not_a_code(self):
        assert row_as_dict(make_order())["Статус"] == "Оплачен"

    def test_carrier_is_named_as_the_operator_named_it(self):
        assert row_as_dict(make_order())["Доставка"] == "СДЭК"

    def test_address_is_not_repeated_in_the_carrier_column(self):
        row = row_as_dict(make_order())
        assert row["Адрес"] == "Ленина 1"
        assert "Ленина" not in row["Доставка"]


class TestComposition:
    def test_delivery_is_not_counted_twice(self):
        """У доставки своя колонка с ценой; в составе она читалась бы как товар."""
        row = row_as_dict(make_order())
        assert row["Состав"] == "Роутер AX3000 × 1"
        assert row["Доставка, ₽"] == "350.00"

    def test_order_without_delivery_leaves_blanks(self):
        order = make_order()
        order.delivery = None
        row = row_as_dict(order)
        assert row["Доставка"] == ""
        assert row["Адрес"] == ""
        assert row["Трек-номер"] == ""


class TestDates:
    def test_moscow_time_not_utc(self):
        """В базе UTC, на экранах админки московское — выгрузка не должна спорить."""
        assert _csv_moment(dt.datetime(2026, 8, 19, 7, 0, tzinfo=dt.UTC)) == "19.08.2026 10:00"

    def test_empty_date_stays_empty(self):
        assert _csv_moment(None) == ""

    def test_unpaid_order_has_no_payment_date(self):
        assert row_as_dict(make_order())["Оплачен"] == ""


class TestSeparatorsInsideValues:
    """Точка с запятой — разделитель колонок, и в адресе она встречается сама."""

    @pytest.mark.parametrize("dangerous", ["Самара; Кировский", 'Дом "У реки"', "Дом 1\nкв 2"])
    def test_value_survives_the_round_trip(self, dangerous):
        order = make_order(customer_city=dangerous)
        buffer = io.StringIO()
        csv.writer(buffer, delimiter=";", lineterminator="\r\n").writerow(
            _order_csv_row(order, CARRIERS)
        )
        parsed = next(csv.reader(io.StringIO(buffer.getvalue()), delimiter=";"))
        assert parsed[ORDERS_CSV_COLUMNS.index("Город")] == dangerous


class TestFileIsGivenToTheBrowserWhole:
    """Админка бота не разбирает файл, а передаёт его как есть."""

    def _source(self, relative: str) -> str:
        return (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")

    def test_excel_gets_its_bom(self):
        """Без BOM Excel показывает кириллицу кракозябрами."""
        assert '"﻿" +' in self._source("api/routes/catalog_api.py")

    def test_admin_does_not_decode_the_file(self):
        source = self._source("bot/web_admin/routes/orders_shop.py")
        assert "export_orders" in source
        assert ".decode(" not in source, "файл должен уехать в браузер байт в байт"

    def test_export_keeps_the_filters_of_the_list(self):
        """«Нашёл — выгрузил»: иначе выгрузка молча отдаст не то, что на экране."""
        source = self._source("bot/web_admin/templates/orders_shop.html")
        assert "orders_shop_export" in source
        assert "status=status_filter" in source
        assert "q=query" in source
