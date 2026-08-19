"""Доставка: варианты, цены и создание записи к заказу.

Цены берутся из настроек (правятся в админке). Интеграция с API перевозчика
вынесена за интерфейс `CarrierClient`: в v1 работает ручной режим, когда
трек-номер вбивает логист.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.enums import OFFERED_DELIVERY_METHODS, DeliveryMethod
from core.models import Delivery, DeliveryZone, DeliveryZonePrice, Order, UnknownCity
from core.services import settings_service

log = structlog.get_logger("services.delivery")

TRACKING_URLS = {
    DeliveryMethod.CDEK: "https://www.cdek.ru/ru/tracking?order_id={track}",
    DeliveryMethod.POST: "https://www.pochta.ru/tracking#{track}",
    DeliveryMethod.BOXBERRY: "https://boxberry.ru/tracking-page?id={track}",
}
"""Для Яндекс Go ссылки нет: он присылает её клиенту сам, придумывать свою незачем."""


@dataclass(frozen=True, slots=True)
class DeliveryOption:
    method: DeliveryMethod
    title: str
    pvz_price: Decimal
    courier_price: Decimal
    days: str
    enabled: bool = True

    def price_for(self, *, to_pvz: bool) -> Decimal:
        return self.pvz_price if to_pvz else self.courier_price


async def get_options(session: AsyncSession, *, only_enabled: bool = True) -> list[DeliveryOption]:
    """Способы доставки в порядке перечисления.

    `only_enabled=False` нужен админке: там показываются и выключенные,
    иначе их нельзя было бы включить обратно.
    """
    raw = await settings_service.get_setting(session, "delivery.methods") or {}
    options: list[DeliveryOption] = []
    for method in OFFERED_DELIVERY_METHODS:
        config = raw.get(method.value)
        if not isinstance(config, dict):
            continue
        # Ключа может не быть у настроек, сохранённых до появления переключателей.
        enabled = bool(config.get("enabled", True))
        if only_enabled and not enabled:
            continue
        options.append(
            DeliveryOption(
                method=method,
                title=str(config.get("title", method.value)),
                pvz_price=Decimal(str(config.get("pvz", "0.00"))),
                courier_price=Decimal(str(config.get("courier", "0.00"))),
                days=str(config.get("days", "")),
                enabled=enabled,
            )
        )
    return options


async def get_option(session: AsyncSession, method: DeliveryMethod) -> DeliveryOption | None:
    for option in await get_options(session):
        if option.method is method:
            return option
    return None


async def calculate_price(
    session: AsyncSession,
    *,
    method: DeliveryMethod,
    to_pvz: bool,
    goods_total: Decimal,
    city: str = "",
) -> Decimal:
    """Цена доставки. С городом — по его зоне, без города — по общей настройке.

    Город приходит пустым только из старых вызовов и из админки, где заказ
    правят задним числом: там зона уже посчитана и лежит в самом заказе.
    """
    option = await get_option(session, method)
    if option is None:
        return Decimal("0.00")

    free_from = await settings_service.get_decimal(session, "delivery.free_from")
    if free_from > 0 and goods_total >= free_from:
        return Decimal("0.00")

    if city:
        zone = await resolve_zone(session, city)
        if zone is None:
            raise UnknownCityError(city)
        prices = await zone_prices(session, zone)
        row = prices.get(method)
        if row is not None:
            return Decimal(str(row.pvz_price if to_pvz else row.courier_price))
        # Зона есть, а цены перевозчика в ней нет: перевозчика завели позже.
        # Общая настройка тут лучше нуля — ноль это «бесплатно», а не «не знаю».
        log.warning("delivery.zone_price_missing", zone=zone.code, method=str(method))
    return option.price_for(to_pvz=to_pvz)


def tracking_url(method: DeliveryMethod, track: str) -> str | None:
    template = TRACKING_URLS.get(method)
    return template.format(track=track) if template and track else None


def attach_delivery(
    order: Order,
    *,
    method: DeliveryMethod,
    price: Decimal,
    city: str,
    recipient_name: str,
    recipient_phone: str,
    address: str | None = None,
    pvz_code: str | None = None,
    pvz_address: str | None = None,
) -> Delivery:
    delivery = Delivery(
        order=order,
        method=method,
        price=price,
        city=city,
        address=address,
        pvz_code=pvz_code,
        pvz_address=pvz_address,
        recipient_name=recipient_name,
        recipient_phone=recipient_phone,
    )
    order.delivery = delivery
    return delivery


class CarrierClient(ABC):
    """Интерфейс для будущей интеграции с API перевозчика."""

    method: DeliveryMethod

    @abstractmethod
    async def create_shipment(self, order: Order) -> str:
        """Создаёт отправление и возвращает трек-номер."""

    @abstractmethod
    async def get_status(self, tracking_number: str) -> str: ...


class ManualCarrier(CarrierClient):
    """Режим v1: отправление оформляет логист, трек вводится в админке."""

    def __init__(self, method: DeliveryMethod) -> None:
        self.method = method

    async def create_shipment(self, order: Order) -> str:
        return order.delivery.tracking_number if order.delivery else ""

    async def get_status(self, tracking_number: str) -> str:
        return ""

# ── Тарифные зоны ─────────────────────────────────────────────────────────────


class UnknownCityError(Exception):
    """Города нет ни в одной зоне — цену назвать нечем.

    Оформление на этом останавливается намеренно: угадать цену значит либо
    отпугнуть клиента вдвое дороже, либо повезти себе в убыток через полстраны.
    """


_CITY_PREFIXES = frozenset({"г", "гор", "город", "пос", "поселок", "с", "село", "п", "рп"})
"""Слова, с которых начинают адрес, а не название: «г. Самара», «пос. Кинель».

