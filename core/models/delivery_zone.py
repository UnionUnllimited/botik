"""Тарифные зоны доставки: цена зависит от того, куда едет посылка.

До этого доставка стоила одинаково по всей стране — и Тольятти, и Владивосток.
На одном ближнем заказе это лишние деньги с клиента, на одном дальнем — минус
из кармана, и чем дальше от склада, тем больше.

Считаем зонами, а не калькулятором перевозчика: у нас один товар, вес
и габариты постоянные, значит цена зависит только от «куда». Зона считается
мгновенно и не тащит в оформление заказа чужой сервис, который может не
ответить ровно тогда, когда клиент достал карту.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    DECIMAL,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.enums import DeliveryMethod
from core.models.base import Base, BigIntPkMixin, enum_column

MONEY = DECIMAL(12, 2)


class DeliveryZone(BigIntPkMixin, Base):
    """Пояс, до которого едет посылка, и список городов в нём.

    Города списком строк, а не отдельной таблицей: их правят целиком, вставкой
    из блокнота, и «добавить пять городов» в текстовом поле делается быстрее,
    чем пятью нажатиями.
    """

    __tablename__ = "delivery_zones"

    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    cities: Mapped[str] = mapped_column(Text, default="", nullable=False)
    """По городу на строку. Сравнение нечувствительно к регистру и к «г.»."""

    days: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    """Срок словами: «2–4 дня». Клиент спрашивает про него не реже, чем про цену."""

    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    prices: Mapped[list[DeliveryZonePrice]] = relationship(
        back_populates="zone", cascade="all, delete-orphan"
    )


class DeliveryZonePrice(BigIntPkMixin, Base):
    """Цена перевозчика в этой зоне: до пункта выдачи и до двери."""

    __tablename__ = "delivery_zone_prices"

    zone_id: Mapped[int] = mapped_column(
        ForeignKey("delivery_zones.id", ondelete="CASCADE"), nullable=False
    )
    method: Mapped[DeliveryMethod] = enum_column(DeliveryMethod, nullable=False)

    pvz_price: Mapped[float] = mapped_column(MONEY, nullable=False)
    courier_price: Mapped[float] = mapped_column(MONEY, nullable=False)

    zone: Mapped[DeliveryZone] = relationship(back_populates="prices")

    __table_args__ = (Index("ix_delivery_zone_prices_zone_method", "zone_id", "method", unique=True),)


class UnknownCity(BigIntPkMixin, Base):
    """Город, которого нет ни в одной зоне.

    Оформление на таком городе останавливается: назвать цену наугад — значит
    либо отпугнуть клиента, либо повезти себе в убыток. Запись нужна, чтобы
    оператор увидел, куда просятся, добавил город в зону и вернулся к человеку.
    """

    __tablename__ = "delivery_unknown_cities"

    city: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    hits: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    """Сколько раз просились. Город с десятком попыток стоит завести первым."""

    tg_id: Mapped[int | None] = mapped_column(BigInteger)
    """Кому не дали оформить — чтобы было к кому вернуться с ценой.
    BigInteger: идентификаторы Telegram давно не влезают в обычный int."""

    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """Город завели в зону — строка остаётся в истории, но из списка уходит."""

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
