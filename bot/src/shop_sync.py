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
import json
import re

import aiosqlite
from loguru import logger

import db_helpers
from src import shop_api

SYNC_INTERVAL_SEC = 300

_DEVICE_LIMIT = re.compile(
    # Длинный вариант первым: иначе «уст» съедало начало «устройства»
    # и в названии оставался хвост «ройство».
    r"\s*[|·-]?\s*\d+\s*(?:устройств\w*|устройство|уст\.?(?![а-яё]))\s*",
    re.IGNORECASE,
)
"""Лимит устройств в названии тарифа: «30 дней | 1 уст.».

Он достался от подписки для телефона, где слоты продавали поштучно.
За роутером сидит вся домашняя сеть, и «1 уст.» на кнопке продления клиент
читает как «работать будет одно устройство» — то есть как обман.

Срезаем на переносе, а не правим названия руками: тарифы заводят в их
админке, и следующий появится с тем же хвостом."""


def strip_device_limit(name: str) -> str:
    """Убирает «| 1 уст.» из названия тарифа, оставляя срок."""
    cleaned = _DEVICE_LIMIT.sub(" ", name or "").strip()
    return re.sub(r"\s{2,}", " ", cleaned).strip(" |·-").strip()


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
        name = strip_device_limit(row.get("name") or "") or f"{days} дн."
        key = (name, days, float(row.get("price") or 0))
        unique.setdefault(
            key,
            {
                "id": row.get("id"),
                "name": name,
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


PANEL_COLUMNS = ("shop_panel_username", "shop_panel_short_uuid")
"""Учётка панели у роутерного клиента — в своих колонках, а не в их
`remnawave_short_uuid`. То поле читают клиентские экраны бота: «мой ключ»,
кнопки подключения, и записанная туда роутерная учётка ушла бы клиенту
ссылкой на телефон. Админке же нужен только факт «ключ есть» и сам ключ —
она научена читать эти колонки рядом с их собственными."""


async def _ensure_panel_columns(db) -> None:
    """Колонки добавляются на месте, как и остальные их миграции: базу
    заводит их код, и своей миграции у нас здесь нет."""
    for column in PANEL_COLUMNS:
        try:
            await db.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
        except aiosqlite.OperationalError:
            pass


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
        await _ensure_panel_columns(db)
        for row in rows:
            until = row.get("until")
            if not row.get("tg_id") or not until:
                continue
            cursor = await db.execute(
                "UPDATE users SET subscription_end_date = ?, "
                "shop_panel_username = ?, shop_panel_short_uuid = ? "
                "WHERE telegram_id = ?",
                (
                    until,
                    row.get("panel_username") or "",
                    row.get("panel_short_uuid") or "",
                    int(row["tg_id"]),
                ),
            )
            updated += cursor.rowcount or 0
        await db.commit()

    if updated:
        logger.info(f"[SUBS] подписок перенесено в базу бота: {updated}")
    return updated


PAYMENTS_CURSOR_KEY = "shop_payments_since"
"""Докуда зеркало платежей дочитало. Живёт в нашей таблице настроек, а не
в памяти: после перезапуска бот продолжает с места, а не перечитывает всё."""

_UPSERT_PAYMENT = """
    INSERT INTO payments
        (payment_id, telegram_id, amount, currency, status, created_at, metadata_json, pwa_notified)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(payment_id) DO UPDATE SET
        amount = excluded.amount,
        currency = excluded.currency,
        status = excluded.status,
        metadata_json = excluded.metadata_json
"""
"""`pwa_notified` при обновлении не трогаем: это флаг «оператору уже
сообщили», и смена статуса не должна его сбрасывать."""


async def _remember_cursor(value: str) -> None:
    async with db_helpers.get_db_connection_safe() as db:
        await db.execute(
            "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (PAYMENTS_CURSOR_KEY, value, "Курсор зеркала платежей магазина"),
        )
        await db.commit()


async def _store_payments(rows: list[dict], *, notified: int) -> tuple[int, int]:
    """Кладёт пачку в нашу таблицу. Возвращает (записано, пропущено).

    Пропускаем платежи клиентов, которых в `users` нет: база держит
    внешние ключи включёнными, и такая строка уронила бы всю пачку.
    Клиент без строки в `users` — тот, кто ни разу не открывал бота,
    и карточки у него всё равно нет.
    """
    tg_ids = sorted({int(row["tg_id"]) for row in rows if row.get("tg_id")})
    if not tg_ids:
        return 0, len(rows)

    written = skipped = 0
    async with db_helpers.get_db_connection_safe() as db:
        marks = ",".join("?" * len(tg_ids))
        async with db.execute(
            f"SELECT telegram_id FROM users WHERE telegram_id IN ({marks})", tg_ids
        ) as cursor:
            known = {int(item[0]) for item in await cursor.fetchall()}

        for row in rows:
            tg_id = int(row.get("tg_id") or 0)
            if tg_id not in known:
                skipped += 1
                continue
            await db.execute(
                _UPSERT_PAYMENT,
                (
                    row["payment_id"],
                    tg_id,
                    float(row.get("amount") or 0),
                    row.get("currency") or "RUB",
                    row.get("status") or "pending",
                    row.get("created_at") or "",
                    json.dumps(row.get("metadata") or {}, ensure_ascii=False),
                    notified,
                ),
            )
            written += 1
        await db.commit()
    return written, skipped


async def sync_payments() -> int:
    """Переносит платежи магазина в нашу таблицу `payments`.

    Карточка клиента, страница платежей, аналитика, выручка на главной
    и «оплатил ли приглашённый» читают одну эту таблицу. Оплата роутера,
    доставки и продления проходит через основное приложение и сюда не
    попадала — у клиента с оплаченным заказом в карточке стояло
    «Платежей 0». Чинить это по экрану значит чинить вечно: экранов
    десятки, и каждый новый снова не видел бы этих денег.

    Первый круг переносит историю целиком и помечает её как «оператору
    сообщено»: иначе push-рассылка админке выстрелила бы всеми старыми
    оплатами разом. Дальше — только изменения с прошлого курсора,
    и о свежих оплатах оператор узнаёт как о своих.
    """
    since = await db_helpers.get_setting_by_key(PAYMENTS_CURSOR_KEY, "")
    first_run = not since
    total_written = total_skipped = 0

    while True:
        data, error = await shop_api.payments_snapshot(since)
        if error:
            logger.debug(f"[PAYMENTS] снимок платежей недоступен: {error}")
            break
        rows = data.get("payments") or []
        if not rows:
            break

        written, skipped = await _store_payments(rows, notified=1 if first_run else 0)
        total_written += written
        total_skipped += skipped

        since = data.get("next_since") or since
        await _remember_cursor(since)
        if len(rows) < int(data.get("limit") or len(rows)):
            break

    if total_written or total_skipped:
        logger.info(
            f"[PAYMENTS] платежей магазина перенесено: {total_written}, "
            f"пропущено без клиента в базе: {total_skipped}"
        )
    return total_written


async def sync_loop() -> None:
    logger.info("[TARIFFS] синхронизация тарифов, подписок и платежей с каталогом запущена")
    while True:
        try:
            await sync_once()
            await sync_subscriptions()
            await sync_payments()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — цикл переживает что угодно
            logger.error(f"[TARIFFS] круг синхронизации упал: {exc}")
        await asyncio.sleep(SYNC_INTERVAL_SEC)


def start_tariff_sync() -> asyncio.Task:
    return asyncio.create_task(sync_loop())
