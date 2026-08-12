"""Страницы, добавленные в админку бота, отрисовываются на реальных данных.

Разбор шаблона таких ошибок не ловит: `order.items` разбирается прекрасно,
а при отрисовке отдаёт метод `dict.items` вместо состава заказа — страница
падает пятисоткой уже у оператора. Ровно это и случилось на карточке заказа.

Базовый шаблон подменён заглушкой: он тянет весь каркас их админки — сессию,
права, версию кэша, — а нам нужен только наш блок `content`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import jinja2
import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "bot" / "web_admin" / "templates"

BASE_STUB = "{% block title %}{% endblock %}{% block content %}{% endblock %}"

ORDER = {
    "id": 2,
    "number": "R-260811-0002",
    "status": "paid",
    "total": "6900.00",
    "subtotal": "6900.00",
    "discount": "0.00",
    "delivery_price": "350.00",
    "customer": "Иванов Иван",
    "phone": "+79001234567",
    "city": "Москва",
    "comment": "",
    "note": "",
    "cancel_reason": "",
    "paid_at": "2026-08-11T09:00:00+00:00",
    "shipped_at": None,
    "created_at": "2026-08-11T08:00:00+00:00",
    "paid": True,
    "tracking_number": "",
    "items": [{"title": "Роутер AX3000", "total": "6900.00"}],
}

PRODUCT = {
    "id": 7,
    "slug": "ax3000",
    "title": "Роутер AX3000",
    "subtitle": "Для квартиры",
    "description": "Описание",
    "model_code": "AX3000",
    "price": "6900.00",
    "old_price": "7900.00",
    "stock": 3,
    "allow_preorder": False,
    "is_active": True,
    "sort_order": 10,
    "specs": {"Порты": "3 LAN"},
    "photo_url": "https://shop.example/media/a.jpg",
    "photo_path": "/media/a.jpg",
}

DELIVERY_OPTIONS = [
    {
        "method": "cdek",
        "title": "СДЭК",
        "pvz_price": "350.00",
        "courier_price": "550.00",
        "days": "3–7 дней",
        "enabled": True,
    }
]

PAGES = {
    "orders_shop.html": {
        "orders": [ORDER],
        "statuses": ["new", "paid"],
        "status_titles": {"new": "Новый", "paid": "Оплачен"},
        "status_filter": "",
        "query": "",
        "total": 1,
        "page": 1,
        "pages": 1,
        "orders_error": "",
    },
    "orders_shop_card.html": {
        "order": ORDER,
        "delivery": {
            "method": "cdek",
            "summary": "CDEK, Москва",
            "address": "Ленина 1",
            "recipient": "Иванов Иван",
            "phone": "+79001234567",
            "tracking_number": "",
            "tracking_url": "",
        },
        "payments": [
            {"id": 1, "provider": "platega", "status": "succeeded", "amount": "6900.00"}
        ],
        "devices": [{"id": 1, "mac": "A0:B1:C2:D3:E4:F5", "model": "AX3000"}],
        "free_devices": [{"mac": "A0:B1:C2:D3:E4:F6", "model": "AX3000"}],
        "next_statuses": ["packing", "cancelled"],
        "status_titles": {"packing": "Собираем", "cancelled": "Отменён", "paid": "Оплачен"},
    },
    "catalog_shop.html": {
        "products": [PRODUCT],
        "catalog_error": "",
        "delivery": DELIVERY_OPTIONS,
        "free_from": "0.00",
        "delivery_error": "",
        "plans": [
            {
                "id": 1,
                "slug": "m3",
                "title": "3 месяца",
                "months": 0,
                "extra_days": 90,
                "price": "1490.00",
                "is_active": True,
                "sort_order": 100,
            }
        ],
        "plans_error": "",
        "catalog_enabled": True,
        "specs_limit": 8,
    },
    "catalog_shop_form.html": {
        "product": PRODUCT,
        "specs_text": "Порты: 3 LAN",
        "title": "Роутер AX3000",
    },
    "devices_stock.html": {
        "devices": [
            {
                "id": 1,
                "mac": "A0:B1:C2:D3:E4:F5",
                "model": "AX3000",
                "serial": "SN1",
                "status": "new",
                "client": "",
                "note": "",
            }
        ],
        "statuses": ["new", "assigned"],
        "status_titles": {"new": "На складе", "assigned": "Отгружено"},
        "total": 1,
        "page": 1,
        "pages": 1,
        "query": "",
        "show_all": False,
        "stock_error": "",
    },
    "user_details.html": {
        "user": {"telegram_id": 614685408, "username": "Union"},
        "client_routers": [
            {
                "id": 1,
                "mac": "A0:B1:C2:D3:E4:F5",
                "model": "AX3000",
                "online": True,
                "activated_at": "2026-08-11T09:00:00+00:00",
            }
        ],
        "client_free_routers": [{"mac": "A0:B1:C2:D3:E4:F6", "model": "AX3000"}],
        "client_routers_error": "",
        "payments": [],
        "promo": [],
        "traffic_stats": {},
        "multi_traffic_stats": {},
        "device_limit_history": [],
        "has_2ip_token": False,
        "user_raw": {},
        "user_columns_meta": [],
    },
    "router_card.html": {
        "device_id": 1,
        "card": {},
        "clients": [
            {"value": "614685408", "tg_id": 614685408, "name": "Union", "username": "union", "phone": ""}
        ],
        "router": {"mac": "A0:B1:C2:D3:E4:F5", "model": "", "online": True, "visitor_port": 20003},
        "client": {},
        "subscription": {},
        "panel": {"username": "a0-b1-c2-d3-e4-f5", "until": None, "active": False},
        "events": [],
        "fleet_error": "",
        "console_output": "",
        "console_command": "",
    },
    "routers_fleet.html": {
        "fleet": {"total": 1, "online": 1},
        "routers": [
            {
                "id": 1,
                "mac": "A0:B1:C2:D3:E4:F5",
                "model": "AX3000",
                "client": "Иванов Иван",
                "online": True,
                "subscription_status": "active",
                "subscription_here": True,
                "clients": 3,
                "cpu_pct": 12,
                "ram_pct": 40,
                "wan_ip": "1.2.3.4",
            }
        ],
        "fleet_error": "",
        "auto_enabled": True,
        "auto_days": 30,
    },
}


@pytest.fixture(scope="module")
def env() -> jinja2.Environment:
    environment = jinja2.Environment(
        loader=jinja2.ChoiceLoader(
            [
                jinja2.DictLoader({"base.html": BASE_STUB}),
                jinja2.FileSystemLoader(str(TEMPLATES)),
            ]
        ),
        autoescape=True,
    )
    # Фильтры их админки — нам важно, что страница собирается, а не как
    # выглядит дата.
    environment.filters["msk_datetime"] = lambda value: str(value or "")
    environment.globals.update(
        url_for=lambda endpoint, **kwargs: f"/{endpoint}",
        get_flashed_messages=lambda **kwargs: [],
        request=SimpleNamespace(path="/", args={}),
        # Права проверяются в их шаблонах напрямую; для отрисовки достаточно
        # самого широкого доступа — иначе половина блоков просто не появится.
        current_user=SimpleNamespace(is_admin=True, is_authenticated=True, auth_id="1"),
        moderator_can_see=lambda section: True,
    )
    return environment


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_renders(env, name):
    html = env.get_template(name).render(**PAGES[name])
    assert html.strip()
    # Метод вместо данных виден в выводе — так падение и выглядело.
    assert "built-in method" not in html
    assert "dict_items" not in html
