"""Настройки, которые правятся из админки без деплоя.

Значения лежат в таблице `settings` (jsonb), читаются через кэш в Redis
с коротким TTL. Дефолты описаны здесь: пустая база — рабочая система.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings as env
from core.models import Setting
from core.redis_client import get_redis

log = structlog.get_logger("services.settings")

CACHE_TTL_SEC = 60

DEFAULTS: dict[str, Any] = {
    # Цен доставки здесь нет намеренно: их называет оператор в самом заказе,
    # по адресу и весу. Прейскурант по зонам прожил неделю — цену по нему всё
    # равно перебивали руками. Названия перевозчиков — в `core/texts.py`.
    "order.shipping_days": "1–2 рабочих дня",
    # Карты пунктов выдачи у перевозчиков свои, и держать их список у себя
    # нельзя: пункты открываются и закрываются каждую неделю. Клиент выбирает
    # на их карте и присылает адрес — так он всегда актуален.
    "delivery.cdek_pickup_url": "https://www.cdek.ru/ru/offices",
    # Не «страница пунктов» у перевозчика, а поиск по карте: постоянного
    # адреса под список ПВЗ у Яндекса нет — прежний `yandex.ru/pvz` отдаёт 404,
    # и клиент, нажавший «Выбрать пункт выдачи», упирался в него посреди
    # оформления. Карта заодно показывает ближайшие к нему, а не все подряд.
    "delivery.yandex_pickup_url": (
        "https://yandex.ru/maps/?text=%D0%AF%D0%BD%D0%B4%D0%B5%D0%BA%D1%81%20"
        "%D0%94%D0%BE%D1%81%D1%82%D0%B0%D0%B2%D0%BA%D0%B0%20%D0%BF%D1%83%D0%BD"
        "%D0%BA%D1%82%20%D0%B2%D1%8B%D0%B4%D0%B0%D1%87%D0%B8"
    ),
    # Инструкций две, и они про разное. Постоянная лежит на самом роутере
    # (`http://192.168.14.1/instruction`) — её открывают из дома, когда
    # «пропал интернет», и она работает даже без него. «Как подключить»
    # живёт на витрине: её читают, пока посылка едет, роутера в сети ещё
    # нет вовсе, и локальный адрес там никуда не ведёт.
    # Пусто — берутся эти умолчания; оператор может заменить, не трогая код.
    "router.instruction_url": "",
    "router.setup_url": "",
    "support.contact": "",
    "support.working_hours": "ежедневно с 10:00 до 22:00 по Москве",
    "activation.auto_enabled": False,
    "landing.enabled": True,
    # Логотип и значок вкладки — разные картинки: в шапке нужна только буква
    # (мелкие детали в строке сливаются), во вкладке — знак целиком.
    # Пусто — витрина рисует свои из статики.
    "landing.logo_url": "",
    "landing.favicon_url": "",
    # Картинка первого экрана. Пусто — берём фото первой модели: отдельную
    # витринную картинку заводят не всегда, а пустое место на первом экране
    # хуже, чем фото товара.
    "landing.hero_image_url": "",
    "landing.hero_title": "Роутер, с которым интернет работает как раньше",
    "landing.hero_subtitle": (
        "Готовое устройство с подпиской на сервис стабильного доступа "
        "к зарубежным ресурсам. Включаете в розетку — работает вся домашняя "
        "сеть: телевизор, приставка, ноутбук, телефоны гостей."
    ),
}

DESCRIPTIONS = {
    "order.shipping_days": "Срок отправки заказа после оплаты",
    "delivery.cdek_pickup_url": "Карта пунктов выдачи СДЭК — кнопка при оформлении",
    "delivery.yandex_pickup_url": "Карта пунктов выдачи Яндекс Go — кнопка при оформлении",
    "router.instruction_url": "Постоянная инструкция на самом роутере: кнопка в «Моём роутере»",
    "router.setup_url": "Как подключить роутер, страница витрины: кнопка в заказе, пока посылка едет",
    "support.contact": "Контакт поддержки для текстов бота",
    "support.working_hours": "Часы работы поддержки",
    "activation.auto_enabled": "Активировать роутер сам, когда отгруженный заказ выходит на связь",
    "landing.enabled": "Показывать витрину в корне домена",
    "landing.logo_url": "Логотип в шапке витрины. Пусто — свой знак",
    "landing.favicon_url": "Значок вкладки браузера. Пусто — свой знак",
    "landing.hero_image_url": "Картинка первого экрана витрины. Пусто — фото первой модели",
    "landing.hero_title": "Заголовок первого экрана витрины",
    "landing.hero_subtitle": "Подзаголовок первого экрана витрины",
}


def _cache_key(key: str) -> str:
    return env.redis.key("settings", key)


async def get_setting(session: AsyncSession, key: str) -> Any:
    """Значение настройки: Redis → БД → дефолт из кода."""
    redis = get_redis()
    try:
        cached = await redis.get(_cache_key(key))
        if cached is not None:
            return json.loads(cached)
    except Exception as exc:  # noqa: BLE001 — кэш не критичен, читаем из БД
        log.warning("settings.cache_read_failed", key=key, error=str(exc))

    row = await session.scalar(select(Setting).where(Setting.key == key))
    value = row.value.get("value") if row is not None else DEFAULTS.get(key)

    try:
        await redis.set(_cache_key(key), json.dumps(value), ex=CACHE_TTL_SEC)
    except Exception as exc:  # noqa: BLE001
        log.warning("settings.cache_write_failed", key=key, error=str(exc))
    return value


async def set_setting(
    session: AsyncSession,
    key: str,
    value: Any,
    *,
    admin_id: int | None = None,
) -> None:
    statement = (
        insert(Setting)
        .values(
            key=key,
            value={"value": value},
            description=DESCRIPTIONS.get(key, ""),
            updated_by_admin_id=admin_id,
        )
        .on_conflict_do_update(
            index_elements=[Setting.key],
            set_={"value": {"value": value}, "updated_by_admin_id": admin_id},
        )
    )
    await session.execute(statement)
    try:
        await get_redis().delete(_cache_key(key))
    except Exception as exc:  # noqa: BLE001
        log.warning("settings.cache_invalidate_failed", key=key, error=str(exc))
    log.info("settings.updated", key=key, admin_id=admin_id)


async def get_decimal(session: AsyncSession, key: str) -> Decimal:
    value = await get_setting(session, key)
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 — битое значение не должно ронять оформление заказа
        log.warning("settings.bad_decimal", key=key, value=value)
        return Decimal(str(DEFAULTS.get(key, "0.00")))


async def get_bool(session: AsyncSession, key: str) -> bool:
    return bool(await get_setting(session, key))


async def get_str(session: AsyncSession, key: str) -> str:
    value = await get_setting(session, key)
    return "" if value is None else str(value)


async def ensure_defaults(session: AsyncSession) -> int:
    """Записывает отсутствующие ключи в БД — чтобы админ видел их в интерфейсе."""
    existing = set((await session.scalars(select(Setting.key))).all())
    created = 0
    for key, value in DEFAULTS.items():
        if key in existing:
            continue
        session.add(Setting(key=key, value={"value": value}, description=DESCRIPTIONS.get(key, "")))
        created += 1
    return created
