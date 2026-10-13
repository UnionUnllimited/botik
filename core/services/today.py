"""Главная страница админки: дела на сегодня и счётчики.

Главная родного продукта считала подписки для телефона: «подписки 1 — 0,2 %
от базы», источник регистрации «Telegram 100 %», топ трафика, всегда пустой
у роутеров. Оператору магазина нужно другое — что сейчас требует его руки.

Дела считает тот же `monitoring.collect`, что и утреннюю сводку. Разошлись бы
они — страница говорила бы «всё хорошо» в то утро, когда сводка пришла
с тремя заказами. Отличие одно: оплаченные заказы здесь все, а не только
опаздывающие, — это список работы, а не жалоба.

Ссылки — видом и номером, а не адресом: адреса у админки бота свои, с её
секретным путём, и знает их только она.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.dates import ensure_utc, to_display, utcnow
from core.enums import PaymentStatus, SubscriptionStatus
from core.models import Device, Order, Payment, Subscription
from core.services import monitoring

LATE_HOURS = monitoring.UNSHIPPED_HOURS
"""С какого возраста оплаченный заказ красный. Тот же порог, с которого
он попадает в утреннюю сводку: красное на странице и строка в сводке —
одно и то же."""


def ago(moment: dt.datetime | None, now: dt.datetime) -> str:
    """«40 мин», «5 ч», «3 дн» — как давно. Точное время оператору не нужно,
    ему нужно понять, горит или нет."""
    if moment is None:
        return "давно"
    seconds = max(0, int((now - ensure_utc(moment)).total_seconds()))
    if seconds < 3600:
        return f"{max(1, seconds // 60)} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч"
    return f"{seconds // 86400} дн"


def _date(moment: dt.datetime | None) -> str:
    return to_display(moment).strftime("%d.%m") if moment else "—"


def _client(device: Device) -> str:
    user = device.user if "user" in device.__dict__ else None
    return user.telegram_name if user is not None else "без клиента"


def _item(title: str, sub: str, kind: str, ident: int, *, late: bool = False) -> dict[str, Any]:
    return {"title": title, "sub": sub, "link": {"kind": kind, "id": ident}, "late": late}


def groups(digest: monitoring.Digest, *, now: dt.datetime) -> list[dict[str, Any]]:
    """Дела по группам, от горящего к терпящему. Пустые группы не отдаём:
    заголовок «0» на главной — шум, который приучает не смотреть."""
    result: list[dict[str, Any]] = []

    def add(key: str, title: str, hint: str, tone: str, items: list[dict[str, Any]]) -> None:
        if items:
            result.append({"key": key, "title": title, "hint": hint, "tone": tone, "items": items})

    late_since = now - dt.timedelta(hours=LATE_HOURS)
    add(
        "unshipped",
        "Оплачены, ждут отгрузки",
        "Деньги взяты — заказ должен уехать за один-два рабочих дня.",
        "warning",
        [
            _item(
                order.public_number,
                f"{why} · оплачен {ago(order.paid_at, now)} назад",
                "order",
                order.id,
                late=bool(order.paid_at and ensure_utc(order.paid_at) < late_since),
            )
            for order, why in sorted(digest.unshipped, key=lambda pair: pair[0].paid_at or now)
        ],
    )
    add(
        "stuck",
        "Включён, а подписка не включилась",
        f"Роутер на связи дольше {monitoring.STUCK_HOURS} ч после отгрузки — автоактивация "
        "откладывает молча. Откройте карточку и активируйте.",
        "danger",
        [
            _item(device.mac, f"{order.public_number} · {_client(device)}", "router", device.id, late=True)
            for order, device in digest.stuck
        ],
    )
    add(
        "shipped_silent",
        "Отгружен, но так и не включён",
        "Неделю с отгрузки роутер ни разу не вышел на связь: посылка потерялась "
        "или клиент её не включил. Позвоните.",
        "warning",
        [
            _item(
                order.public_number,
                f"отгружен {_date(order.shipped_at)} · роутер {device.mac}",
                "order",
                order.id,
            )
            for order, device in digest.shipped_silent
        ],
    )
    add(
        "silent",
        "Молчит у клиента",
        f"Активированный роутер не выходит на связь дольше {monitoring.SILENT_HOURS} ч.",
        "warning",
        [
            _item(
                device.mac,
                f"{_client(device)} · молчит {ago(monitoring.last_seen(device), now)}",
                "router",
                device.id,
            )
            for device in digest.silent
        ],
    )
    add(
        "expiring",
        "Подписка кончается",
        f"Ближайшие {monitoring.EXPIRING_DAYS} дн. Напоминания клиенту ушли — "
        "это список для звонка, если они не сработали.",
        "info",
        [
            _item(
                user.telegram_name,
                f"кончается {_date(subscription.expires_at)}",
                "router" if subscription.device_id else "client",
                subscription.device_id or (user.tg_id or 0),
            )
            for subscription, user in sorted(digest.expiring, key=lambda pair: pair[0].expires_at or now)
        ],
    )
    return result


async def counters(session: AsyncSession, *, now: dt.datetime) -> dict[str, Any]:
    """Четыре числа сверху: за сутки и прямо сейчас."""
    day_ago = now - dt.timedelta(days=1)
    total = await session.scalar(select(func.count()).select_from(Device)) or 0
    online = await session.scalar(
        select(func.count()).select_from(Device).where(Device.frp_online.is_(True))
    ) or 0
    paid = await session.scalar(
        select(func.count()).select_from(Order).where(Order.paid_at >= day_ago)
    ) or 0
    revenue = await session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.paid_at >= day_ago, Payment.status == PaymentStatus.SUCCEEDED
        )
    )
    active = await session.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.status == SubscriptionStatus.ACTIVE)
    ) or 0
    return {
        "fleet_total": int(total),
        "fleet_online": int(online),
        "orders_paid_24h": int(paid),
        "revenue_24h": f"{revenue or 0:.2f}",
        "subscriptions_active": int(active),
    }


async def build(session: AsyncSession, *, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    digest = await monitoring.collect(session, now=now, unshipped_hours=0)
    found = groups(digest, now=now)
    return {
        "generated_at": now.isoformat(),
        "counters": await counters(session, now=now),
        "groups": found,
        "total": sum(len(group["items"]) for group in found),
    }
