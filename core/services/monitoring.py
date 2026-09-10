"""Что в парке пошло не так — сводкой оператору.

Четыре вопроса, на которые до сих пор никто не отвечал, пока клиент не
позвонил: роутер у клиента перестал выходить на связь, отгруженная посылка
так и не включилась, роутер на связи, а подписка на нём не включилась,
подписка кончается и никто не продлевает.

Сводка, а не алерт на каждое событие. Алерты требуют помнить, о чём уже
сообщали, иначе один молчащий роутер шлёт сообщение каждый круг и его
перестают читать через день. Сводка раз в сутки такой памяти не требует
вовсе: она описывает состояние, а не происшествие.

Когда сказать нечего — не отправляется ничего. Ежедневное «всё в порядке»
читают неделю, а потом перестают, и вместе с ним перестают читать всё
остальное.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.dates import ensure_utc, utcnow
from core.enums import OrderStatus, SubscriptionStatus
from core.models import Device, Order, Subscription, User

log = structlog.get_logger("services.monitoring")

SILENT_HOURS = 24
"""Сутки. Роутер перезагружают, интернет у клиента моргает, туннель
поднимается не мгновенно — всё это укладывается в часы, а не в сутки."""

SHIPPED_SILENT_DAYS = 7
"""Неделя с отгрузки. СДЭК по стране идёт до пяти дней, и раньше срока
«посылка не включилась» означало бы «посылка ещё едет»."""

STUCK_HOURS = 3
"""Сколько часов роутер может быть на связи, не включив подписку.

Автоактивация пробует на каждом обходе присутствия, то есть десятки раз
в час, и откладывает попытку молча — туннель после включения поднимается
не сразу. Три часа таких попыток подряд — это уже не «не успел».
"""

EXPIRING_DAYS = 3
"""За сколько дней до конца подписки показывать её оператору. Клиенту
напоминания уходят раньше и не один раз; это список для того, кто будет
звонить, если напоминания не сработали."""


@dataclass(slots=True)
class Digest:
    """Что нашлось. Пустой — значит поводов писать нет."""

    silent: list[Device] = field(default_factory=list)
    shipped_silent: list[tuple[Order, Device]] = field(default_factory=list)
    stuck: list[tuple[Order, Device]] = field(default_factory=list)
    expiring: list[tuple[Subscription, User]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.silent or self.shipped_silent or self.stuck or self.expiring)


def _last_seen(device: Device) -> dt.datetime | None:
    seen = (device.last_heartbeat_at, device.last_poll_at, device.frp_last_seen_at)
    # Приводим к UTC здесь, а не у каждого сравнения: часть драйверов отдаёт
    # дату без пояса, и сравнение с «сейчас» на такой падает целиком.
    latest = max((value for value in seen if value), default=None)
    return ensure_utc(latest) if latest is not None else None


async def collect(session: AsyncSession, *, now: dt.datetime | None = None) -> Digest:
    """Собирает сводку по парку. Ничего не меняет и никому не пишет."""
    now = now or utcnow()
    digest = Digest()

    # 1. Роутер у клиента молчит сутки. Берём только активированные: коробка
    # на складе молчит по определению, и сообщать об этом незачем.
    silent_since = now - dt.timedelta(hours=SILENT_HOURS)
    for device in await session.scalars(
        select(Device)
        .where(Device.activated_at.is_not(None), Device.frp_online.is_(False))
        .options(selectinload(Device.user))
    ):
        seen = _last_seen(device)
        if seen is None or seen < silent_since:
            digest.silent.append(device)

    # 2. Заказ отгружен неделю назад, а роутер ни разу не вышел на связь.
    # Либо посылка потерялась, либо клиент её не включил — и то и другое
    # оператор узнаёт от клиента, а должен раньше.
    shipped_before = now - dt.timedelta(days=SHIPPED_SILENT_DAYS)
    for order in await session.scalars(
        select(Order).where(
            Order.status == OrderStatus.SHIPPED,
            Order.shipped_at.is_not(None),
            Order.shipped_at < shipped_before,
        )
    ):
        device = await session.scalar(select(Device).where(Device.order_id == order.id))
        if device is not None and device.activated_at is None and _last_seen(device) is None:
            digest.shipped_silent.append((order, device))

    # 3. Роутер у клиента на связи, а подписка так и не включилась.
    #
    # В прошлые два повода такой роутер не попадает ни одним: он не молчит,
    # и активированным его тоже не назвать. Автоактивация же откладывает
    # попытку молча — панель не ответила, SSH отказал, ссылка не легла, — и
    # пробует снова на каждом обходе. Хоть год. Клиент при этом видит
    # «подписка настраивается» на работающем роутере, платёж прошёл, и
    # узнавали мы об этом от него.
    # «Активирован» и «Закрыт» сюда не берём: там активация уже случилась.
    stuck_before = now - dt.timedelta(hours=STUCK_HOURS)
    for order in await session.scalars(
        select(Order).where(
            Order.status.in_((OrderStatus.SHIPPED, OrderStatus.DELIVERED)),
            Order.shipped_at.is_not(None),
            Order.shipped_at < stuck_before,
        )
    ):
        # Клиента подтягиваем сразу: сводку собирает не тот код, что её пишет,
        # а дотянуться до него потом — это скрытый запрос из чужой корутины.
        device = await session.scalar(
            select(Device).where(Device.order_id == order.id).options(selectinload(Device.user))
        )
        if device is None or device.activated_at is not None:
            continue
        seen = _last_seen(device)
        if seen is not None and seen >= silent_since:
            digest.stuck.append((order, device))

    # 4. Подписка кончается, продления нет. Клиенту напоминания уже ушли —
    # это список для того, кто будет звонить, если они не сработали.
    expiring_until = now + dt.timedelta(days=EXPIRING_DAYS)
    for subscription in await session.scalars(
        select(Subscription).where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expires_at.is_not(None),
            Subscription.expires_at <= expiring_until,
            Subscription.expires_at > now,
        )
    ):
        user = await session.get(User, subscription.user_id)
        if user is not None:
            digest.expiring.append((subscription, user))

    log.info(
        "monitoring.collected",
        silent=len(digest.silent),
        shipped_silent=len(digest.shipped_silent),
        stuck=len(digest.stuck),
        expiring=len(digest.expiring),
    )
    return digest
