"""Рендер страниц админки в статические файлы — для просмотра вёрстки.

Поднимать ради вёрстки Postgres и Redis незачем: шаблоны рисуются
подставными данными, результат складывается в `build/preview`.

    python -m scripts.preview_admin
"""

from __future__ import annotations

import datetime as dt
import types
from decimal import Decimal
from pathlib import Path

from api.admin.routes.dashboard import CHART_HEIGHT, CHART_WIDTH, SERIES_DAYS, _chart
from api.admin.templating import templates
from core.enums import OrderStatus, SubscriptionStatus
from core.services import stats
from core.services.remnawave import PanelStatus, RemnaHost, RemnaNode

OUT_DIR = Path(__file__).resolve().parent.parent / "build" / "preview"


class FakeQuery:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._values.get(key, default)


class FakePrincipal:
    def __init__(self) -> None:
        self.admin = types.SimpleNamespace(login="owner")
        self.role = "owner"

    def can(self, section: str) -> bool:
        return True


def _dashboard_data() -> stats.Dashboard:
    data = stats.Dashboard()
    data.day = stats.PeriodStats(orders=6, paid_orders=4, revenue=Decimal("38600"), new_users=5)
    data.week = stats.PeriodStats(orders=31, paid_orders=22, revenue=Decimal("214300"), new_users=28)
    data.month = stats.PeriodStats(orders=118, paid_orders=79, revenue=Decimal("806450"), new_users=96)
    data.day_trend = stats.Trend(Decimal("38600"), Decimal("29100"))
    data.week_trend = stats.Trend(Decimal("214300"), Decimal("238900"))
    data.month_trend = stats.Trend(Decimal("806450"), Decimal("806450"))
    data.mrr = Decimal("143200")
    data.users_total = 412
    data.users_blocked_bot = 9
    data.devices_total = 168
    data.devices_online = 141
    data.devices_active = 152
    data.subscriptions_active = 137
    data.subscriptions_grace = 4
    data.subscriptions_pending = 11
    data.expiring_7d = 18
    data.orders_awaiting = 5
    data.orders_to_ship = 7
    data.tickets_open = 2
    return data


def _series() -> list[tuple[dt.date, Decimal]]:
    amounts = [0, 12400, 9900, 0, 27800, 31200, 18600, 0, 22100, 40300, 35700, 12900, 29400, 38600]
    today = dt.date(2026, 8, 4)
    first = today - dt.timedelta(days=SERIES_DAYS - 1)
    return [(first + dt.timedelta(days=index), Decimal(value)) for index, value in enumerate(amounts)]


def _orders() -> list[types.SimpleNamespace]:
    rows = [
        ("R-260804-0007", "Титан Карл Иванович", "10 900", OrderStatus.PAID),
        ("R-260804-0006", "Ковалёва Анна", "7 649", OrderStatus.PACKING),
        ("R-260803-0005", "Мороз Пётр", "9 999", OrderStatus.SHIPPED),
        ("R-260803-0004", "Лебедев Игорь", "12 340", OrderStatus.AWAITING_PAYMENT),
    ]
    return [
        types.SimpleNamespace(
            id=index,
            public_number=number,
            customer_name=name,
            user=types.SimpleNamespace(display_name=name),
            total=Decimal(total.replace(" ", "")),
            status=status,
        )
        for index, (number, name, total, status) in enumerate(rows, start=1)
    ]


def _expiring() -> list[types.SimpleNamespace]:
    now = dt.datetime.now(dt.UTC)
    rows = [("Ковалёва Анна", "Год", 3), ("Мороз Пётр", "Полгода", 9), ("Титан Карл", "Месяц", 21)]
    return [
        types.SimpleNamespace(
            user_id=index,
            user=types.SimpleNamespace(display_name=name),
            plan=types.SimpleNamespace(title=plan),
            expires_at=now + dt.timedelta(days=days),
            status=SubscriptionStatus.ACTIVE,
        )
        for index, (name, plan, days) in enumerate(rows, start=1)
    ]


def _panel_status() -> PanelStatus:
    return PanelStatus(
        configured=True,
        ok=True,
        checked_at=dt.datetime.now(dt.UTC),
        users_total=389,
        users_online=147,
        nodes_total=6,
        nodes_online=5,
        hosts_total=4,
        version="2.1.14",
    )


def _remna_nodes() -> list[RemnaNode]:
    raw = [
        ("nl-01", "Amsterdam", "45.132.10.4", "NL", True, False, 61, 812_340_000_000),
        ("de-01", "Frankfurt", "45.132.11.9", "DE", True, False, 44, 511_000_000_000),
        ("fi-01", "Helsinki", "45.132.12.7", "FI", True, False, 22, 190_400_000_000),
        ("tr-01", "Istanbul", "45.132.13.2", "TR", False, False, 0, 8_400_000_000),
        ("us-01", "New York", "45.132.14.6", "US", True, False, 19, 77_900_000_000),
        ("ae-01", "Dubai", "45.132.15.1", "AE", False, True, 0, 0),
    ]
    return [
        RemnaNode(
            uuid=uuid,
            name=name,
            address=address,
            port=443,
            country_code=code,
            is_online=online,
            is_disabled=disabled,
            is_connected=online,
            users_online=users,
            traffic_used_bytes=traffic,
            xray_version="25.3.6" if online else "",
            status_message="" if online or disabled else "нет ответа больше часа",
        )
        for uuid, name, address, code, online, disabled, users, traffic in raw
    ]


def _remna_hosts() -> list[RemnaHost]:
    raw = [
        ("h1", "Netherlands · Reality", "nl.example.net", False),
        ("h2", "Germany · Reality", "de.example.net", False),
        ("h3", "Finland · Reality", "fi.example.net", False),
        ("h4", "Turkey · тест", "tr.example.net", True),
    ]
    return [
        RemnaHost(uuid=uuid, remark=remark, address=address, port=443, is_disabled=disabled)
        for uuid, remark, address, disabled in raw
    ]


def render(name: str, **context: object) -> None:
    payload = {
        "request": types.SimpleNamespace(query_params=FakeQuery({}), url=types.SimpleNamespace(path="/")),
        "principal": FakePrincipal(),
        "csrf_token": "preview",
        "current_path": context.pop("current_path", "/admin/"),
    }
    payload.update(context)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / name
    target.write_text(templates.env.get_template(name).render(**payload), encoding="utf-8")
    print(f"готово: {target}")


def main() -> None:
    series = _series()
    bars, peak = _chart(series)
    render(
        "dashboard.html",
        current_path="/admin/",
        data=_dashboard_data(),
        bars=bars,
        peak=peak,
        series_days=SERIES_DAYS,
        chart_width=CHART_WIDTH,
        chart_height=CHART_HEIGHT,
        recent_orders=_orders(),
        expiring=_expiring(),
        integrations=[
            ("Оплата PLATEGA", True, ""),
            ("Туннели к роутерам", True, ""),
            ("Панель Remnawave", False, "REMNAWAVE_TOKEN"),
        ],
        awaiting_statuses=[],
    )
    render(
        "remnawave.html",
        current_path="/admin/remnawave",
        status=_panel_status(),
        nodes=_remna_nodes(),
        hosts=_remna_hosts(),
        imported={"h1": types.SimpleNamespace(remarks="Router_Netherlands · Reality")},
        our_nodes_total=3,
        node_prefix="Router_",
        base_url="http://remnawave-backend:3000",
        paths={"статистика": "/api/system/stats", "узлы": "/api/nodes", "хосты": "/api/hosts"},
    )


if __name__ == "__main__":
    main()
