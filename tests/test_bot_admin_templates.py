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
    "delivered_at": None,
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
            {
                "id": 1,
                "provider": "platega",
                "status": "succeeded",
                "status_label": "оплачен",
                "amount": "6900.00",
            }
        ],
        "devices": [{"id": 1, "mac": "A0:B1:C2:D3:E4:F5", "model": "AX3000"}],
        "free_devices": [{"mac": "A0:B1:C2:D3:E4:F6", "model": "AX3000"}],
        "next_statuses": ["packing", "cancelled"],
        "status_titles": {"packing": "Собираем", "cancelled": "Отменён", "paid": "Оплачен"},
    },
    "catalog_promos.html": {
        "promo_error": "",
        "promos": [
            {
                "id": 1, "code": "ROUTER10", "description": "Рассылка на 12 августа",
                "discount_type": "percent", "value": "10.00", "max_uses": 100,
                "used_count": 3, "per_user_limit": 1, "min_amount": "5000.00",
                "valid_until": "2026-09-01T00:00:00+00:00",
                "new_clients_only": True, "is_active": True,
            },
            {
                "id": 2, "code": "SALE500", "description": "",
                "discount_type": "fixed", "value": "500.00", "max_uses": 0,
                "used_count": 0, "per_user_limit": 0, "min_amount": "0.00",
                "valid_until": None, "new_clients_only": False, "is_active": False,
            },
        ],
    },
    "catalog_shop.html": {"products": [PRODUCT], "catalog_error": ""},
    "catalog_delivery.html": {
        "delivery": DELIVERY_OPTIONS,
        "free_from": "0.00",
        "delivery_error": "",
    },
    "catalog_delivery_zones.html": {
        "zones": [
            {
                "id": 1,
                "code": "home",
                "title": "Самара и область",
                "days": "1–2 дня",
                "cities": "Самара\nТольятти",
                "cities_count": 2,
                "prices": {
                    "cdek": {"pvz": "200.00", "courier": "350.00"},
                    "yandex": {"pvz": "150.00", "courier": "300.00"},
                },
            },
            # У дальней зоны нет цены Яндекса: перевозчика завели позже зон,
            # и страница обязана нарисовать пустое поле, а не упасть.
            {
                "id": 6,
                "code": "far_east",
                "title": "Дальний Восток",
                "days": "7–14 дней",
                "cities": "Владивосток",
                "cities_count": 1,
                "prices": {"cdek": {"pvz": "950.00", "courier": "1200.00"}},
            },
        ],
        "methods": [
            {"method": "cdek", "title": "СДЭК"},
            {"method": "yandex", "title": "Яндекс Go"},
        ],
        "unknown": [
            {
                "id": 3,
                "city": "Урюпинск",
                "hits": 4,
                "tg_id": 614685408,
                "last_seen_at": "2026-08-18T12:30:00+00:00",
            },
            {"id": 4, "city": "ъ", "hits": 1, "tg_id": 0, "last_seen_at": ""},
        ],
        "zones_error": "",
    },
    "catalog_settings.html": {
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
        "client_subscription": {"status": "active", "until": "2026-09-04T10:00:00+00:00"},
        "payments": [],
        "promo": [],
        "traffic_stats": {},
        "multi_traffic_stats": {},
        "device_limit_history": [],
        "has_2ip_token": False,
        "user_raw": {},
        "user_columns_meta": [],
    },
    "domain_lists.html": {
        "fleet_error": "",
        "sources": [
            {
                "id": 1, "url": "https://example.com/block.lst", "title": "Заблокированное",
                "kind": "domain", "is_enabled": True, "last_lines": 1200,
                "last_error": "", "last_ok_at": "2026-08-13T15:00:00+00:00",
            },
            {
                "id": 2, "url": "https://example.com/subnets.lst", "title": "",
                "kind": "ip", "is_enabled": False, "last_lines": 0,
                "last_error": "HTTP 404", "last_ok_at": None,
            },
        ],
        "manual": {
            "domain": {
                "body": "my.example.com",
                "updated_by": "union",
                "updated_at": "2026-08-13T15:00:00+00:00",
            },
            "ip": {"body": "", "updated_by": "", "updated_at": None},
        },
        "history": {
            "domain": [
                {
                    "id": 4, "author": "union", "added": 12, "removed": 0,
                    "created_at": "2026-08-13T15:00:00+00:00",
                },
                {
                    "id": 3, "author": "откат к версии 1", "added": 0, "removed": 0,
                    "created_at": "2026-08-12T09:30:00+00:00",
                },
            ],
            "ip": [],
        },
        "imported": {},
        "config": {
            "lists_poll_interval_min": "10",
            "lists_local_dir": "/var/www/lists",
            "lists_s3_endpoint": "https://storage.yandexcloud.net",
            "lists_s3_bucket": "router-lists",
            "lists_s3_region": "ru-central1",
            "lists_s3_prefix": "lists/",
            "lists_s3_access_key": "задан",
            "lists_s3_secret_key": "",
        },
        "files": [
            {
                "kind": "domain", "title": "Домены", "name": "domains.lst",
                "url": "https://example.com/lists/domains.lst", "lines": 51234,
            },
            {
                "kind": "ip", "title": "Подсети IPv4", "name": "ip.lst",
                "url": "https://example.com/lists/ip.lst", "lines": 0,
            },
        ],
        "last_build": {
            "domains": 51234, "ips": 812, "failed_sources": 1,
            "finished_at": "2026-08-13T15:04:00+00:00", "error": "", "uploaded": False,
        },
    },
    "router_card.html": {
        "device_id": 1,
        "card": {},
        "clients": [
            {"value": "614685408", "tg_id": 614685408, "name": "Union", "username": "union", "phone": ""}
        ],
        "router": {
            "id": 1,
            "mac": "A0:B1:C2:D3:E4:F5",
            "model": "AX3000",
            "fw_version": "25.12.3",
            "status": "active",
            "status_label": "работает",
            "online": True,
            "last_seen": "2026-08-12T14:25:04+00:00",
            "activated_at": "2026-08-04T18:35:00+00:00",
            "wan_ip": "192.168.16.136",
            "uptime_sec": 90000,
            "clients": 17,
            "cpu_pct": 0,
            "ram_pct": 22,
            "rx_bytes": 1024,
            "tx_bytes": 2048,
            "visitor_port": 20003,
        },
        "client": {"id": 5, "name": "Иванов Иван", "email": "", "phone": "+79001234567"},
        "subscription": {
            "status": "active",
            "label": "активна",
            "until": "2026-09-04T10:00:00+00:00",
            "here": True,
        },
        "panel": {"username": "a0-b1-c2-d3-e4-f5", "until": None, "active": False},
        "events": [],
        "fleet_error": "",
        "console_output": "",
        "console_command": "",
    },
    "routers_fleet.html": {
        "fleet": {
            "fleet_total": 120,
            "silent": 4,
            "no_client": 7,
            "total": 120,
            "online": 116,
        },
        "routers": [
            {
                "id": 1,
                "mac": "A0:B1:C2:D3:E4:F5",
                "model": "AX3000",
                "client": "Иванов Иван",
                "online": True,
                "subscription_status": "active",
                "subscription_label": "активна",
                "status_label": "работает",
                "fw_version": "25.12.3",
                "uptime_sec": 372000,
                "visitor_port": 7101,
                "rx_bytes": 5_368_709_120,
                "tx_bytes": 1_073_741_824,
                "last_seen": "2026-08-13T15:04:00+00:00",
                "subscription_here": True,
                "clients": 3,
                "cpu_pct": 12,
                "ram_pct": 40,
                "wan_ip": "1.2.3.4",
            }
        ],
        "fleet_error": "",
        "auto_enabled": True,
        "filters": {"q": "", "link": "", "client": ""},
        "page": 1,
        "pages": 3,
        "total": 120,
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


OURS = {
    "catalog_delivery.html",
    "catalog_delivery_zones.html",
    "catalog_settings.html",
    "catalog_promos.html",
    "catalog_shop.html",
    "catalog_shop_form.html",
    "devices_stock.html",
    "orders_shop.html",
    "orders_shop_card.html",
    "domain_lists.html",
    "router_card.html",
    "routers_fleet.html",
}
"""Страницы, написанные нами. Их рисуем строго: обращение к непереданной
переменной должно падать в тесте, а не рисоваться пустотой у оператора.
Для чужих страниц так нельзя — там свой контекст в сотню ключей."""


@pytest.fixture(scope="module")
def strict_env(env) -> jinja2.Environment:
    strict = env.overlay(undefined=jinja2.StrictUndefined)
    strict.filters.update(env.filters)
    strict.globals.update(env.globals)
    return strict


@pytest.mark.parametrize("name", sorted(OURS))
def test_our_page_has_everything_it_asks_for(strict_env, name):
    assert strict_env.get_template(name).render(**PAGES[name]).strip()


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_renders(env, name):
    html = env.get_template(name).render(**PAGES[name])
    assert html.strip()
    # Метод вместо данных виден в выводе — так падение и выглядело.
    assert "built-in method" not in html
    assert "dict_items" not in html