Отбрасываются уже после разбора на слова, а не шаблоном по строке: люди пишут
и «г.Самара» без пробела, и «г . Самара» — по строке это разные случаи,
по словам один.
"""


def normalize_city(value: str) -> str:
    """Приводит написанное клиентом к сравнимому виду.

    Люди пишут «г. Нижний Новгород», «нижний новгород», «Нижний-Новгород» —
    и всё это один город. Букву «ё» сводим к «е»: половина клавиатур её
    не набирает, а Королёв и Королев — не разные города.
    """
    text = (value or "").strip().lower().replace("ё", "е")
    words = [word for word in re.split(r"[^а-яa-z0-9]+", text) if word]
    while words and words[0] in _CITY_PREFIXES:
        words.pop(0)
    return " ".join(words)


async def resolve_zone(session: AsyncSession, city: str) -> DeliveryZone | None:
    """Ищет зону по названию города. `None` — города не знаем."""
    target = normalize_city(city)
    if not target:
        return None
    zones = await session.scalars(select(DeliveryZone).order_by(DeliveryZone.sort_order))
    for zone in zones:
        for line in (zone.cities or "").splitlines():
            if normalize_city(line) == target:
                return zone
    return None


async def remember_unknown_city(session: AsyncSession, city: str, *, tg_id: int | None) -> None:
    """Запоминает город, которого нет в зонах.

    Нужно оператору: он видит, куда просятся, добавляет город в зону и
    возвращается к человеку с ценой. Счётчик показывает, какой город заводить
    первым — тот, куда постучались десять раз, важнее случайного.
    """
    normalized = normalize_city(city)
    if not normalized:
        return
    row = await session.scalar(select(UnknownCity).where(UnknownCity.normalized == normalized))
    if row is None:
        session.add(
            UnknownCity(city=city.strip()[:120], normalized=normalized[:120], tg_id=tg_id)
        )
    else:
        row.hits += 1
        row.resolved = False
        row.tg_id = tg_id or row.tg_id
    log.info("delivery.unknown_city", city=city, tg_id=tg_id)


async def zone_prices(session: AsyncSession, zone: DeliveryZone) -> dict[DeliveryMethod, DeliveryZonePrice]:
    rows = await session.scalars(
        select(DeliveryZonePrice).where(DeliveryZonePrice.zone_id == zone.id)
    )
    return {row.method: row for row in rows}

