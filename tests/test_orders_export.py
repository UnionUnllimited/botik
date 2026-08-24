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


class TestOrdersSorting:
    """Порядок в списке заказов. Тот же приём, что на странице роутеров."""

    def _source(self, relative: str) -> str:
        return (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")

    def test_known_columns(self):
        from api.routes.catalog_api import ORDERS_SORT_COLUMNS

        assert set(ORDERS_SORT_COLUMNS) == {"number", "customer", "city", "total", "status", "created"}

    def test_second_key_is_the_id(self):
        """Без него заказы с одинаковой суммой переставляются между
        страницами, и один и тот же попадает на обе."""
        api = self._source("api/routes/catalog_api.py")
        body = api[api.index("async def manage_orders") :]
        body = body[: body.index("ORDERS_CSV_COLUMNS")]
        assert "Order.id.desc()" in body

    def test_unknown_column_is_ignored(self):
        api = self._source("api/routes/catalog_api.py")
        body = api[api.index("async def manage_orders") :]
        body = body[: body.index("ORDERS_CSV_COLUMNS")]
        assert "ORDERS_SORT_COLUMNS.get(sort)" in body
        assert "if column is None" in body

    def test_headers_are_links(self):
        page = self._source("bot/web_admin/templates/orders_shop.html")
        assert "sort_th(" in page
        assert "{{ sort_th('total'" in page

    def test_search_keeps_the_order(self):
        """«Найти» отправляет только свои поля — сортировку несём скрытыми."""
        page = self._source("bot/web_admin/templates/orders_shop.html")
        assert 'name="sort"' in page and 'name="dir"' in page


class TestOrderDeletion:
    """Удаление заказа — только для ошибочных и брошенных.

    Оплаченный заказ это платёж, чек и, скорее всего, уехавшее железо.
    Стерев его, мы потеряем сверку с провайдером и историю клиента,
    а восстановить будет неоткуда.
    """

    def _source(self, relative: str) -> str:
        return (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")

    def test_only_untouched_statuses(self):
        from api.routes.catalog_api import DELETABLE_ORDER_STATUSES
        from core.enums import OrderStatus

        assert OrderStatus.PAID not in DELETABLE_ORDER_STATUSES
        assert OrderStatus.SHIPPED not in DELETABLE_ORDER_STATUSES
        assert OrderStatus.NEW in DELETABLE_ORDER_STATUSES

    def test_paid_order_is_refused(self):
        api = self._source("api/routes/catalog_api.py")
        body = api[api.index("async def manage_order_delete") :]
        body = body[: body.index("\n@router")]
        assert "order.paid_at is not None" in body

    def test_payments_block_deletion(self):
        """Платёж мог остаться и у неоплаченного заказа — висящая ссылка.
        Стерев заказ, мы оставили бы платёж без хозяина."""
        api = self._source("api/routes/catalog_api.py")
        body = api[api.index("async def manage_order_delete") :]
        body = body[: body.index("\n@router")]
        assert "Payment.order_id == order.id" in body

    def test_device_is_released_not_deleted(self):
        """Роутер — вещь, а не запись: он остаётся на складе."""
        api = self._source("api/routes/catalog_api.py")
        body = api[api.index("async def manage_order_delete") :]
        body = body[: body.index("\n@router")]
        assert "update(Device)" in body and "order_id=None" in body

    def test_button_hidden_for_paid_orders(self):
        card = self._source("bot/web_admin/templates/orders_shop_card.html")
        assert "{% if not order.paid %}" in card
        assert "admin.order_shop_delete" in card


class TestOurPayments:
    """Раздел платежей показывает наши деньги: роутеры и доставку.

    У продукта свой раздел и своя таблица — там подписка для телефона.
    Оплата железа идёт через нас и в его базу не попадает вовсе, поэтому
    оператор искал платёж там и не находил.
    """

    def _source(self, relative: str) -> str:
        return (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")

    def test_endpoint_exists(self):
        api = self._source("api/routes/catalog_api.py")
        assert '@router.get("/manage/payments")' in api

    def test_delivery_is_told_apart_from_the_router(self):
        """Доставка — второй платёж по тому же заказу, и в списке её надо
        отличать: сверка считает по платежам, а не по заказу."""
        from api.routes.catalog_api import PAYMENT_PURPOSE_LABELS

        assert PAYMENT_PURPOSE_LABELS["delivery"] != PAYMENT_PURPOSE_LABELS["order"]

    def test_menu_points_to_our_page(self):
        base = self._source("bot/web_admin/templates/base.html")
        assert "admin.payments_shop" in base

    def test_page_links_to_the_order(self):
        page = self._source("bot/web_admin/templates/payments_shop.html")
        assert "admin.order_shop_card" in page
