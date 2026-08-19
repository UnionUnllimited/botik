"""Тарифные зоны доставки от склада в Самаре

Доставка стоила одинаково по всей стране: и Тольятти, и Владивосток. На ближнем
заказе это лишние деньги с клиента, на дальнем — минус из кармана.

Зоны и цены заводятся сразу, иначе после выката оформление встало бы у всех:
незнакомый город заказ не пропускает. Цены — публичные тарифы для частных лиц
на посылку около килограмма; по договору с перевозчиком будет дешевле, и это
первое, что стоит поправить в админке по первым накладным.

Города — областные центры и крупные города. Остальные оператор дописывает
по мере появления: их видно в списке неопознанных.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (код, название, срок, города, {метод: (ПВЗ, курьер)})
_ZONES: tuple[tuple[str, str, str, str, dict[str, tuple[str, str]]], ...] = (
    (
        "home",
        "Самара и область",
        "1–2 дня",
        "Самара\nТольятти\nСызрань\nНовокуйбышевск\nЧапаевск\nОтрадный\nЖигулёвск\n"
        "Кинель\nПохвистнево\nОктябрьск\nНефтегорск\nБезенчук\nКрасный Яр",
        {"cdek": ("200", "350"), "yandex": ("150", "300"), "post": ("200", "300")},
    ),
    (
        "volga",
        "Поволжье",
        "2–3 дня",
        "Казань\nУфа\nСаратов\nУльяновск\nОренбург\nПенза\nНижний Новгород\nИжевск\n"
        "Чебоксары\nЙошкар-Ола\nСаранск\nКиров\nПермь\nНабережные Челны\nСтерлитамак\n"
        "Балаково\nЭнгельс\nДимитровград\nНовотроицк\nОрск\nАльметьевск\nНижнекамск",
        {"cdek": ("280", "450"), "yandex": ("220", "420"), "post": ("250", "380")},
    ),
    (
        "central",
        "Центр и Юг",
        "3–4 дня",
        "Москва\nЗеленоград\nПодольск\nХимки\nБалашиха\nМытищи\nЛюберцы\nКоролёв\n"
        "Красногорск\nВоронеж\nЛипецк\nТамбов\nРязань\nТула\nКалуга\nБрянск\nОрёл\n"
        "Курск\nБелгород\nСмоленск\nТверь\nЯрославль\nКострома\nИваново\nВладимир\n"
        "Ростов-на-Дону\nКраснодар\nСочи\nВолгоград\nАстрахань\nСтаврополь\nМахачкала\n"
        "Владикавказ\nНальчик\nГрозный\nМайкоп\nЭлиста\nНовороссийск\nТаганрог\nСевастополь\n"
        "Симферополь",
        {"cdek": ("350", "520"), "yandex": ("300", "500"), "post": ("300", "430")},
    ),
    (
        "northwest_urals",
        "Северо-Запад и Урал",
        "3–5 дней",
        "Санкт-Петербург\nВыборг\nГатчина\nПсков\nВеликий Новгород\nПетрозаводск\n"
        "Вологда\nЧереповец\nАрхангельск\nСыктывкар\nКалининград\nЕкатеринбург\n"
        "Челябинск\nТюмень\nКурган\nМагнитогорск\nНижний Тагил\nСургут\nНижневартовск\n"
        "Ханты-Мансийск\nТобольск\nЗлатоуст\nКаменск-Уральский",
        {"cdek": ("400", "580"), "yandex": ("350", "560"), "post": ("330", "470")},
    ),
    (
        "siberia",
        "Сибирь",
        "5–7 дней",
        "Новосибирск\nОмск\nКрасноярск\nБарнаул\nКемерово\nНовокузнецк\nТомск\nИркутск\n"
        "Улан-Удэ\nЧита\nАбакан\nКызыл\nБийск\nАнгарск\nБратск\nАчинск\nНорильск",
        {"cdek": ("550", "750"), "yandex": ("500", "720"), "post": ("400", "560")},
    ),
    (
        "far_east",
        "Дальний Восток и Крайний Север",
        "7–14 дней",
        "Владивосток\nХабаровск\nЯкутск\nМагадан\nПетропавловск-Камчатский\n"
        "Южно-Сахалинск\nБлаговещенск\nБиробиджан\nАнадырь\nУссурийск\nНаходка\n"
        "Комсомольск-на-Амуре\nМурманск\nСалехард\nНовый Уренгой\nНоябрьск\nВоркута",
        {"cdek": ("950", "1200"), "yandex": ("900", "1180"), "post": ("600", "800")},
    ),
)


def upgrade() -> None:
    zones = op.create_table(
        "delivery_zones",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("code", sa.String(length=32), nullable=False, unique=True),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("cities", sa.Text(), nullable=False, server_default=""),
        sa.Column("days", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    prices = op.create_table(
        "delivery_zone_prices",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "zone_id",
            sa.BigInteger(),
            sa.ForeignKey("delivery_zones.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("method", sa.String(length=32), nullable=False),
        sa.Column("pvz_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("courier_price", sa.Numeric(12, 2), nullable=False),
    )
    op.create_index(
        "ix_delivery_zone_prices_zone_method",
        "delivery_zone_prices",
        ["zone_id", "method"],
        unique=True,
    )

    op.create_table(
        "delivery_unknown_cities",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("city", sa.String(length=120), nullable=False),
        sa.Column("normalized", sa.String(length=120), nullable=False, unique=True),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("tg_id", sa.BigInteger()),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.bulk_insert(
        zones,
        [
            {
                "code": code,
                "title": title,
                "cities": cities,
                "days": days,
                "sort_order": (index + 1) * 10,
            }
            for index, (code, title, days, cities, _prices) in enumerate(_ZONES)
        ],
    )

    # Цены заводим отдельным проходом: идентификаторы зон известны только
    # после вставки, а угадывать их по счётчику — верный способ разъехаться
    # на пересоздании базы.
    bind = op.get_bind()
    rows = []
    for code, _title, _days, _cities, tariffs in _ZONES:
        zone_id = bind.execute(
            sa.text("SELECT id FROM delivery_zones WHERE code = :code"), {"code": code}
        ).scalar_one()
        for method, (pvz, courier) in tariffs.items():
            rows.append(
                {"zone_id": zone_id, "method": method, "pvz_price": pvz, "courier_price": courier}
            )
    op.bulk_insert(prices, rows)


def downgrade() -> None:
    op.drop_table("delivery_unknown_cities")
    op.drop_index("ix_delivery_zone_prices_zone_method", table_name="delivery_zone_prices")
    op.drop_table("delivery_zone_prices")
    op.drop_table("delivery_zones")
