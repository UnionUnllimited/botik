"""Подхватить из панели сроки роутеров, у которых нашей подписки нет.

    python -m scripts.adopt_panel_subscriptions           # только показать
    python -m scripts.adopt_panel_subscriptions --apply   # записать

До 27 августа 2026 ручная активация заводила одну учётку в панели, а в наших
таблицах не появлялось ничего. Роутеры, включённые так, работают до сих пор,
но для нас подписки у них нет: парк пишет «нет», их нет в отборе «истекает»
и в утренней сводке, клиенту не уходят напоминания о конце срока, а в боте
не видна дата. Доступ при этом отключит панель — в свою дату, молча.

Скрипт заводит каждому такому роутеру подписку на тот срок, что стоит
в панели, — тем же путём, что и ручная активация сегодня, только с пометкой
«подхвачено из панели». Трогает только роутеры с клиентом и без своей
подписки. Срок в панели не меняет.

Без `--apply` ничего не пишет: сначала посмотреть список, потом записывать.
Повторный запуск безопасен — у подхваченных подписка уже есть, и второй раз
их не тронет.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from core.dates import to_display, utcnow
from core.db import dispose_engine, session_scope
from core.logging import configure_logging
from core.models import Device, Subscription
from core.services import activation, remnawave
from core.services import subscriptions as subscription_service

ADOPT = "adopt"
EXPIRED = "expired"
NO_ACCOUNT = "no_account"
NO_DATE = "no_date"

COMMENT = (
    "Срок подхвачен из панели: учётку заводили до того, как активация "
    "стала записывать подписку."
)


@dataclass(slots=True)
class Finding:
    device: Device
    verdict: str
    until: dt.datetime | None
    others: str
    existing: Subscription | None = None
    """Своя запись роутера, истёкшая или отменённая. Есть — продлеваем её,
    а не заводим вторую рядом: одна подписка — один роутер."""
    """Остальные подписки клиента — чтобы увидеть, не ждёт ли где-то
    оплаченная, которую этот роутер должен был получить."""


def classify(until: dt.datetime | None, *, has_account: bool, now: dt.datetime) -> str:
    """Что делать с роутером без своей подписки, по сроку в панели.

    Истёкшую не подхватываем: подписка заведётся сразу истёкшей, и клиенту
    уйдёт «ваша подписка закончилась» о доступе, который кончился давно и,
    возможно, по договорённости. Это решение человека, а не скрипта.
    """
    if not has_account:
        return NO_ACCOUNT
    if until is None:
        return NO_DATE
    if until <= now:
        return EXPIRED
    return ADOPT


def _date(value: dt.datetime | None) -> str:
    return to_display(value).strftime("%d.%m.%Y") if value else "—"


async def _others(session, device: Device) -> str:
    rows = await session.scalars(
        select(Subscription).where(Subscription.user_id == device.user_id).order_by(Subscription.id)
    )
    parts = []
    for row in rows:
        if row.device_id == device.id:
            where = "этот"
        else:
            where = f"роутер {row.device_id}" if row.device_id else "без роутера"
        parts.append(f"#{row.id} {row.status} ({where}, до {_date(row.expires_at)})")
    return "; ".join(parts) or "—"


async def collect(session, now: dt.datetime) -> list[Finding]:
    findings: list[Finding] = []
    # Учётки — одним запросом на весь парк, а не по запросу на роутер;
    # выбирает их то же правило, что активация и сверка сроков в парке.
    accounts = await remnawave.client().users()
    devices = await session.scalars(
        select(Device)
        .where(Device.user_id.is_not(None))
        .options(selectinload(Device.user))
        .order_by(Device.id)
    )
    for device in devices:
        if await subscription_service.get_for_device(session, device.id) is not None:
            continue
        mine = activation.pick_router_accounts(accounts, device.mac)
        account = mine[0] if mine else None
        until = activation.panel_expiry_of(account)
        verdict = classify(until, has_account=account is not None, now=now)
        existing = await subscription_service.device_subscription(session, device.id)
        findings.append(Finding(device, verdict, until, await _others(session, device), existing))
    return findings


TITLES = {
    ADOPT: "подхватим",
    EXPIRED: "в панели истекла — не трогаем",
    NO_ACCOUNT: "учётки в панели нет",
    NO_DATE: "в панели без срока",
}


async def run(apply: bool) -> int:
    now = utcnow()
    adopted = 0
    async with session_scope() as session:
        try:
            findings = await collect(session, now)
        except remnawave.RemnawaveError as exc:
            print(f"Панель не ответила, ничего не тронуто: {exc}")
            return 0
        if not findings:
            print("Роутеров с клиентом и без своей подписки нет. Делать нечего.")
            return 0

        for item in findings:
            client = item.device.user.telegram_name if item.device.user else "—"
            title = TITLES[item.verdict]
            if item.verdict == ADOPT:
                title = (
                    f"продлим #{item.existing.id} (было до {_date(item.existing.expires_at)})"
                    if item.existing is not None
                    else "заведём подписку"
                )
            print(f"{item.device.mac}  {client:<22}  {title:<38}  панель до {_date(item.until)}")
            print(f"    подписки клиента: {item.others}")

            if apply and item.verdict == ADOPT:
                await subscription_service.grant_manual(
                    session,
                    user_id=item.device.user_id,
                    device_id=item.device.id,
                    days=max((item.until - now).days, 1),
                    until=item.until,
                    # Отсчёт — с того дня, когда роутер включили, а не с сегодня:
                    # подписка начиналась тогда, и история должна это показывать.
                    now=item.device.activated_at or now,
                    source="panel",
                    comment=COMMENT,
                )
                adopted += 1

        ready = sum(1 for item in findings if item.verdict == ADOPT)
        if apply:
            print(f"\nЗаписано: {adopted}.")
        else:
            print(f"\nПодхватить можно: {ready}. Ничего не записано — для записи добавьте --apply.")
    return adopted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="записать, а не только показать")
    args = parser.parse_args()
    configure_logging("adopt_panel")

    async def _main() -> None:
        try:
            await run(args.apply)
        finally:
            await dispose_engine()

    asyncio.run(_main())


if __name__ == "__main__":
    main()
