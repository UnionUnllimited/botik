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

import json
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.deps import get_session, get_transaction
from api.service_auth import require_token
from core import validators
from core.config import settings
from core.enums import DeliveryMethod, OrderStatus, PaymentProviderName, PaymentPurpose, VatCode
from core.models import Order, Product, User
from core.services import delivery as delivery_service
from core.services import media, settings_service
from core.services import orders as order_service
from core.services import payments as payment_service
from core.services import promo as promo_service

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
