"""Каталог роутеров наружу — для бота и раздела «Каталог» в его админке.

Товары живут в одной таблице `products` у нас. Второй каталог в SQLite бота
завести было бы проще, но тогда цена оказалась бы в двух местах и разошлась
бы в первый же день распродажи. Поэтому схема та же, что у парка роутеров:
данные отдаём по HTTP, их бот показывает, их админка правит.

Здесь же оформление заказа: расчёт сумм, промокоды и снимок цен уже написаны
в `core/services/orders.py`, и дублировать их в чужом коде незачем. Бот
собирает ответы клиента и присылает их одним запросом.

Клиент опознаётся по `tg_id`: у бота свои пользователи в своей базе, у нас
свои в `users`, общего между ними только номер в Telegram.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.deps import get_session, get_transaction
from api.service_auth import require_token
from core import texts, validators
from core.config import settings
from core.dates import utcnow
from core.enums import (
    OFFERED_DELIVERY_METHODS,
    DeliveryMethod,
    DeviceStatus,
    OrderStatus,
    PaymentProviderName,
    PaymentPurpose,
    VatCode,
)
from core.models import Device, Notification, Order, Payment, Plan, Product, User
from core.security import normalize_mac
from core.services import activation, media, settings_service
from core.services import delivery as delivery_service
from core.services import orders as order_service
from core.services import payments as payment_service
from core.services import promo as promo_service
from core.services import subscriptions as subscription_service

log = structlog.get_logger("api.catalog")

router = APIRouter(
    prefix="/api/v1/catalog",
    tags=["catalog"],
    include_in_schema=False,
    dependencies=[Depends(require_token)],
)


# --- Товары ------------------------------------------------------------------


def _photo_url(product: Product) -> str:
    """Абсолютная ссылка на картинку: её тянет Telegram, а он ходит снаружи."""
    raw = product.photo_url or ""
    if not raw or raw.startswith("http"):
        return raw
    return f"{settings.api.public_base_url.rstrip('/')}{raw}"


def _product_payload(product: Product) -> dict[str, Any]:
    """Деньги строкой, а не числом: float по дороге теряет копейки."""
    return {
        "id": product.id,
        "slug": product.slug,
        "title": product.title,
        "subtitle": product.subtitle or "",
        "description": product.description or "",
        "model_code": product.model_code or "",
        "price": str(product.price),
        "old_price": str(product.old_price) if product.old_price is not None else "",
        "vat_code": str(product.vat_code),
        "stock": product.stock,
        "allow_preorder": product.allow_preorder,
        "in_stock": product.in_stock,
        "is_active": product.is_active,
        "sort_order": product.sort_order,
        "specs": product.specs or {},
        "photo_url": _photo_url(product),
        "photo_path": product.photo_url or "",
    }


@router.get("/products")
async def list_products(
    session: AsyncSession = Depends(get_session),
    include_hidden: bool = Query(default=False, alias="all"),
) -> dict:
    """Список моделей. Боту — только то, что продаётся, админке — всё."""
    query = select(Product).order_by(Product.sort_order, Product.id)
    if not include_hidden:
        query = query.where(Product.is_active.is_(True))
    products = list(await session.scalars(query))
    return {
        "total": len(products),
        "currency": settings.app.currency,
        "products": [_product_payload(product) for product in products],
    }


async def _product_or_404(session: AsyncSession, product_id: int) -> Product:
    product = await session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return product


@router.get("/products/{product_id}")
async def product_card(product_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    product = await _product_or_404(session, product_id)
    return {"product": _product_payload(product), "currency": settings.app.currency}


def _iso_dt(value) -> str | None:
    return value.isoformat() if value else None


def _decimal(raw: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(raw).replace(",", ".").strip() or default)
    except InvalidOperation:
        return Decimal(default)


def _int(raw: Any, default: int = 0) -> int:
    try:
        return int(str(raw).strip() or default)
    except ValueError:
        return default


def _specs(raw: Any) -> dict[str, str] | None:
    """Характеристики приходят готовым объектом или строкой JSON из формы."""
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(key): str(value) for key, value in parsed.items()}


@router.post("/products/{product_id}")
async def save_product(
    product_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Сохранение карточки из админки бота. `product_id = 0` — создание новой."""
    product = await session.get(Product, product_id) if product_id else None
    creating = product is None

    title = str(payload.get("title", "")).strip()
    if creating:
        slug = str(payload.get("slug", "")).strip().lower()
        if not slug:
            return {"ok": False, "error": "Нужен slug — короткое имя латиницей."}
        exists = await session.scalar(select(Product).where(Product.slug == slug))
        if exists is not None:
            return {"ok": False, "error": "Товар с таким slug уже есть."}
        if not title:
            return {"ok": False, "error": "Нужно название."}
        product = Product(slug=slug, title=title, price=Decimal("0"))
        session.add(product)

    # Отсутствующий ключ и пустые характеристики — разные вещи: первое значит
    # «не трогай», и стирать ими заполненную таблицу нельзя. У новой карточки
    # значения по умолчанию ещё не проставлены — там пусто, а не None.
    specs = _specs(payload.get("specs")) if "specs" in payload else (product.specs or {})
    if specs is None:
        return {"ok": False, "error": 'Характеристики: ожидается объект JSON вида {"Порты": "3 LAN"}.'}

    product.title = title or product.title
    product.subtitle = str(payload.get("subtitle", "")).strip()
    product.description = str(payload.get("description", "")).strip()
    product.model_code = str(payload.get("model_code", "")).strip()
    product.price = _decimal(payload.get("price"), str(product.price))
    old_price = str(payload.get("old_price", "")).strip()
    product.old_price = _decimal(old_price) if old_price else None
    product.stock = _int(payload.get("stock"), product.stock)
    product.sort_order = _int(payload.get("sort_order"), product.sort_order)
    product.is_active = bool(payload.get("is_active"))
    product.allow_preorder = bool(payload.get("allow_preorder"))
    product.specs = specs

    vat = str(payload.get("vat_code", "")).strip()
    if vat:
        try:
            product.vat_code = VatCode(vat)
        except ValueError:
            return {"ok": False, "error": "Неизвестный код НДС."}

    # Ссылку на картинку разрешаем задать вручную: фото могло лежать на чужом
    # хостинге ещё до того, как появилась загрузка файлом.
    if "photo_path" in payload:
        photo_path = str(payload.get("photo_path", "")).strip()
        if not photo_path and product.photo_url:
            media.delete_image(product.photo_url)
        product.photo_url = photo_path or None

    await session.flush()
    log.info(
        "catalog.product_saved",
        product_id=product.id,
        created=creating,
        price=str(product.price),
        stock=product.stock,
    )
    return {"ok": True, "product": _product_payload(product)}


