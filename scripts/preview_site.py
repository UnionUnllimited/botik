"""Рендер страниц сайта в статические файлы — чтобы посмотреть вёрстку.

То же, что `scripts.preview_admin`, только для клиентских страниц: шаблоны
рисуются подставными данными, Postgres и Redis не нужны. Формы в результате
не работают — это картинка, а не сайт.

    python -m scripts.preview_site
"""

from __future__ import annotations

import datetime as dt
import types
from decimal import Decimal
from pathlib import Path

from api.site.templating import templates
from core.enums import DeviceStatus, OrderStatus, SubscriptionStatus
from core.validators import PASSWORD_MIN_LENGTH

OUT_DIR = Path(__file__).resolve().parent.parent / "build" / "preview_site"

NOW = dt.datetime(2026, 8, 9, 12, tzinfo=dt.UTC)


def _products() -> list[types.SimpleNamespace]:
    return [
        types.SimpleNamespace(
            slug="zbt-z8103ax",
            title="ZBT Z8103AX",
            subtitle="Wi-Fi 6, 3 порта LAN, для квартиры",
            description="Роутер приходит настроенным.\nОстаётся включить в розетку и ввести MAC на сайте.",
            price=Decimal("7900"),
            old_price=Decimal("8900"),
            in_stock=True,
            photo_url=None,
            specs={"Wi-Fi": "802.11ax, 3000 Мбит/с", "Порты": "3 × LAN, 1 × WAN", "Питание": "12 В"},
        ),
        types.SimpleNamespace(
            slug="cudy-wr3000e",
            title="Cudy WR3000E",
            subtitle="Компактный, в командировку",
            description="",
            price=Decimal("6400"),
            old_price=None,
            in_stock=False,
            photo_url=None,
            specs={},
        ),
    ]


def _plans() -> list[types.SimpleNamespace]:
    return [
        types.SimpleNamespace(
            title="3 месяца", months=3, extra_days=0,
            price=Decimal("1500"), old_price=None, price_per_month=Decimal("500"),
        ),
        types.SimpleNamespace(
            title="12 месяцев", months=12, extra_days=30,
            price=Decimal("4800"), old_price=Decimal("6000"), price_per_month=Decimal("400"),
        ),
    ]


def _orders() -> list[types.SimpleNamespace]:
    return [
        types.SimpleNamespace(
            public_number="RS-000412",
            created_at=NOW - dt.timedelta(days=12),
            total=Decimal("12700"),
            status=OrderStatus.DELIVERED,
        ),
        types.SimpleNamespace(
            public_number="RS-000517",
            created_at=NOW - dt.timedelta(days=2),
            total=Decimal("4800"),
            status=OrderStatus.AWAITING_PAYMENT,
        ),
    ]


def render(name: str, out_name: str, **context: object) -> None:
    payload: dict[str, object] = {
        "request": types.SimpleNamespace(url=types.SimpleNamespace(path="/")),
        "client": context.pop("client", None),
        "csrf_token": "preview",
        "current_path": context.pop("current_path", "/"),
    }
    payload.update(context)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / out_name
    target.write_text(templates.env.get_template(name).render(**payload), encoding="utf-8")
    print(f"готово: {target}")


def main() -> None:
    signed_in = types.SimpleNamespace(session=types.SimpleNamespace(csrf="preview"))

    render("index.html", "index.html", current_path="/", products=_products(), plans=_plans())
    render("product.html", "product.html", current_path="/catalog/zbt-z8103ax", product=_products()[0])
    render(
        "auth_form.html",
        "login.html",
        current_path="/login",
        is_register=False,
        error="",
        email="",
        password_min=PASSWORD_MIN_LENGTH,
        action="/login",
        title="Вход",
        subtitle="Войдите, чтобы открыть личный кабинет",
        submit="Войти",
    )
    render(
        "auth_form.html",
        "register.html",
        current_path="/register",
        is_register=True,
        error="Такая почта уже зарегистрирована. Войдите или смените адрес.",
        email="client@example.com",
        password_min=PASSWORD_MIN_LENGTH,
        action="/register",
        title="Регистрация",
        subtitle="Почта и пароль — этого хватит, чтобы следить за подпиской",
        submit="Зарегистрироваться",
    )
    render(
        "cabinet.html",
        "cabinet.html",
        current_path="/cabinet",
        client=signed_in,
        user=types.SimpleNamespace(email="client@example.com"),
        subscription=types.SimpleNamespace(
            status=SubscriptionStatus.ACTIVE,
            expires_at=NOW + dt.timedelta(days=48),
            pending_expires_at=None,
        ),
        plan=types.SimpleNamespace(title="12 месяцев"),
        devices=[
            types.SimpleNamespace(mac="A0:B1:C2:D3:E4:F5", model="ZBT Z8103AX", status=DeviceStatus.ACTIVE)
        ],
        orders=_orders(),
    )
    render(
        "cabinet.html",
        "cabinet_empty.html",
        current_path="/cabinet",
        client=signed_in,
        user=types.SimpleNamespace(email="new@example.com"),
        subscription=None,
        plan=None,
        devices=[],
        orders=[],
    )


if __name__ == "__main__":
    main()
