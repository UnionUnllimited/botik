"""Доставка: варианты, цены и создание записи к заказу.

Цены берутся из настроек (правятся в админке). Интеграция с API перевозчика
вынесена за интерфейс `CarrierClient`: в v1 работает ручной режим, когда
трек-номер вбивает логист.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from core.enums import DeliveryMethod
from core.models import Delivery, Order
from core.services import settings_service

TRACKING_URLS = {
    DeliveryMethod.CDEK: "https://www.cdek.ru/ru/tracking?order_id={track}",
    DeliveryMethod.POST: "https://www.pochta.ru/tracking#{track}",
    DeliveryMethod.BOXBERRY: "https://boxberry.ru/tracking-page?id={track}",
}


@dataclass(frozen=True, slots=True)
class DeliveryOption:
    method: DeliveryMethod
    title: str
    pvz_price: Decimal
    courier_price: Decimal
    days: str

    @property
    def is_pickup(self) -> bool:
        return self.method is DeliveryMethod.PICKUP

    def price_for(self, *, to_pvz: bool) -> Decimal:
        return self.pvz_price if to_pvz else self.courier_price


async def get_options(session: AsyncSession) -> list[DeliveryOption]:
    raw = await settings_service.get_setting(session, "delivery.methods") or {}
    options: list[DeliveryOption] = []
    for method in DeliveryMethod:
        config = raw.get(method.value)
        if not isinstance(config, dict):
            continue
        options.append(
            DeliveryOption(
                method=method,
                title=str(config.get("title", method.value)),
                pvz_price=Decimal(str(config.get("pvz", "0.00"))),
                courier_price=Decimal(str(config.get("courier", "0.00"))),
                days=str(config.get("days", "")),
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
) -> Decimal:
    option = await get_option(session, method)
    if option is None:
        return Decimal("0.00")
    free_from = await settings_service.get_decimal(session, "delivery.free_from")
    if free_from > 0 and goods_total >= free_from:
        return Decimal("0.00")
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