@router.post("/products/{product_id}/photo")
async def upload_photo(
    product_id: int,
    photo: UploadFile = File(...),
    session: AsyncSession = Depends(get_transaction),
) -> dict:
    """Картинка приходит файлом из формы админки и ложится в наш том `/media`."""
    product = await _product_or_404(session, product_id)
    try:
        saved = media.save_image(
            await photo.read(), photo.content_type or "", prefix=f"product-{product.id}"
        )
    except media.MediaError as exc:
        return {"ok": False, "error": str(exc)}

    media.delete_image(product.photo_url)
    product.photo_url = saved
    await session.flush()
    return {"ok": True, "product": _product_payload(product)}


@router.post("/products/{product_id}/delete")
async def delete_product(product_id: int, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Удаление карточки. Прошлые заказы не страдают: в них лежит снимок
    названия и цены, а ссылка на товар обнуляется самой базой."""
    product = await _product_or_404(session, product_id)
    title = product.title
    media.delete_image(product.photo_url)
    await session.delete(product)
    log.info("catalog.product_deleted", product_id=product_id, title=title)
    return {"ok": True}


# --- Сроки подписки ----------------------------------------------------------
#
# Срок выбирается вместе с роутером: без него оплата не из чего создаёт подписку,
# и приехавший роутер нечем активировать. Это те же тарифы, по которым считается
# цена и продление, — второго списка сроков заводить нельзя.


def _plan_payload(plan: Plan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "slug": plan.slug,
        "title": plan.title,
        "description": plan.description or "",
        "months": plan.months,
        "extra_days": plan.extra_days,
        "price": str(plan.price),
        "old_price": str(plan.old_price) if plan.old_price is not None else "",
        "price_per_month": str(plan.price_per_month),
        "is_active": plan.is_active,
        "is_default": plan.is_default,
        "sort_order": plan.sort_order,
    }


@router.get("/plans")
async def list_plans(
    session: AsyncSession = Depends(get_session),
    include_hidden: bool = Query(default=False, alias="all"),
) -> dict:
    query = select(Plan).order_by(Plan.sort_order, Plan.months)
    if not include_hidden:
        query = query.where(Plan.is_active.is_(True))
    plans = list(await session.scalars(query))
    return {"total": len(plans), "plans": [_plan_payload(plan) for plan in plans]}


@router.post("/plans/{plan_id}")
async def save_plan(
    plan_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Сохранение срока из админки бота. `plan_id = 0` — создание нового."""
    plan = await session.get(Plan, plan_id) if plan_id else None
    creating = plan is None
    title = str(payload.get("title", "")).strip()

    if creating:
        slug = str(payload.get("slug", "")).strip().lower()
        if not slug:
            return {"ok": False, "error": "Нужен slug — короткое имя латиницей."}
        if await session.scalar(select(Plan).where(Plan.slug == slug)) is not None:
            return {"ok": False, "error": "Срок с таким slug уже есть."}
        if not title:
            return {"ok": False, "error": "Нужно название."}
        plan = Plan(slug=slug, title=title, months=1, price=Decimal("0"))
        session.add(plan)

    plan.title = title or plan.title
    plan.description = str(payload.get("description", "")).strip()
    plan.months = max(_int(payload.get("months"), plan.months), 0)
    plan.extra_days = max(_int(payload.get("extra_days"), plan.extra_days), 0)
    plan.price = _decimal(payload.get("price"), str(plan.price))
    old_price = str(payload.get("old_price", "")).strip()
    plan.old_price = _decimal(old_price) if old_price else None
    plan.sort_order = _int(payload.get("sort_order"), plan.sort_order)
    plan.is_active = bool(payload.get("is_active"))

    if plan.months <= 0 and plan.extra_days <= 0:
        return {"ok": False, "error": "Срок пустой: укажите месяцы или дни."}

    await session.flush()
    log.info("catalog.plan_saved", plan_id=plan.id, created=creating, months=plan.months)
    return {"ok": True, "plan": _plan_payload(plan)}


@router.post("/plans/{plan_id}/delete")
async def delete_plan(plan_id: int, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Удаление срока. Оплаченные подписки не страдают: у них свой снимок
    условий, а ссылка на тариф обнуляется самой базой."""
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    await session.delete(plan)
    log.info("catalog.plan_deleted", plan_id=plan_id, title=plan.title)
    return {"ok": True}


# --- Доставка и проверка полей ----------------------------------------------


@router.get("/delivery")
async def delivery_options(session: AsyncSession = Depends(get_session)) -> dict:
    """Способы доставки с ценами — их показывает бот при оформлении."""
    options = await delivery_service.get_options(session)
    free_from = await settings_service.get_decimal(session, "delivery.free_from")
    return {
        "free_from": str(free_from),
        "options": [
            {
                "method": str(option.method),
                "title": option.title,
                "pvz_price": str(option.pvz_price),
                "courier_price": str(option.courier_price),
                "days": option.days,
            }
            for option in options
        ],
    }


_CLEANERS = {
    "name": validators.clean_full_name,
    "phone": validators.clean_phone,
    "city": validators.clean_city,
    "address": validators.clean_address,
    "pvz": validators.clean_pvz,
}

_COMPLAINTS = {
    "name": "Нужны фамилия и имя целиком, буквами. Например: Иванов Иван",
    "phone": "Не похоже на российский номер. Например: +7 900 123-45-67",
    "city": "Название города буквами, без цифр",
    "address": "Адрес слишком короткий — улица, дом, квартира",
    "pvz": "Пункт выдачи слишком короткий",
}


@router.post("/validate")
async def validate_field(payload: dict) -> dict:
    """Проверка одного поля заказа.

    Правила живут в `core/validators.py` и повторять их в чужом боте нельзя:
    разъехавшись, они пропустят телефон, на который потом не дозвонится
    перевозчик. Бот спрашивает по полю за шаг и получает уже причёсанное
    значение — телефон в едином формате, лишние пробелы убраны.
    """
    field = str(payload.get("field", "")).strip()
    cleaner = _CLEANERS.get(field)
    if cleaner is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="unknown_field")

    value = cleaner(str(payload.get("value", "")))
    if not value:
        return {"ok": False, "error": _COMPLAINTS[field]}
    return {"ok": True, "value": value}


# --- Клиент и его роутер -----------------------------------------------------


@router.post("/clients")
async def register_client(payload: dict, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Заводит клиента при первом обращении в бот.

    До каталога клиент появлялся у нас только вместе с заказом, и до тех пор
    роутер не к кому было привязать: оператор вводит MAC при отгрузке, а строки
    в `users` ещё нет. Поэтому бот отмечается здесь при входе.
    """
    user = await _user_by_tg(session, payload, create=True)
    username = str(payload.get("username", "")).strip()[:64]
    first_name = str(payload.get("first_name", "")).strip()[:128]
    # Имя в Telegram меняется, и хранить первое навсегда незачем.
    if username and user.username != username:
        user.username = username
    if first_name and user.first_name != first_name:
        user.first_name = first_name
    return {"ok": True, "id": user.id}


@router.get("/my-router")
async def my_router(tg_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Экран «Мой роутер»: что с устройством и подпиской.

    Роутер привязывается к клиенту оператором при отгрузке — по MAC, который
    уходит в посылке. Поэтому здесь же показывается и состояние заказа: пока
    роутер в пути, это единственное, что клиенту интересно.
    """
    user = await session.scalar(select(User).where(User.tg_id == tg_id))
    if user is None:
        return {"has_client": False, "router": None, "order": None}

    device = await session.scalar(
        select(Device).where(Device.user_id == user.id).order_by(Device.id.desc())
    )
    order = await session.scalar(
        select(Order)
        .where(Order.user_id == user.id)
        .order_by(Order.id.desc())
        .options(selectinload(Order.delivery), selectinload(Order.items))
    )

    router_payload = None
    if device is not None:
        now = utcnow()
        # Срок знает панель: подписку роутеру выдаёт она, а не мы.
        panel_until = None
        if settings.remnawave.is_configured:
            try:
                account = await asyncio.wait_for(activation.panel_account_of(device), timeout=3)
                panel_until = activation.panel_expiry_of(account)
            except TimeoutError:
                log.warning("catalog.panel_timeout", device_id=device.id)

        router_payload = {
            "mac": device.mac,
            "model": device.model or "",
            "status": str(device.status),
            "online": device.frp_online
            or device.is_online(threshold_min=settings.subscription.heartbeat_offline_min, now=now),
            "activated": device.activated_at is not None,
            "clients": (device.clients_wifi or 0) + (device.clients_dhcp or 0),
            "uptime_sec": device.uptime_sec or 0,
            "rx_bytes": device.rx_bytes or 0,
            "tx_bytes": device.tx_bytes or 0,
            "until": panel_until.isoformat() if panel_until else None,
            "active": bool(panel_until and panel_until > now),
        }

    return {
        "has_client": True,
        "router": router_payload,
        "order": _order_payload(order) if order is not None else None,
    }


# --- Очередь сообщений -------------------------------------------------------
#
# Отправляет их бот: клиент разговаривает с ним, и токен есть только у него.
# Мы кладём готовый текст, бот забирает пачку, отправляет и отчитывается.

OUTBOX_MAX_ATTEMPTS = 5
"""После пяти неудач перестаём предлагать сообщение: доставить его уже нечем,
а очередь не должна расти вечно из-за одного заблокировавшего бота клиента."""


@router.get("/outbox")
async def outbox(limit: int = 20, session: AsyncSession = Depends(get_session)) -> dict:
    """Что отправить клиентам прямо сейчас."""
    pending = list(
        await session.scalars(
            select(Notification)
            .where(
                Notification.sent_at.is_(None),
                Notification.attempts < OUTBOX_MAX_ATTEMPTS,
            )
            .order_by(Notification.id)
            .limit(max(min(limit, 100), 1))
        )
    )
    return {
        "messages": [
            {
                "id": item.id,
                "tg_id": item.tg_id,
                "text": item.text,
                "buttons": item.buttons or [],
                "kind": item.kind,
            }
            for item in pending
        ]
    }


@router.post("/outbox/{message_id}/ack")
async def outbox_ack(
    message_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Отчёт бота о судьбе сообщения.

    `blocked` — клиент закрылся от бота: помечаем его у себя, чтобы не копить
    ему очередь, и сообщение больше не предлагаем. Прочие ошибки просто
    считаем попытками — связь и Telegram отказывают временно.
    """
    message = await session.get(Notification, message_id)
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")

    if payload.get("ok"):
        message.sent_at = utcnow()
        message.last_error = None
        return {"ok": True}

    message.attempts += 1
    message.last_error = str(payload.get("error", ""))[:500]
    if payload.get("blocked"):
        message.attempts = OUTBOX_MAX_ATTEMPTS
        await session.execute(
            update(User)
            .where(User.tg_id == message.tg_id, User.bot_blocked.is_(False))
            .values(bot_blocked=True, bot_blocked_at=utcnow())
        )
        log.info("catalog.outbox_blocked", tg_id=message.tg_id)
    return {"ok": True}


# --- Продление ---------------------------------------------------------------
#
# Продлевать подписку роутера умеет только наша цепочка: учётка в панели заведена
# на MAC устройства (`tg{id}_{mac}`), и продление двигает срок именно ей. Их
# собственное продление работает с учёткой `tg{id}` — это подписка для приложения
# на телефоне, к роутеру отношения не имеющая: клиент заплатил бы, а доступ
# на роутере не продлился.


@router.get("/renew")
async def renew_state(tg_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Что показать на экране продления: текущий срок и доступные периоды."""
    user = await session.scalar(select(User).where(User.tg_id == tg_id))
    subscription = (
        await subscription_service.get_current(session, user.id) if user is not None else None
    )
    plans = list(
        await session.scalars(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.months)
        )
    )
    return {
        "has_client": user is not None,
        "subscription": {
            "status": str(subscription.status) if subscription else "",
            "until": _iso_dt(subscription.expires_at) if subscription else None,
        },
        "plans": [_plan_payload(plan) for plan in plans],
    }


@router.post("/renew")
async def renew_start(payload: dict, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Ссылка на оплату продления. Срок двигается уже по факту оплаты."""
    user = await _user_by_tg(session, payload, create=False)
    if user is None:
        return {"ok": False, "error": "Сначала оформите заказ — продлевать пока нечего."}

    plan = await session.get(Plan, _int(payload.get("plan_id")))
    if plan is None or not plan.is_active:
        return {"ok": False, "error": "Этот срок больше не продаётся."}

    subscription = await subscription_service.get_current(session, user.id)
    if subscription is None:
        return {
            "ok": False,
            "error": "Подписки нет. Она появится вместе с заказом роутера.",
        }

    try:
        payment = await payment_service.start_payment(
            session,
            user=user,
            provider_name=PaymentProviderName.PLATEGA,
            amount=plan.price,
            purpose=PaymentPurpose.SUBSCRIPTION,
            description=f"Продление подписки: {plan.title}",
            plan=plan,
            subscription=subscription,
        )
    except Exception as exc:  # noqa: BLE001 — причина уже описана человеческим языком
        log.warning("catalog.renew_payment_failed", tg_id=tg_id_of(payload), error=str(exc))
        return {"ok": False, "error": f"Оплата сейчас недоступна: {exc}"}

    return {"ok": True, "pay_url": payment.confirmation_url or "", "plan": _plan_payload(plan)}


def tg_id_of(payload: dict) -> int:
    return _int(payload.get("tg_id"))


# --- Заказ -------------------------------------------------------------------


async def _user_by_tg(session: AsyncSession, payload: dict, *, create: bool) -> User | None:
    """Клиент по номеру в Telegram. У бота своя база пользователей, у нас своя —
    общее между ними только это число."""
    tg_id = _int(payload.get("tg_id"))
    if not tg_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="tg_id_required")

    user = await session.scalar(select(User).where(User.tg_id == tg_id))
    if user is None and create:
        user = User(
            tg_id=tg_id,
            username=str(payload.get("username", "")).strip()[:64] or None,
            first_name=str(payload.get("first_name", "")).strip()[:128] or None,
        )
        session.add(user)
        await session.flush()
    return user


def _draft(payload: dict) -> order_service.OrderDraft:
    """Черновик из ответов клиента. Значения уже проверены `/validate`,
    но приходят от чужого процесса — приводим повторно."""
    method_raw = str(payload.get("delivery_method", "")).strip()
    try:
        method = DeliveryMethod(method_raw) if method_raw else None
    except ValueError:
        method = None

    to_pvz = bool(payload.get("delivery_to_pvz", True))
    return order_service.OrderDraft(
        product_id=_int(payload.get("product_id")) or None,
        plan_id=_int(payload.get("plan_id")) or None,
        customer_name=validators.clean_full_name(str(payload.get("name", ""))),
        customer_phone=validators.clean_phone(str(payload.get("phone", ""))),
        customer_city=validators.clean_city(str(payload.get("city", ""))),
        delivery_method=method,
        delivery_to_pvz=to_pvz,
        delivery_address="" if to_pvz else validators.clean_address(str(payload.get("address", ""))),
        pvz_address=validators.clean_pvz(str(payload.get("address", ""))) if to_pvz else "",
        promo_code=str(payload.get("promo_code", "")).strip(),
        comment=str(payload.get("comment", "")).strip()[:500],
        utm_source="bot",
    )


def _totals_payload(totals: order_service.OrderTotals) -> dict:
    promo = totals.promo_result
    return {
        "subtotal": str(totals.subtotal),
        "discount": str(totals.discount),
        "delivery": str(totals.delivery),
        "total": str(totals.total),
        "currency": settings.app.currency,
        "product": _product_payload(totals.product) if totals.product else None,
        "plan": _plan_payload(totals.plan) if totals.plan else None,
        "promo": {"code": promo.promo.code, "discount": str(totals.discount)} if promo else None,
    }


@router.post("/orders/quote")
async def quote_order(payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    """Суммы заказа до его создания — экран подтверждения в боте.

    Клиента здесь не заводим: предпросмотр не повод создавать строку в `users`.
    Пока его нет, промокод «на одного человека» считается неиспользованным —
    при оформлении он будет проверен ещё раз, уже с настоящим id.
    """
    user = await _user_by_tg(session, payload, create=False)
    try:
        totals = await order_service.calculate_totals(
            session, draft=_draft(payload), user_id=user.id if user else 0
        )
    except (order_service.OrderError, promo_service.PromoError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **_totals_payload(totals)}


_CANCELLABLE = (OrderStatus.NEW, OrderStatus.AWAITING_PAYMENT)
"""Пока не оплачен — клиент отменяет сам. Дальше только через поддержку:
деньги уже у нас, и отмена превращается в возврат."""


def _order_payload(order: Order) -> dict:
    """Заказ должен приходить сюда с загруженными составом и доставкой:
    ленивая подгрузка в асинхронной сессии — это исключение, а не запрос."""
    return {
        "id": order.id,
        "number": order.public_number,
        "status": str(order.status),
        "subtotal": str(order.subtotal),
        "discount": str(order.discount_total),
        "delivery": str(order.delivery_price),
        "total": str(order.total),
        "currency": order.currency,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "items": [
            {"title": item.title, "total": str(item.total_price)} for item in (order.items or [])
        ],
        "delivery_summary": order_service.delivery_summary(order.delivery),
        "tracking_number": (order.delivery.tracking_number or "") if order.delivery else "",
    }


@router.post("/orders")
async def create_order(payload: dict, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Оформление заказа и ссылка на оплату."""
    user = await _user_by_tg(session, payload, create=True)
    if user.is_blocked:
        return {"ok": False, "error": "Заказ оформить нельзя. Напишите в поддержку."}

    draft = _draft(payload)
    if not draft.customer_name or not draft.customer_phone:
        return {"ok": False, "error": "Не хватает имени или телефона."}

    try:
        order = await order_service.create_order(session, user=user, draft=draft)
    except (order_service.OrderError, promo_service.PromoError) as exc:
        return {"ok": False, "error": str(exc)}

    # Ссылка на оплату — необязательная часть: заказ уже принят, и падать
    # из-за недоступного провайдера нельзя. Не вышло — заказ ждёт оплаты,
    # а зависший платёж уберёт `expire_stale_payments`.
    pay_url = ""
    payment_error = ""
    try:
        payment = await payment_service.start_payment(
            session,
            user=user,
            provider_name=PaymentProviderName.PLATEGA,
            amount=order.total,
            purpose=PaymentPurpose.ORDER,
            description=f"Заказ {order.public_number}",
            order=order,
        )
        pay_url = payment.confirmation_url or ""
    except Exception as exc:  # noqa: BLE001 — причина уже описана человеческим языком
        payment_error = str(exc)
        log.warning("catalog.payment_failed", order_id=order.id, error=payment_error)

    await session.flush()
    # Перечитываем со связями: у только что созданного заказа состав и доставка
    # для ответа не загружены, а тянуть их по одной в асинхронной сессии нельзя.
    saved = await order_service.get_order(session, order.id)
    return {
        "ok": True,
        "order": _order_payload(saved or order),
        "pay_url": pay_url,
        "payment_error": payment_error,
    }


@router.get("/orders")
async def list_orders(
    tg_id: int, limit: int = 10, session: AsyncSession = Depends(get_session)
) -> dict:
    """Заказы клиента — экран «Мои заказы» в боте."""
    user = await session.scalar(select(User).where(User.tg_id == tg_id))
    if user is None:
        return {"orders": []}

    found = await session.scalars(
        select(Order)
        .where(Order.user_id == user.id)
        .order_by(Order.id.desc())
        .limit(max(min(limit, 50), 1))
        .options(selectinload(Order.items), selectinload(Order.delivery))
    )
    return {"orders": [_order_payload(order) for order in found]}


# --- Управление заказами из админки бота -------------------------------------
#
# Заказов у них нет и быть не может: их продукт продаёт подписку, а не железо
# с посылкой и трек-номером. Поэтому раздел переезжает целиком — список,
# карточка, статусы, доставка и привязка роутера к заказу.

ORDERS_PAGE_SIZE = 30


def _manage_order_row(order: Order) -> dict:
    return {
        "id": order.id,
        "number": order.public_number,
        "status": str(order.status),
        "total": str(order.total),
        "customer": order.customer_name or (order.user.display_name if order.user else ""),
        "phone": order.customer_phone or "",
        "city": order.customer_city or "",
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "paid": order.paid_at is not None,
        "tracking_number": (order.delivery.tracking_number or "") if order.delivery else "",
    }


@router.get("/manage/orders")
async def manage_orders(
    status_filter: str = Query(default="", alias="status"),
    q: str = "",
    page: int = 1,
    session: AsyncSession = Depends(get_session),
) -> dict:
    page = max(page, 1)
    query = select(Order).options(selectinload(Order.user), selectinload(Order.delivery))
    counter = select(func.count()).select_from(Order)

    if status_filter:
        query = query.where(Order.status == status_filter)
        counter = counter.where(Order.status == status_filter)
    text = q.strip()
    if text:
        pattern = f"%{text}%"
        condition = or_(
            Order.public_number.ilike(pattern),
            Order.customer_name.ilike(pattern),
            Order.customer_phone.ilike(pattern),
            Order.customer_city.ilike(pattern),
        )
        query = query.where(condition)
        counter = counter.where(condition)

    total = await session.scalar(counter) or 0
    orders = list(
        await session.scalars(
            query.order_by(Order.id.desc())
            .limit(ORDERS_PAGE_SIZE)
            .offset((page - 1) * ORDERS_PAGE_SIZE)
        )
    )
    return {
        "total": total,
        "page": page,
        "pages": max((total + ORDERS_PAGE_SIZE - 1) // ORDERS_PAGE_SIZE, 1),
        "statuses": [str(item) for item in OrderStatus],
        "orders": [_manage_order_row(order) for order in orders],
    }


async def _order_or_404(session: AsyncSession, order_id: int) -> Order:
    order = await order_service.get_order(session, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return order


@router.get("/manage/orders/{order_id}")
async def manage_order_card(order_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    order = await _order_or_404(session, order_id)
    payments = list(
        await session.scalars(
            select(Payment).where(Payment.order_id == order.id).order_by(Payment.id.desc())
        )
    )
    devices = list(await session.scalars(select(Device).where(Device.order_id == order.id)))
    free_devices = list(
        await session.scalars(
            select(Device)
            .where(Device.status == DeviceStatus.NEW, Device.order_id.is_(None))
            .order_by(Device.id)
            .limit(50)
        )
    )
    delivery = order.delivery
    return {
        "order": _manage_order_row(order)
        | {
            "subtotal": str(order.subtotal),
            "discount": str(order.discount_total),
            "delivery_price": str(order.delivery_price),
            "comment": order.comment or "",
            "note": order.admin_note or "",
            "cancel_reason": order.cancel_reason or "",
            "paid_at": order.paid_at.isoformat() if order.paid_at else None,
            "shipped_at": order.shipped_at.isoformat() if order.shipped_at else None,
            "items": [
                {"title": item.title, "total": str(item.total_price)} for item in (order.items or [])
            ],
        },
        "delivery": {
            "method": str(delivery.method) if delivery else "",
            "summary": order_service.delivery_summary(delivery),
            "address": (delivery.pvz_address or delivery.address or "") if delivery else "",
            "recipient": delivery.recipient_name if delivery else "",
            "phone": delivery.recipient_phone if delivery else "",
            "tracking_number": (delivery.tracking_number or "") if delivery else "",
            "tracking_url": (delivery.tracking_url or "") if delivery else "",
        },
        "payments": [
            {
                "id": payment.id,
                "provider": str(payment.provider),
                "status": str(payment.status),
                "amount": str(payment.amount),
                "created_at": payment.created_at.isoformat() if payment.created_at else None,
            }
            for payment in payments
        ],
        "devices": [{"id": item.id, "mac": item.mac, "model": item.model or ""} for item in devices],
        "free_devices": [{"mac": item.mac, "model": item.model or ""} for item in free_devices],
        "next_statuses": [
            str(item) for item in OrderStatus if order_service.can_transition(order.status, item)
        ],
    }


def _status_notice(order: Order, reason: str) -> str:
    """Текст для клиента при смене статуса.

    Отправляет его их бот, а не мы: клиент разговаривает с ним, и сообщение
    от другого бота он в лучшем случае не узнает, а в худшем не получит вовсе.
    Мы только собираем текст — тексты заказов живут у нас.
    """
    template = texts.ORDER_STATUS_TEXTS.get(order.status)
    if template is None:
        return ""
    notice = template.format(number=order.public_number, reason=reason or "").strip()
    delivery = order.delivery
    if order.status is OrderStatus.SHIPPED and delivery and delivery.tracking_number:
        notice += "\n\n" + texts.TRACK_INFO.format(track=delivery.tracking_number)
        if delivery.tracking_url:
            notice += "\n" + delivery.tracking_url
    return notice


@router.post("/manage/orders/{order_id}/status")
async def manage_order_status(
    order_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    order = await _order_or_404(session, order_id)
    reason = str(payload.get("reason", "")).strip()
    try:
        order_service.set_status(order, OrderStatus(str(payload.get("status", ""))), reason=reason or None)
    except (order_service.OrderError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    await session.flush()
    return {
        "ok": True,
        "status": str(order.status),
        "tg_id": order.user.tg_id if order.user else None,
        "notice": _status_notice(order, reason),
    }


@router.post("/manage/orders/{order_id}/shipping")
async def manage_order_shipping(
    order_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    order = await _order_or_404(session, order_id)
    if order.delivery is None:
        return {"ok": False, "error": "У заказа нет доставки — трек-номеру негде лежать."}

    track = str(payload.get("tracking_number", "")).strip()[:64]
    order.delivery.tracking_number = track or None
    order.delivery.tracking_url = delivery_service.tracking_url(order.delivery.method, track)
    return {"ok": True, "tracking_url": order.delivery.tracking_url or ""}


@router.post("/manage/orders/{order_id}/device")
async def manage_order_device(
    order_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Привязка MAC к заказу при отгрузке — по нему клиент активирует роутер."""
    order = await _order_or_404(session, order_id)
    mac = normalize_mac(str(payload.get("mac", "")))
    if not mac:
        return {"ok": False, "error": "Некорректный MAC. Формат: A0:B1:C2:D3:E4:F5"}

    device = await session.scalar(select(Device).where(Device.mac == mac))
    if device is None:
        device = Device(mac=mac, model=str(payload.get("model", "")).strip()[:64], status=DeviceStatus.NEW)
        session.add(device)
        await session.flush()
    elif device.order_id and device.order_id != order.id:
        return {"ok": False, "error": f"MAC {mac} уже привязан к другому заказу."}

    device.order_id = order.id
    device.user_id = order.user_id
    if device.status is DeviceStatus.NEW:
        device.status = DeviceStatus.ASSIGNED
    log.info("catalog.device_attached", order_id=order.id, mac=mac)
    return {"ok": True, "mac": mac}


@router.post("/manage/orders/{order_id}/note")
async def manage_order_note(
    order_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    order = await session.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    order.admin_note = str(payload.get("note", "")).strip()[:2000] or None
    return {"ok": True}


# --- Настройки доставки ------------------------------------------------------


@router.get("/manage/delivery")
async def manage_delivery_read(session: AsyncSession = Depends(get_session)) -> dict:
    """Все способы, включая выключенные: иначе их нельзя было бы включить обратно."""
    options = await delivery_service.get_options(session, only_enabled=False)
    free_from = await settings_service.get_decimal(session, "delivery.free_from")
    return {
        "free_from": str(free_from),
        "options": [
            {
                "method": str(option.method),
                "title": option.title,
                "pvz_price": str(option.pvz_price),
                "courier_price": str(option.courier_price),
                "days": option.days,
                "enabled": option.enabled,
            }
            for option in options
        ],
    }


@router.post("/manage/delivery")
async def manage_delivery_save(
    payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Сохраняет цены и переключатели. Способы перечислены у нас, их набор
    правится кодом: перевозчик — это ещё и договор, а не строка в форме."""
    current = await settings_service.get_setting(session, "delivery.methods") or {}
    incoming = payload.get("options")
    if not isinstance(incoming, dict):
        return {"ok": False, "error": "Ожидается объект со способами доставки."}

    updated = dict(current)
    for method in OFFERED_DELIVERY_METHODS:
        raw = incoming.get(method.value)
        if not isinstance(raw, dict):
            continue
        block = dict(updated.get(method.value) or {})
        block["title"] = str(raw.get("title", block.get("title", method.value)))[:60]
        block["pvz"] = str(_decimal(raw.get("pvz"), str(block.get("pvz", "0"))))
        block["courier"] = str(_decimal(raw.get("courier"), str(block.get("courier", "0"))))
        block["days"] = str(raw.get("days", block.get("days", "")))[:40]
        block["enabled"] = bool(raw.get("enabled"))
        updated[method.value] = block

    await settings_service.set_setting(session, "delivery.methods", updated)
    await settings_service.set_setting(
        session, "delivery.free_from", str(_decimal(payload.get("free_from"), "0"))
    )
    log.info("catalog.delivery_saved")
    return {"ok": True}


@router.get("/orders/{order_id}")
async def order_card(
    order_id: int, tg_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    """Карточка заказа. `tg_id` обязателен: по одному номеру заказа нельзя
    показывать чужую доставку с адресом и телефоном."""
    order = await order_service.get_order(session, order_id)
    if order is None or order.user is None or order.user.tg_id != tg_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return {"order": _order_payload(order), "cancellable": order.status in _CANCELLABLE}


@router.post("/orders/{order_id}/cancel")
async def cancel_order(
    order_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    order = await order_service.get_order(session, order_id)
    tg_id = _int(payload.get("tg_id"))
    if order is None or order.user is None or order.user.tg_id != tg_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    if order.status not in _CANCELLABLE:
        return {"ok": False, "error": "Такой заказ отменяет только поддержка."}

    order_service.set_status(order, OrderStatus.CANCELLED, reason="Отменён клиентом в боте")
    log.info("catalog.order_cancelled", order_id=order.id, number=order.public_number)
    return {"ok": True}
