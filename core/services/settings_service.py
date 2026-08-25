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
    "delivery.yandex_pickup_url": "https://yandex.ru/pvz",
    # Инструкций две, и они про разное: постоянная (у клиента всегда)
    # и «как подключить» (нужна один раз, пока посылка едет). Пусто — витрина
    # отдаст свои страницы; оператор может заменить адреса, не трогая код.
    "router.instruction_url": "",
    "router.setup_url": "",
    "support.contact": "",
    "support.working_hours": "ежедневно с 10:00 до 22:00 по Москве",
    "activation.auto_enabled": False,
    "landing.enabled": True,
    # Пусто — витрина рисует свой знак из статики. Оператор может указать
    # адрес своего файла (например, загруженного в «Каталог → Настройки»),
    # и он же станет значком вкладки.
    "landing.logo_url": "",
    "landing.hero_title": "Роутер, за которым интернет работает как раньше",
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
    "router.instruction_url": "Постоянная инструкция: кнопка в «Моём роутере»",
    "router.setup_url": "Как подключить роутер: кнопка в заказе, пока посылка едет",
    "support.contact": "Контакт поддержки для текстов бота",
    "support.working_hours": "Часы работы поддержки",
    "activation.auto_enabled": "Активировать роутер сам, когда отгруженный заказ выходит на связь",
    "landing.enabled": "Показывать витрину в корне домена",
    "landing.logo_url": "Логотип витрины и значок вкладки. Пусто — свой знак",
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
