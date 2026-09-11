"""Заказы: сборка из черновика, суммы, смена статусов."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core import texts
from core.dates import to_display
from core.enums import DeliveryMethod, DeliverySpeed, OrderItemType, OrderStatus
from core.models import Delivery, Order, OrderItem, Plan, Product, PromoCode, User
from core.services import delivery as delivery_service
from core.services import promo as promo_service

log = structlog.get_logger("services.orders")

MONEY = Decimal("0.01")

# Разрешённые переходы. Всё остальное — ошибка оператора, а не «на всякий случай».
ALLOWED_TRANSITIONS: dict[OrderStatus, tuple[OrderStatus, ...]] = {
    # PACKING из NEW разрешён для заказов с оплатой при получении: деньги придут
    # от перевозчика, а собирать посылку нужно сразу.
    OrderStatus.NEW: (
        OrderStatus.AWAITING_PAYMENT,
        OrderStatus.PAID,
        OrderStatus.PACKING,
        OrderStatus.CANCELLED,
    ),
    OrderStatus.AWAITING_PAYMENT: (OrderStatus.PAID, OrderStatus.CANCELLED),
    OrderStatus.PAID: (OrderStatus.PACKING, OrderStatus.SHIPPED, OrderStatus.CANCELLED, OrderStatus.REFUNDED),
    OrderStatus.PACKING: (OrderStatus.SHIPPED, OrderStatus.CANCELLED, OrderStatus.REFUNDED),
    # «Активирован» достижим прямо из «Отправлен»: роутер выходит на связь
    # у клиента раньше, чем оператор отметит доставку, — а отметить её может
    # и некому, трек-номер закрывается сам.
    OrderStatus.SHIPPED: (OrderStatus.DELIVERED, OrderStatus.ACTIVATED, OrderStatus.REFUNDED),
    OrderStatus.DELIVERED: (OrderStatus.ACTIVATED, OrderStatus.DONE, OrderStatus.REFUNDED),
    # Дальше идти некуда: роутер у клиента и работает. Остаётся только возврат.
    OrderStatus.ACTIVATED: (OrderStatus.REFUNDED,),
    OrderStatus.DONE: (OrderStatus.REFUNDED,),
    OrderStatus.CANCELLED: (),
    OrderStatus.REFUNDED: (),
}


class OrderError(Exception):
    """Ошибка оформления, текст которой можно показать клиенту."""


@dataclass(slots=True)
class OrderDraft:
    """Черновик заказа, собранный в диалоге бота."""

    product_id: int | None = None
    plan_id: int | None = None
    customer_name: str = ""
    customer_phone: str = ""
    customer_city: str = ""
    delivery_speed: DeliverySpeed | None = None
    """Что выбрал клиент: быстро и дороже или дешевле, но ждать неделю."""
    delivery_method: DeliveryMethod | None = None
    """Перевозчик. Клиент его не выбирает — ставит оператор при отгрузке."""
    delivery_to_pvz: bool = True
    delivery_address: str = ""
    pvz_code: str = ""
    pvz_address: str = ""
    promo_code: str = ""
    is_cod: bool = False
    comment: str = ""
    utm_source: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class OrderTotals:
    subtotal: Decimal = Decimal("0.00")
    discount: Decimal = Decimal("0.00")
    delivery: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    product: Product | None = None
    plan: Plan | None = None
    promo_result: promo_service.PromoResult | None = None


def _round(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def public_number(order_id: int, created_at: dt.datetime) -> str:
    """R-260803-0042 — короткий номер, который клиент называет в поддержке."""
    return f"R-{to_display(created_at):%y%m%d}-{order_id:04d}"


SOLD_OUT_STATUSES = (OrderStatus.CANCELLED, OrderStatus.REFUNDED)
"""Заказы, которые предел не занимают: отменённые и возвращённые."""


async def sold_units(session: AsyncSession, product_id: int) -> int:
    """Сколько роутеров этой модели уже разобрано заказами.

    Считаем по заказам, а не списываем число у товара. Списание пришлось бы
    возвращать при каждой отмене и возврате, а забытый возврат тихо съедает
    остаток: товар «кончился», хотя роутеры лежат. Счёт по заказам чинится сам.
    """
    total = await session.scalar(
        select(func.coalesce(func.sum(OrderItem.quantity), 0))
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            OrderItem.product_id == product_id,
            Order.status.notin_(SOLD_OUT_STATUSES),
        )
    )
    return int(total or 0)


async def units_left(session: AsyncSession, product: Product) -> int:
    """Сколько ещё можно продать по нынешнему пределу. Меньше нуля не бывает."""
    return max((product.stock or 0) - await sold_units(session, product.id), 0)


async def units_left_map(session: AsyncSession, products: list[Product]) -> dict[int, int]:
    """Пределы сразу для списка товаров — одним запросом.

    Витрину открывает случайный человек из поиска, и запрос на каждую
    карточку там лишний: моделей две сегодня и десять завтра, а страница
    одна и та же.
    """
    if not products:
        return {}
    rows = await session.execute(
        select(OrderItem.product_id, func.coalesce(func.sum(OrderItem.quantity), 0))
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            OrderItem.product_id.in_([product.id for product in products]),
            Order.status.notin_(SOLD_OUT_STATUSES),
        )
        .group_by(OrderItem.product_id)
    )
    sold = {product_id: int(total or 0) for product_id, total in rows}
    return {
        product.id: max((product.stock or 0) - sold.get(product.id, 0), 0)
        for product in products
    }


def sellable(product: Product, left: int | None) -> bool:
    """Показывать ли товар как доступный.

    Предзаказ продаётся всегда — на то он и предзаказ. `left is None` значит,
    что вызывающий предел не считал: тогда судим по самому числу, как раньше.
    """
    if product.allow_preorder:
        return True
    return product.stock > 0 if left is None else left > 0


async def calculate_totals(
    session: AsyncSession,
    *,
    draft: OrderDraft,
    user_id: int,
) -> OrderTotals:
    """Считает суммы заказа. Единственное место, где рождается итоговая цена."""
    totals = OrderTotals()

    if draft.product_id:
        product = await session.get(Product, draft.product_id)
        if product is None or not product.is_active:
            raise OrderError("Этот роутер больше не продаётся")
        # Остаток — предел, который оператор ставит руками: сколько роутеров
        # он готов продать сейчас. Прошиты они или ещё лежат в коробке — его
        # дело; наше дело не принять заказов больше предела.
        if not sellable(product, await units_left(session, product)):
            raise OrderError("Роутера нет в наличии")
        totals.product = product
        totals.subtotal += product.price

    if draft.plan_id:
        plan = await session.get(Plan, draft.plan_id)
        if plan is None or not plan.is_active:
            raise OrderError("Этот тариф больше не доступен")
        totals.plan = plan
        totals.subtotal += plan.price

    if totals.subtotal <= 0:
        raise OrderError("Пустой заказ")

    if draft.promo_code:
        totals.promo_result = await promo_service.validate(
            session,
            code=draft.promo_code,
            user_id=user_id,
            amount=totals.subtotal,
            product_id=draft.product_id,
            plan_id=draft.plan_id,
        )
        totals.discount = totals.promo_result.discount

    # Доставку в сумму заказа не кладём: её цену называет оператор после
    # оформления, а до того честной суммы нет. Клиент платит за роутер
    # и подписку, доставку — вторым платежом по нашей цене.

    totals.subtotal = _round(totals.subtotal)
    totals.discount = _round(totals.discount)
    totals.delivery = _round(totals.delivery)
    totals.total = _round(totals.subtotal - totals.discount + totals.delivery)
    if totals.total < 0:
        totals.total = Decimal("0.00")
    return totals


async def _hold_the_shelf(session: AsyncSession, product_id: int | None) -> None:
    """Занимает строку товара до конца сделки — чтобы последний роутер не ушёл дважды.

    Предел считается по заказам, а два заказа, пришедшие в одну секунду,
    считают его одновременно. Ни один не видит чужой незавершённой сделки:
    оба читают «остался один», оба проходят проверку, и оба оформляются.
    Клиент получает «заказ принят», а роутера на складе нет — и узнаёт об
    этом оператор при отгрузке, когда деньги уже взяты.

    Блокировка строки выстраивает такие заказы в очередь: второй ждёт первого
    и пересчитывает предел по настоящему остатку. Строка товара тут просто
    замок — саму её мы не меняем.

    SQLite про `FOR UPDATE` не знает и молча его пропускает; там сделки и не
    идут параллельно.
    """
    if not product_id:
        return
    await session.execute(select(Product.id).where(Product.id == product_id).with_for_update())


async def _hold_the_promo(session: AsyncSession, code: str) -> None:
    """Занимает строку промокода до конца сделки.

    Предел «столько-то раз» проверяется чтением, а растёт записью. Два
    заказа, пришедшие в одну секунду, читают одно и то же число и оба
    проходят последнее применение: код на один раз срабатывает дважды, и
    вторую скидку никто не назначал.

    Запираем только при оформлении: то же самое чтение идёт при каждом
    показе цены, и держать там замок значило бы выстраивать в очередь всех,
    кто просто вводит код в поле."""
    normalized = promo_service.normalize_code(code or "")
    if not normalized:
        return
    await session.execute(
        select(PromoCode.id).where(PromoCode.code == normalized).with_for_update()
    )


async def create_order(
    session: AsyncSession,
    *,
    user: User,
    draft: OrderDraft,
) -> Order:
    """Создаёт заказ со снимком цен и составом. Промокод фиксируется здесь же."""
    await _hold_the_shelf(session, draft.product_id)
    await _hold_the_promo(session, draft.promo_code)
    totals = await calculate_totals(session, draft=draft, user_id=user.id)

    order = Order(
        public_number="",
        user_id=user.id,
        status=OrderStatus.NEW,
        subtotal=totals.subtotal,
        discount_total=totals.discount,
        delivery_price=totals.delivery,
        total=totals.total,
        currency="RUB",
        is_cod=draft.is_cod,
        customer_name=draft.customer_name or user.full_name or user.display_name,
        customer_phone=draft.customer_phone or (user.phone or ""),
        customer_city=draft.customer_city,
        comment=draft.comment or None,
        utm_source=draft.utm_source or user.utm_source,
        promo_code_id=totals.promo_result.promo.id if totals.promo_result else None,
    )
    session.add(order)

    if totals.product is not None:
        order.items.append(
            OrderItem(
                item_type=OrderItemType.PRODUCT,
                product_id=totals.product.id,
                title=totals.product.title,
                quantity=1,
                unit_price=totals.product.price,
                total_price=totals.product.price,
                vat_code=totals.product.vat_code,
            )
        )
    if totals.plan is not None:
        order.items.append(
            OrderItem(
                item_type=OrderItemType.PLAN,
                plan_id=totals.plan.id,
                title=f"Подписка: {totals.plan.title}",
                quantity=1,
                unit_price=totals.plan.price,
                total_price=totals.plan.price,
                vat_code=totals.plan.vat_code,
                meta={"months": totals.plan.months, "extra_days": totals.plan.extra_days},
            )
        )
    # Строки «Доставка» в составе нет: её цена появится позже и уедет
    # отдельным платежом. Пустая строка на ноль рублей читалась бы как
    # «доставка бесплатная».

    # Раньше здесь стояло ещё «и в заказе есть товар». Условие было лишним
    # и вредным: заказ без строки товара (например, с нулевой ценой на акции)
    # терял выбранную клиентом доставку целиком, а оператор видел в карточке
    # прочерк там, где человек честно выбрал скорость и оставил адрес.
    if draft.delivery_speed is not None:
        delivery_service.attach_delivery(
            session,
            order,
            speed=draft.delivery_speed,
            method=draft.delivery_method or DeliveryMethod.CDEK,
            city=draft.customer_city,
            recipient_name=order.customer_name,
            recipient_phone=order.customer_phone,
            address=draft.delivery_address or None,
            pvz_code=draft.pvz_code or None,
            pvz_address=draft.pvz_address or None,
        )

    await session.flush()
    order.public_number = public_number(order.id, order.created_at or dt.datetime.now(dt.UTC))

    if totals.promo_result is not None:
        await promo_service.register_usage(
            session,
            promo=totals.promo_result.promo,
            user_id=user.id,
            order_id=order.id,
            discount=totals.discount,
        )

    # Контакты покупателя переиспользуем в следующих заказах.
    user.full_name = order.customer_name or user.full_name
    user.phone = order.customer_phone or user.phone
    user.city = order.customer_city or user.city

    log.info(
        "order.created",
        order_id=order.id,
        number=order.public_number,
        user_id=user.id,
        total=str(order.total),
        is_cod=order.is_cod,
    )
    return order


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, ())


def set_status(
    order: Order, target: OrderStatus, *, reason: str | None = None, force: bool = False
) -> None:
    """Меняет статус и проставляет отметки времени.

    Порядок переходов защищает автоматику: оплата, отгрузка и активация ходят
    по нему и не должны перескакивать через шаг. Человека он защищать не может
    и не должен — жизнь идёт не по схеме: посылку вернули, клиент передумал
    после отгрузки, заказ закрыли раньше времени. Поэтому оператор переводит
    заказ куда нужно, а `force` отличает такой перевод от машинного.
    """
    if order.status is target:
        return
    if not force and not can_transition(order.status, target):
        raise OrderError(f"Недопустимый переход {order.status} → {target}")

    now = dt.datetime.now(dt.UTC)
    order.status = target
    match target:
        case OrderStatus.PAID:
            order.paid_at = order.paid_at or now
        case OrderStatus.SHIPPED:
            order.shipped_at = now
            if order.delivery is not None:
                order.delivery.shipped_at = now
        case OrderStatus.DELIVERED:
            order.delivered_at = now
            if order.delivery is not None:
                order.delivery.delivered_at = now
        case OrderStatus.ACTIVATED:
            # Отметку доставки ставим заодно, если её не было: роутер работает
            # у клиента — значит посылка дошла, кто бы её ни отметил.
            order.delivered_at = order.delivered_at or now
            if order.delivery is not None:
                order.delivery.delivered_at = order.delivery.delivered_at or now
        case OrderStatus.CANCELLED:
            order.cancelled_at = now
            order.cancel_reason = reason
        case OrderStatus.REFUNDED:
            order.cancel_reason = reason
        case _:
            pass
    log.info("order.status_changed", order_id=order.id, status=str(target), reason=reason)


async def load_for_status(session: AsyncSession, order_id: int) -> Order | None:
    """Заказ, готовый к смене статуса.

    Именно так его и надо брать перед `set_status`: тот трогает `order.delivery`
    (ставит отметки об отгрузке и доставке), а у заказа, поднятого через
    `session.get`, связь не загружена. Обращение к ней уходит в базу синхронно,
    и под async-движком это `MissingGreenlet` — то есть пятисотка вместо
    смены статуса.
    """
    return await session.scalar(
        select(Order).where(Order.id == order_id).options(selectinload(Order.delivery))
    )


async def get_order(session: AsyncSession, order_id: int) -> Order | None:
    return await session.scalar(
        select(Order)
        .where(Order.id == order_id)
        .options(
            selectinload(Order.items),
            selectinload(Order.delivery),
            selectinload(Order.user),
        )
    )


async def list_user_orders(session: AsyncSession, user_id: int, *, limit: int = 10) -> list[Order]:
    result = await session.scalars(
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.id.desc())
        .limit(limit)
        .options(selectinload(Order.items), selectinload(Order.delivery))
    )
    return list(result)


SPEED_SUMMARY = {
    DeliverySpeed.FAST: "быстрая",
    DeliverySpeed.WEEKLY: "раз в неделю",
}


def delivery_summary(delivery: Delivery | None) -> str:
    """Строка доставки для карточек и выгрузки.

    Скорость впереди перевозчика: её выбирал клиент, и по ней оператор
    понимает, срочный это заказ или ждёт партии.
    """
    if delivery is None:
        return "—"
    if delivery.method is DeliveryMethod.PICKUP:
        return texts.DELIVERY_METHOD_TITLES[DeliveryMethod.PICKUP]
    speed = SPEED_SUMMARY.get(delivery.speed, "")
    # Перевозчик человеческим именем: «СДЭК», а не «CDEK». Оператор ищет
    # в списке первое, а по коду из перечисления заказ не находится.
    carrier = texts.DELIVERY_METHOD_TITLES.get(delivery.method, delivery.method.value.upper())
    target = delivery.pvz_address or delivery.address or delivery.city
    head = f"{speed}, {carrier}" if speed else carrier
    return f"{head}, {target}"
