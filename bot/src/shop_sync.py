"""Тарифы бота → сроки подписки основного приложения.

Правятся тарифы в одном месте — в разделе «Тарифы» этой админки. Но считает
по ним не она: цена заказа, срок активации роутера и продление живут в основном
приложении, в его таблице сроков. Поэтому список зеркалится туда.

Направление одностороннее и другим быть не может: у тарифов один хозяин,
иначе цены разъедутся в первый же день.

Отправляем после каждой правки и раз в несколько минут вдогонку: правку могли
внести мимо админки, а тариф, не доехавший до основного приложения, значит
заказ по старой цене.
"""

from __future__ import annotations

import asyncio

from loguru import logger

import db_helpers
from src import shop_api

SYNC_INTERVAL_SEC = 300


def _clean(tariffs: list[dict]) -> list[dict]:
    """Оставляет по одному тарифу на каждое предложение.

    В таблице один и тот же срок лежит по разу на способ оплаты — так устроен
    их выбор тарифа. Клиенту роутера способ оплаты не выбирают, и шесть
    одинаковых «30 дней» в списке ему показывать незачем.
    """
    unique: dict[tuple, dict] = {}
    for row in tariffs:
        days = int(row.get("days") or 0)
        if days <= 0:
            continue
        key = ((row.get("name") or "").strip(), days, float(row.get("price") or 0))
        unique.setdefault(
            key,
            {
                "id": row.get("id"),
                "name": row.get("name") or f"{days} дн.",
                "days": days,
                "price": row.get("price") or 0,
                "description": row.get("description") or "",
                "sort_order": row.get("sort_order") or 100,
                "is_active": bool(row.get("is_active", 1)),
            },
        )
    return list(unique.values())


async def sync_once() -> tuple[dict, str]:
    """Одна отправка. Ошибку возвращаем, а не бросаем: сохранение тарифа
    не должно падать из-за того, что соседний сервис молчит."""
    try:
        tariffs = await db_helpers.get_active_tariffs()
    except Exception as exc:  # noqa: BLE001 — читаем чужую базу, причина в лог
        logger.warning(f"[TARIFFS] не прочитались тарифы: {exc}")
        return {}, str(exc)

    data, error = await shop_api.sync_plans(_clean(tariffs))
    if error:
        logger.warning(f"[TARIFFS] не отправились в каталог: {error}")
    else:
        logger.info(
            "[TARIFFS] сроки в каталоге обновлены: "
            f"новых {data.get('created', 0)}, обновлено {data.get('updated', 0)}, "
            f"скрыто {data.get('hidden', 0)}"
        )
    return data, error


async def sync_subscriptions() -> int:
    """Переносит подписки роутеров в нашу таблицу `users`.

    Дашборд, фильтры «активные/истёкшие», рассылки и шапка карточки клиента
    читают одно поле `subscription_end_date` в нашей базе. Подписка роутера
    живёт в основном приложении, и без этого зеркала везде показывается
    «Без подписки» — сколько экранов ни правь.

    Пишем только тем, у кого подписка там есть: свои записи, если продукт
    когда-нибудь снова начнут продавать подпиской для телефона, не трогаем.
    """
    rows, error = await shop_api.subscriptions_snapshot()
    if error:
        logger.debug(f"[SUBS] снимок подписок недоступен: {error}")
        return 0

    updated = 0
    async with db_helpers.get_db_connection_safe() as db:
        for row in rows:
            until = row.get("until")
            if not row.get("tg_id") or not until:
                continue
            cursor = await db.execute(
                "UPDATE users SET subscription_end_date = ? WHERE telegram_id = ?",
                (until, int(row["tg_id"])),
            )
            updated += cursor.rowcount or 0
        await db.commit()

    if updated:
        logger.info(f"[SUBS] подписок перенесено в базу бота: {updated}")
    return updated


async def sync_loop() -> None:
    logger.info("[TARIFFS] синхронизация тарифов и подписок с каталогом запущена")
    while True:
        try:
            await sync_once()
            await sync_subscriptions()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — цикл переживает что угодно
            logger.error(f"[TARIFFS] круг синхронизации упал: {exc}")
        await asyncio.sleep(SYNC_INTERVAL_SEC)


def start_tariff_sync() -> asyncio.Task:
    return asyncio.create_task(sync_loop())
