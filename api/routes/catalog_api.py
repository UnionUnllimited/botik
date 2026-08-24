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
import csv
import datetime as dt
import io
import json
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.deps import get_session, get_transaction
from api.service_auth import require_token
from core import texts, validators
from core.config import settings
from core.dates import to_display, utcnow
from core.enums import (
    OFFERED_DELIVERY_METHODS,
    DeliveryMethod,
    DeviceStatus,
    OrderItemType,
    OrderStatus,
    PaymentProviderName,
    PaymentPurpose,
    PromoDiscountType,
    VatCode,
)
from core.models import (
    Device,
    Notification,
    Order,
    Payment,
    Plan,
    Product,
    PromoCode,
    Subscription,
    User,
)
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


TARIFF_SLUG_PREFIX = "tariff-"
"""Признак срока, приехавшего из тарифов бота. Свои когда-то заводились руками
и различались бы только на глаз — префикс делает происхождение явным."""


@router.post("/plans/sync")
async def sync_plans(payload: dict, session: AsyncSession = Depends(get_transaction)) -> dict:
    """Зеркалит тарифы бота в наши сроки подписки.

    Единственное место правки — раздел «Тарифы» в админке бота. Наша цепочка
    считает по `plans` (цена заказа, срок активации, продление), поэтому тарифы
    приезжают сюда как есть и обновляются целиком.

    Всё, чего в присланном списке нет, выключается — включая сроки, заведённые
    здесь раньше руками: два источника цен на одно и то же расходятся в первый
    же день. Не удаляем: на них ссылаются оплаченные заказы и подписки.
    """
    incoming = payload.get("tariffs")
    if not isinstance(incoming, list):
        return {"ok": False, "error": "Ожидается список тарифов."}

    seen: set[str] = set()
    created = updated = 0
    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        tariff_id = _int(raw.get("id"))
        days = _int(raw.get("days"))
        if not tariff_id or days <= 0:
            continue

        slug = f"{TARIFF_SLUG_PREFIX}{tariff_id}"
        seen.add(slug)
        plan = await session.scalar(select(Plan).where(Plan.slug == slug))
        if plan is None:
            plan = Plan(slug=slug, title="", months=0, price=Decimal("0"))
            session.add(plan)
            created += 1
        else:
            updated += 1

        plan.title = str(raw.get("name", "")).strip()[:200] or f"{days} дн."
        plan.description = str(raw.get("description") or "").strip()
        # Их тариф меряется днями, и переводить их в месяцы нельзя: «30 дней»
        # и «месяц» — разные сроки в феврале.
        plan.months = 0
        plan.extra_days = days
        plan.price = _decimal(raw.get("price"), "0")
        plan.is_active = bool(raw.get("is_active", True))
        plan.sort_order = _int(raw.get("sort_order"), 100)

    stale = await session.scalars(
        select(Plan).where(Plan.is_active.is_(True), Plan.slug.notin_(seen or {""}))
    )
    hidden = 0
    for plan in stale:
        plan.is_active = False
        hidden += 1

    log.info("catalog.plans_synced", created=created, updated=updated, hidden=hidden)
    return {"ok": True, "created": created, "updated": updated, "hidden": hidden}


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
    """Варианты доставки для экрана оформления — скорость, без цен.

    Цену называет оператор после того, как заказ оформлен: она зависит от
    города и габаритов, и обещать её заранее нечестно. До этого была попытка
    считать по тарифным зонам — цену всё равно перебивали руками, а город,
    которого в зонах не оказалось, останавливал оформление у живого клиента.
    """
    return {
        "ok": True,
        "options": [
            {
                "speed": str(option.speed),
                "title": option.title,
                "description": option.description,
            }
            for option in await delivery_service.speed_options(session)
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


@router.get("/my-router/available")
async def my_router_available(tg_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Есть ли клиенту что показывать на экране «Мой роутер».

    Спрашивается при отрисовке главного меню, поэтому здесь только два
    индексных запроса и ни одного обращения к панели: полный `/my-router`
    ждёт от неё срок подписки до трёх секунд, и в меню это недопустимо.

    Правило то же, что на самом экране: показывать, когда есть роутер или
    заказ. Без того и другого кнопка вела к «роутера у вас нет» — клиент,
    зашедший в бота впервые, получал экран про покупку, которой не было.
    """
    user = await session.scalar(select(User).where(User.tg_id == tg_id))
    if user is None:
        return {"show": False}
    has_device = await session.scalar(
        select(Device.id).where(Device.user_id == user.id).limit(1)
    )
    if has_device is not None:
        return {"show": True}
    has_order = await session.scalar(
        select(Order.id).where(Order.user_id == user.id).limit(1)
    )
    return {"show": has_order is not None}


@router.get("/my-router")
async def my_router(
    tg_id: int, device_id: int = 0, session: AsyncSession = Depends(get_session)
) -> dict:
    """Экран «Мой роутер»: что с устройством и подпиской.

    Роутер привязывается к клиенту оператором при отгрузке — по MAC, который
    уходит в посылке. Поэтому здесь же показывается и состояние заказа: пока
    роутер в пути, это единственное, что клиенту интересно.

    Роутеров у клиента может быть несколько: купил второй на дачу, поменял
    по гарантии. Раньше отдавался только последний по номеру, и первый просто
    исчезал с экрана вместе со своей подпиской. Теперь отдаётся список, а
    `device_id` выбирает, чьи показания разворачивать.

    Показания разворачиваются для одного: срок подписки знает панель, и спросить
    её по каждому роутеру значит сложить их ожидания в одном экране.
    """
    user = await session.scalar(select(User).where(User.tg_id == tg_id))
    if user is None:
        return {"has_client": False, "routers": [], "router": None, "order": None}

    devices = list(
        await session.scalars(
            select(Device).where(Device.user_id == user.id).order_by(Device.id.desc())
        )
    )
    order = await session.scalar(
        select(Order)
        .where(Order.user_id == user.id)
        .order_by(Order.id.desc())
        .options(selectinload(Order.delivery), selectinload(Order.items))
    )

    now = utcnow()
    current = None
    if devices:
        current = next((d for d in devices if d.id == device_id), devices[0])

    router_payload = None
    if current is not None:
        # Срок знает панель: подписку роутеру выдаёт она, а не мы.
        panel_until = None
        if settings.remnawave.is_configured:
            try:
                account = await asyncio.wait_for(activation.panel_account_of(current), timeout=3)
                panel_until = activation.panel_expiry_of(account)
            except TimeoutError:
                log.warning("catalog.panel_timeout", device_id=current.id)

        router_payload = {
            "id": current.id,
            "mac": current.mac,
            "model": current.model or "",
            "status": str(current.status),
            "online": current.frp_online
            or current.is_online(threshold_min=settings.subscription.heartbeat_offline_min, now=now),
            "activated": current.activated_at is not None,
            "clients": (current.clients_wifi or 0) + (current.clients_dhcp or 0),
            "uptime_sec": current.uptime_sec or 0,
            "rx_bytes": current.rx_bytes or 0,
            "tx_bytes": current.tx_bytes or 0,
            "until": panel_until.isoformat() if panel_until else None,
            "active": bool(panel_until and panel_until > now),
        }

    return {
        "has_client": True,
        # Инструкция лежит на самом роутере и открывается из домашней сети:
        # так она не расходится с прошивкой и доступна ещё до того, как
        # появится интернет, — а нужна она ровно в этот момент.
        "instruction_url": str(
            await settings_service.get_setting(session, "router.instruction_url")
            or texts.DEFAULT_INSTRUCTION_URL
        ),
        # Список — без обращения к панели: он нужен, чтобы нарисовать кнопки
        # выбора, а срок разворачивается только у выбранного.
        "routers": [
            {
                "id": device.id,
                "mac": device.mac,
                "model": device.model or "",
                "online": device.frp_online
                or device.is_online(
                    threshold_min=settings.subscription.heartbeat_offline_min, now=now
                ),
                "activated": device.activated_at is not None,
            }
            for device in devices
        ],
        "router": router_payload,
        "order": _order_payload(order) if order is not None else None,
    }


@router.get("/subscriptions")
async def subscriptions_snapshot(session: AsyncSession = Depends(get_session)) -> dict:
    """Все подписки клиентов с Telegram — для зеркала в базе бота.

    Его дашборд, фильтры, рассылки и шапка карточки клиента читают одно поле
    `users.subscription_end_date` в его собственной базе. Подписка роутера живёт
    у нас, и без зеркала там честное «Без подписки» — сколько экранов ни правь.
    Поэтому отдаём снимок целиком, а бот раскладывает его по своим строкам.
    """
    rows = await session.execute(
        select(User.tg_id, Subscription.status, Subscription.expires_at)
        .join(Subscription, Subscription.user_id == User.id)
        .where(User.tg_id.is_not(None))
        .order_by(Subscription.id)
    )
    latest: dict[int, dict] = {}
    for tg_id, state, expires_at in rows:
        # Подписок у человека может быть несколько за историю; в базе бота
        # поле одно, и туда должна попасть последняя по сроку.
        current = latest.get(tg_id)
        if current and current["until"] and expires_at and current["until"] >= expires_at.isoformat():
            continue
        latest[tg_id] = {
            "tg_id": tg_id,
            "status": str(state),
            "until": _iso_dt(expires_at),
        }
    return {"total": len(latest), "subscriptions": list(latest.values())}


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
    # Скорость выбирает клиент, перевозчика ставит оператор при отгрузке.
    # Незнакомое значение — не отказ: без скорости заказ просто оформится
    # без доставки, а это тише, чем уронить оформление на опечатке.
    speed = delivery_service.parse_speed(str(payload.get("delivery_speed", "")))

    to_pvz = bool(payload.get("delivery_to_pvz", True))
    return order_service.OrderDraft(
        product_id=_int(payload.get("product_id")) or None,
        plan_id=_int(payload.get("plan_id")) or None,
        customer_name=validators.clean_full_name(str(payload.get("name", ""))),
        customer_phone=validators.clean_phone(str(payload.get("phone", ""))),
        customer_city=validators.clean_city(str(payload.get("city", ""))),
        delivery_speed=speed,
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
        "delivery_price": str(order.delivery.price) if order.delivery else "0.00",
        "awaiting_quote": delivery_service.awaiting_quote(order.delivery),
        "delivery_paid": bool(order.delivery and order.delivery.paid_at),
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

PAYMENT_LABELS = {
    "pending": "ждёт оплаты",
    "waiting_for_capture": "деньги удержаны",
    "succeeded": "оплачен",
    "canceled": "отменён",
    "failed": "не прошёл",
    "refunded": "возвращён",
    "expired": "просрочен",
}
"""Состояния платежа словами. Оператор читает эту таблицу при разборе
«я оплатил, а заказ висит» — `succeeded` ему в этом не помощник."""


def _manage_order_row(order: Order) -> dict:
    return {
        "id": order.id,
        "number": order.public_number,
        "status": str(order.status),
        "total": str(order.total),
        "customer": order.customer_name or (order.user.display_name if order.user else ""),
        # Телеграм покупателя: имя из доставки тёзок не различает, а связаться
        # с человеком по заказу оператор может только там. Без него из карточки
        # заказа нельзя было ни написать, ни открыть клиента.
        "customer_telegram": order.user.telegram_name if order.user else "",
        "customer_tg_id": (order.user.tg_id or 0) if order.user else 0,
        "phone": order.customer_phone or "",
        "city": order.customer_city or "",
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "paid": order.paid_at is not None,
        "tracking_number": (order.delivery.tracking_number or "") if order.delivery else "",
        # Заказ ждёт, пока оператор назовёт цену доставки. Отдельно от статуса:
        # заказ при этом «Оплачен», и мешать одно с другим нельзя.
        "awaiting_quote": delivery_service.awaiting_quote(order.delivery),
        "delivery_paid": bool(order.delivery and order.delivery.paid_at),
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


ORDERS_CSV_COLUMNS = (
    "Номер",
    "Создан",
    "Статус",
    "Телеграм",
    "Клиент",
    "Телефон",
    "Город",
    "Доставка",
    "Адрес",
    "Трек-номер",
    "Состав",
    "Товары, ₽",
    "Скидка, ₽",
    "Доставка, ₽",
    "Итого, ₽",
    "Оплачен",
    "Отправлен",
    "Доставлен",
    "Комментарий клиента",
    "Заметка оператора",
)


def _csv_moment(value: dt.datetime | None) -> str:
    """Дата для таблицы — в том же часовом поясе, что и на экранах админки."""
    return f"{to_display(value):%d.%m.%Y %H:%M}" if value else ""


def _order_csv_row(order: Order, carriers: dict[DeliveryMethod, str]) -> list[str]:
    delivery = order.delivery
    goods = [item for item in (order.items or []) if item.item_type is not OrderItemType.DELIVERY]
    return [
        order.public_number,
        _csv_moment(order.created_at),
        texts.ORDER_STATUS_TITLES.get(order.status, str(order.status)),
        # Телеграм отдельной колонкой: по нему с покупателем и связываются,
        # а имя из доставки в таблице тёзок не различает.
        order.user.telegram_name if order.user else "",
        order.customer_name or (order.user.display_name if order.user else ""),
        order.customer_phone or "",
        order.customer_city or "",
        # Только перевозчик: адрес идёт своей колонкой, и повторять его здесь
        # значит мешать сортировку по способу доставки.
        carriers.get(delivery.method, str(delivery.method)) if delivery else "",
        (delivery.pvz_address or delivery.address or "") if delivery else "",
        (delivery.tracking_number or "") if delivery else "",
        "; ".join(f"{item.title} × {item.quantity}" for item in goods),
        str(order.subtotal),
        str(order.discount_total),
        str(order.delivery_price),
        str(order.total),
        _csv_moment(order.paid_at),
        _csv_moment(order.shipped_at),
        _csv_moment(order.delivered_at),
        order.comment or "",
        order.admin_note or "",
    ]


@router.get("/manage/orders/export")
async def manage_orders_export(
    status_filter: str = Query(default="", alias="status"),
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Выгрузка заказов в CSV — под теми же фильтрами, что и список.

    Отдаём файлом, а не строкой в JSON: выгрузку открывают в таблице, и лишний
    слой кодирования по дороге только ломает переносы строк в адресах.

    Точка с запятой и BOM — ради Excel: с запятой он свалит строку в одну
    ячейку, без BOM покажет кириллицу кракозябрами. Это выгрузка для человека
    с таблицей, а не для другой программы.
    """
    query = (
        select(Order)
        .options(
            selectinload(Order.user), selectinload(Order.delivery), selectinload(Order.items)
        )
        .order_by(Order.id.desc())
    )
    if status_filter:
        query = query.where(Order.status == status_filter)
    text = q.strip()
    if text:
        pattern = f"%{text}%"
        query = query.where(
            or_(
                Order.public_number.ilike(pattern),
                Order.customer_name.ilike(pattern),
                Order.customer_phone.ilike(pattern),
                Order.customer_city.ilike(pattern),
            )
        )

    # Названия перевозчиков берём те, что оператор задал в настройках, а не
    # коды из перечисления: в таблице он ищет «СДЭК», а не «cdek».
    carriers = {
        option.method: option.title
        for option in await delivery_service.get_options(session, only_enabled=False)
    }

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(ORDERS_CSV_COLUMNS)
    count = 0
    for order in await session.scalars(query):
        writer.writerow(_order_csv_row(order, carriers))
        count += 1

    log.info("catalog.orders_exported", count=count, status=status_filter or "все")
    stamp = to_display(utcnow()).strftime("%Y-%m-%d")
    return Response(
        content="﻿" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="orders-{stamp}.csv"'},
    )


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
            "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
            # Доставка идёт отдельной строкой ниже; в составе она была вторым
            # разом и читалась как двойной счёт.
            "items": [
                {"title": item.title, "total": str(item.total_price)}
                for item in (order.items or [])
                if item.item_type is not OrderItemType.DELIVERY
            ],
        },
        "delivery": {
            "method": str(delivery.method) if delivery else "",
            "speed": str(delivery.speed) if delivery else "",
            "speed_title": order_service.SPEED_SUMMARY.get(delivery.speed, "") if delivery else "",
            "price": str(delivery.price) if delivery else "0.00",
            "awaiting_quote": delivery_service.awaiting_quote(delivery),
            "paid": bool(delivery and delivery.paid_at),
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
                "status_label": PAYMENT_LABELS.get(str(payment.status), str(payment.status)),
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


def _status_notice(order: Order, reason: str, *, instruction_url: str = "") -> str:
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
    if order.status is OrderStatus.DELIVERED:
        # Момент, когда человек держит коробку и не знает, что дальше.
        # Инструкция лежит на самом роутере: она не может разойтись
        # с прошивкой и открывается ещё до того, как появится интернет.
        notice += "\n\n" + texts.DELIVERY_INSTRUCTION.format(
            instruction=instruction_url or texts.DEFAULT_INSTRUCTION_URL
        )
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
    instruction_url = await settings_service.get_setting(session, "router.instruction_url")
    return {
        "ok": True,
        "status": str(order.status),
        "tg_id": order.user.tg_id if order.user else None,
        "notice": _status_notice(order, reason, instruction_url=str(instruction_url or "")),
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


@router.post("/manage/orders/{order_id}/delivery-quote")
async def manage_delivery_quote(
    order_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Оператор называет цену доставки — клиенту уходит счёт на неё.

    Второй платёж по тому же заказу, а не пересчёт первого: роутер и подписку
    клиент уже оплатил, и трогать оплаченную сумму нельзя — сверка и возвраты
    считают по платежам, а не по тому, что сейчас написано в заказе.

    Ноль — законная цена: доставку можно и подарить. Отличает «бесплатно»
    от «ещё не считали» отметка `quoted_at`, а не сама сумма.
    """
    order = await _order_or_404(session, order_id)
    if order.delivery is None:
        return {"ok": False, "error": "У заказа нет доставки — цену назначать нечему."}

    price = _decimal(payload.get("price"), "0")
    if price < 0:
        return {"ok": False, "error": "Цена доставки не может быть отрицательной."}

    delivery_service.set_quote(order.delivery, price)
    days = str(payload.get("days", "")).strip()[:40]

    pay_url = ""
    if price > 0 and order.user is not None:
        try:
            payment = await payment_service.start_payment(
                session,
                user=order.user,
                provider_name=PaymentProviderName.PLATEGA,
                amount=price,
                purpose=PaymentPurpose.DELIVERY,
                description=f"Доставка по заказу {order.public_number}",
                order=order,
            )
            pay_url = payment.confirmation_url or ""
        except Exception as exc:  # noqa: BLE001 — причина уже написана для человека
            log.warning("catalog.delivery_payment_failed", order_id=order.id, error=str(exc))
            return {"ok": False, "error": f"Счёт выставить не вышло: {exc}"}
    elif price == 0:
        # Дарёную доставку платить не за что — отмечаем оплаченной сразу,
        # иначе заказ навсегда останется «ждёт оплаты доставки».
        order.delivery.paid_at = utcnow()

    notice = texts.DELIVERY_QUOTE.format(
        number=order.public_number,
        price=f"{price:.2f}".rstrip("0").rstrip("."),
        days=days or "уточним при отправке",
    ) if price > 0 else texts.DELIVERY_FREE.format(number=order.public_number)

    log.info(
        "catalog.delivery_quoted", order_id=order.id, price=str(price), has_link=bool(pay_url)
    )
    return {
        "ok": True,
        "price": str(price),
        "pay_url": pay_url,
        "tg_id": order.user.tg_id if order.user else None,
        "notice": notice,
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


@router.post("/orders/{order_id}/delivery-payment")
async def delivery_payment_link(
    order_id: int, payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Свежая ссылка на оплату доставки — по кнопке клиента.

    Ссылка PLATEGA живёт пятнадцать минут, поэтому выданная вместе со счётом
    к вечеру уже мертва. Класть её в напоминание бессмысленно: клиент нажмёт
    и упрётся в «срок истёк». Живую выдаём тогда, когда он сам готов платить.

    `tg_id` обязателен: по одному номеру заказа нельзя выставлять счёт
    чужому человеку.
    """
    order = await order_service.get_order(session, order_id)
    tg_id = _int(payload.get("tg_id"))
    if order is None or order.user is None or order.user.tg_id != tg_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")

    delivery = order.delivery
    if delivery is None or delivery.quoted_at is None:
        return {"ok": False, "error": "Стоимость доставки ещё не посчитана."}
    if delivery.paid_at is not None:
        return {"ok": False, "error": "Доставка по этому заказу уже оплачена."}
    if delivery.price <= 0:
        return {"ok": False, "error": "Доставка по этому заказу бесплатная."}

    try:
        payment = await payment_service.start_payment(
            session,
            user=order.user,
            provider_name=PaymentProviderName.PLATEGA,
            amount=delivery.price,
            purpose=PaymentPurpose.DELIVERY,
            description=f"Доставка по заказу {order.public_number}",
            order=order,
        )
    except Exception as exc:  # noqa: BLE001 — причина уже написана для человека
        log.warning("catalog.delivery_link_failed", order_id=order.id, error=str(exc))
        return {"ok": False, "error": f"Оплата сейчас недоступна: {exc}"}

    return {"ok": True, "pay_url": payment.confirmation_url or "", "price": str(delivery.price)}


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


# ── Промокоды каталога ────────────────────────────────────────────────────────
#
# Скидки на железо — наши: цену заказа считаем мы, и промокод участвует в этом
# расчёте. У бота свои промокоды, на подписку, и это разные вещи: один даёт
# скидку на роутер в посылке, другой — дни к сроку.
#
# Раздел удалён вместе с нашей админкой, и с тех пор промокод в боте применялся,
# а завести его можно было только запросом в базу.


def _promo_row(promo: PromoCode) -> dict:
    return {
        "id": promo.id,
        "code": promo.code,
        "description": promo.description,
        "discount_type": str(promo.discount_type),
        "value": str(promo.value),
        "max_uses": promo.max_uses,
        "used_count": promo.used_count,
        "per_user_limit": promo.per_user_limit,
        "min_amount": str(promo.min_amount),
        "valid_until": promo.valid_until.isoformat() if promo.valid_until else None,
        "new_clients_only": promo.new_clients_only,
        "is_active": promo.is_active,
    }


@router.get("/manage/promos")
async def manage_promos(session: AsyncSession = Depends(get_session)) -> dict:
    """Промокоды каталога: список для страницы в админке."""
    promos = await session.scalars(select(PromoCode).order_by(PromoCode.id.desc()))
    return {"promos": [_promo_row(promo) for promo in promos]}


@router.post("/manage/promos")
async def manage_promo_create(
    payload: dict, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Заводит промокод. Код нормализуется — сравнение идёт по верхнему регистру."""
    code = str(payload.get("code") or "").strip().upper()
    if not code or len(code) > 32:
        return {"ok": False, "error": "Код нужен, не длиннее 32 символов."}
    if not code.replace("-", "").replace("_", "").isalnum():
        return {"ok": False, "error": "В коде только латиница, цифры, дефис и подчёркивание."}
    if await session.scalar(select(PromoCode.id).where(PromoCode.code == code)):
        return {"ok": False, "error": "Такой код уже есть."}

    discount_type = str(payload.get("discount_type") or "percent")
    if discount_type not in (PromoDiscountType.PERCENT, PromoDiscountType.FIXED):
        return {"ok": False, "error": "Тип скидки — процент или рубли."}
    try:
        value = Decimal(str(payload.get("value") or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return {"ok": False, "error": "Размер скидки — число."}
    if value <= 0:
        return {"ok": False, "error": "Скидка должна быть больше нуля."}
    if discount_type == PromoDiscountType.PERCENT and value > 100:
        return {"ok": False, "error": "Процент не может быть больше 100."}

    def _int(key: str, default: int) -> int:
        try:
            return max(0, int(payload.get(key, default)))
        except (TypeError, ValueError):
            return default

    valid_until = None
    raw_until = str(payload.get("valid_until") or "").strip()
    if raw_until:
        try:
            valid_until = dt.datetime.fromisoformat(raw_until).replace(tzinfo=dt.UTC)
        except ValueError:
            return {"ok": False, "error": "Дата окончания — в виде ГГГГ-ММ-ДД."}

    session.add(
        PromoCode(
            code=code,
            description=str(payload.get("description") or "")[:255],
            discount_type=PromoDiscountType(discount_type),
            value=value,
            max_uses=_int("max_uses", 0),
            per_user_limit=_int("per_user_limit", 1),
            min_amount=Decimal(str(payload.get("min_amount") or "0")).quantize(Decimal("0.01")),
            valid_until=valid_until,
            new_clients_only=bool(payload.get("new_clients_only")),
        )
    )
    log.info("catalog.promo_created", code=code)
    return {"ok": True}


@router.post("/manage/promos/{promo_id}/toggle")
async def manage_promo_toggle(
    promo_id: int, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Включает и выключает код. Удаление отдельно: у кода есть история
    применений, и снять его с продажи чаще нужно временно."""
    promo = await session.get(PromoCode, promo_id)
    if promo is None:
        raise HTTPException(status_code=404, detail="Промокод не найден")
    promo.is_active = not promo.is_active
    return {"ok": True, "is_active": promo.is_active}


@router.post("/manage/promos/{promo_id}/delete")
async def manage_promo_delete(
    promo_id: int, session: AsyncSession = Depends(get_transaction)
) -> dict:
    """Удаляет код вместе с историей применений.

    Использованный код удалять не даём: на него ссылаются заказы, и «почему
    здесь скидка» после удаления не восстановить.
    """
    promo = await session.get(PromoCode, promo_id)
    if promo is None:
        raise HTTPException(status_code=404, detail="Промокод не найден")
    if promo.used_count:
        return {"ok": False, "error": "Код уже применяли — его можно только выключить."}
    await session.delete(promo)
    return {"ok": True}
