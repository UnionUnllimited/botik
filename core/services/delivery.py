"""Доставка: что выбирает клиент и как считается её цена.

Цену доставки называет оператор после оформления заказа, а не бот при
оформлении. Так решено 21 августа 2026, после недели жизни с тарифными зонами:
по зонам цену всё равно перебивали руками, а город, которого в зонах не
оказалось, останавливал оформление у живого клиента.

Клиент выбирает не перевозчика и не зону, а скорость: быстро и дороже или
дешевле, но ждать ближайшего понедельника. Перевозчик — забота оператора:
он зависит от города, веса и действующего договора, и клиенту эта развилка
ничего не объясняет.

Интеграция с API перевозчика вынесена за интерфейс `CarrierClient`: в v1
работает ручной режим, когда трек-номер вбивает логист.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.dates import utcnow
from core.enums import DeliveryMethod, DeliverySpeed
from core.models import Delivery, Order
from core.services import settings_service

log = structlog.get_logger("services.delivery")

TRACKING_URLS = {
    DeliveryMethod.CDEK: "https://www.cdek.ru/ru/tracking?order_id={track}",
    DeliveryMethod.POST: "https://www.pochta.ru/tracking#{track}",
    DeliveryMethod.BOXBERRY: "https://boxberry.ru/tracking-page?id={track}",
}
"""Для Яндекс Go ссылки нет: он присылает её клиенту сам, придумывать свою незачем."""


@dataclass(frozen=True, slots=True)
class SpeedOption:
    """Как клиент видит выбор скорости.

    Без цены: её называют после оформления. Обещать сумму заранее мы не можем —
    она зависит от города и габаритов, а обещанная и не сошедшаяся цена хуже
    честного «посчитаем и напишем».
    """

    speed: DeliverySpeed
    title: str
    description: str


DEFAULT_SPEED_TITLES = {
    DeliverySpeed.FAST: "🚀 Быстрая",
    DeliverySpeed.WEEKLY: "🗓 Обычная",
}

DEFAULT_SPEED_DESCRIPTIONS = {
    DeliverySpeed.FAST: (
        "Отправляем в течение двух рабочих дней, курьерской службой. "
        "Дороже, зато не нужно ждать понедельника."
    ),
    DeliverySpeed.WEEKLY: (
        "Отправляем партией по понедельникам. Дешевле, но если заказ сделан "
        "во вторник, посылка выедет на следующей неделе."
    ),
}

SPEED_SETTING_KEYS = {
    DeliverySpeed.FAST: ("delivery.fast_title", "delivery.fast_description"),
    DeliverySpeed.WEEKLY: ("delivery.weekly_title", "delivery.weekly_description"),
}
"""Названия и описания правятся в админке: это витрина, а не логика."""


async def speed_options(session: AsyncSession) -> list[SpeedOption]:
    """Варианты для экрана выбора. Порядок — от быстрого к дешёвому."""
    options: list[SpeedOption] = []
    for speed in (DeliverySpeed.FAST, DeliverySpeed.WEEKLY):
        title_key, description_key = SPEED_SETTING_KEYS[speed]
        title = await settings_service.get_setting(session, title_key)
        description = await settings_service.get_setting(session, description_key)
        options.append(
            SpeedOption(
                speed=speed,
                title=str(title or DEFAULT_SPEED_TITLES[speed]),
                description=str(description or DEFAULT_SPEED_DESCRIPTIONS[speed]),
            )
        )
    return options


def parse_speed(value: str) -> DeliverySpeed | None:
    try:
        return DeliverySpeed(str(value).strip())
    except ValueError:
        return None


def awaiting_quote(delivery: Delivery | None) -> bool:
    """Заказ ждёт, пока оператор назовёт цену доставки.

    Смотрим на отметку, а не на цену: ноль в цене — это «бесплатно», а не
    «ещё не считали», и по нему заказ уехал бы в сборку неоплаченным.
    """
    return delivery is not None and delivery.quoted_at is None


def set_quote(delivery: Delivery, price: Decimal, *, now: dt.datetime | None = None) -> None:
    delivery.price = price
    delivery.quoted_at = now or utcnow()


NO_DELIVERY = "none"
NOT_QUOTED = "not_quoted"
AWAITING_PAYMENT = "awaiting_payment"
PAID = "paid"


def state(delivery: Delivery | None) -> str:
    """Состояние доставки — отдельно от статуса заказа.

    Одним статусом это не выражается: заказ к этому моменту «Оплачен» —
    роутер и подписку клиент уже купил, — и при этом ждёт денег за перевозку.
    Смешав их, мы или потеряли бы оплату товара, или показали бы «ждёт оплаты»
    там, где ждать нечего.

    Порядок проверок важен: оплаченная доставка перестаёт быть ожидающей,
    а ноль — законная цена, и «бесплатно» отличается от «ещё не считали»
    отметкой `quoted_at`, а не суммой.
    """
    if delivery is None:
        return NO_DELIVERY
    if delivery.paid_at is not None:
        return PAID
    if delivery.quoted_at is None:
        return NOT_QUOTED
    return AWAITING_PAYMENT


def tracking_url(method: DeliveryMethod, track: str) -> str | None:
    template = TRACKING_URLS.get(method)
    return template.format(track=track) if template and track else None


def attach_delivery(
    order: Order,
    *,
    speed: DeliverySpeed,
    method: DeliveryMethod,
    city: str,
    recipient_name: str,
    recipient_phone: str,
    address: str | None = None,
    pvz_code: str | None = None,
    pvz_address: str | None = None,
) -> Delivery:
    """Заводит доставку к заказу. Цена остаётся нулевой до расчёта оператором."""
    delivery = Delivery(
        order=order,
        method=method,
        speed=speed,
        price=Decimal("0.00"),
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
