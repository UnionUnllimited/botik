"""Активация роутера клиентом.

Цепочка целиком: клиент называет MAC с корпуса → находим его устройство →
поднимаем к нему туннель → заводим учётку в панели → по SSH отдаём роутеру
ссылку подписки → запускаем отсчёт срока.

Два решения, которые стоит держать в голове.

**Ссылку выдаёт панель, а не мы.** Роутер получает `subscriptionUrl` из
Remnawave, а не наш `/sub/{token}`. Это заметно проще, но значит, что
отключение доступа при неоплате делается тоже в панели, а наш журнал
обращений за подпиской остаётся пустым: запросы идут мимо нас.

**Отсчёт срока начинается только после успешной доставки.** Если роутер
не ответил, подписка остаётся ждущей активации и дни клиента не горят.
"""

from __future__ import annotations

import re

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.dates import utcnow
from core.enums import DeviceStatus, SubscriptionStatus
from core.models import Device, Subscription, User
from core.redis_client import RateLimiter
from core.security import normalize_mac
from core.services import remnawave, router_shell, routers, subscriptions

log = structlog.get_logger("services.activation")

APPLY_SCRIPT = "/usr/bin/apply_sub.sh"
"""Скрипт прошивки: прописывает ссылку в passwall и перечитывает подписку."""

_USERNAME_ALLOWED = re.compile(r"[^A-Za-z0-9_-]")


class ActivationError(RuntimeError):
    """Понятная клиенту причина отказа: текст уходит прямо в бот."""


def username_for(user: User, mac: str) -> str:
    """Имя учётки в панели: телеграм клиента и MAC роутера.

    Панель принимает только латиницу, цифры, дефис и подчёркивание, поэтому
    разделители MAC убираем, а результат чистим на случай, если шаблон
    поменяют на что-то с пробелами.
    """
    raw = settings.remnawave.username_template.format(
        tg_id=user.tg_id,
        mac=mac.replace(":", "").lower(),
        user_id=user.id,
    )
    return _USERNAME_ALLOWED.sub("", raw)[:34]


async def _check_rate_limit(user: User) -> None:
    limiter = RateLimiter()
    allowed, _ = await limiter.hit(
        # Ключ по нашему id, а не по tg_id: у клиента с сайта его нет,
        # и все такие попали бы в общее ведро «activation:None» на всех.
        f"activation:{user.id}",
        limit=settings.security.activation_attempts_per_hour,
        window_sec=3600,
    )
    if not allowed:
        raise ActivationError(
            "Слишком много попыток активации. Попробуйте через час или напишите в поддержку."
        )


async def _resolve_device(session: AsyncSession, user: User, mac: str) -> Device:
    """Ищет устройство по MAC и проверяет, что клиент имеет на него право.

    Чужой роутер активировать нельзя: MAC написан на корпусе, и без этой
    проверки его хватило бы, чтобы увести устройство у другого клиента.
    """
    device = await session.scalar(select(Device).where(Device.mac == mac))

    if device is None:
        raise ActivationError(
            "Такой роутер не найден. Проверьте MAC на наклейке — "
            "он состоит из 12 знаков, буквы только от A до F."
        )
    if device.status is DeviceStatus.BLOCKED:
        raise ActivationError("Роутер заблокирован. Напишите в поддержку, разберёмся.")
    if device.user_id is not None and device.user_id != user.id:
        raise ActivationError("Этот роутер уже активирован другим аккаунтом.")

    return device


async def _pending_subscription(session: AsyncSession, user: User) -> Subscription:
    subscription = await subscriptions.get_pending(session, user.id)
    if subscription is None:
        active = await subscriptions.get_active(session, user.id)
        if active is not None:
            raise ActivationError(
                "Подписка уже активна. Если роутер не работает, напишите в поддержку."
            )
        raise ActivationError(
            "Нет оплаченной подписки. Оформите её в разделе «Подписка» — после оплаты вернитесь сюда."
        )
    if subscription.plan is None:
        # Без тарифа не посчитать ни срок в панели, ни дату окончания у нас.
        raise ActivationError("У подписки не указан тариф. Напишите в поддержку, поправим вручную.")
    return subscription


async def _ensure_tunnel(session: AsyncSession, device: Device) -> None:
    """Готовит туннель к роутеру и ждёт, пока visitor его подхватит."""
    from worker.tasks.frpc_config import sync_frpc_config

    await routers.ensure_frp_binding(session, device)
    await session.flush()
    await sync_frpc_config()


async def deliver_subscription(device: Device, url: str) -> str:
    """Отдаёт роутеру ссылку подписки скриптом прошивки."""
    safe = url.replace("'", "'\\''")
    result = await router_shell.run(device, f"{APPLY_SCRIPT} '{safe}'", timeout=60)
    if not result.ok:
        raise ActivationError(
            "Роутер не принял настройки. Проверьте, что он включён и подключён к интернету "
            f"кабелем провайдера. Ответ устройства: {result.output[:200] or 'пусто'}"
        )
    return result.output[:2000]


async def sync_panel_expiry(session: AsyncSession, subscription: Subscription) -> bool:
    """Переносит срок подписки в учётку панели.

    Вызывается после любого продления. Ничего не бросает: подписка у нас уже
    продлена и оплата принята, и падать из-за недоступной панели нельзя —
    расхождение поправит следующий вызов или админ руками.

    Возвращает True, если срок в панели обновлён.
    """
    if subscription.expires_at is None:
        return False

    device = await session.scalar(
        select(Device)
        .where(
            Device.user_id == subscription.user_id,
            Device.status.notin_([DeviceStatus.REVOKED, DeviceStatus.BLOCKED]),
        )
        .order_by(Device.activated_at.desc().nulls_last(), Device.id.desc())
        .limit(1)
    )
    if device is None:
        # Роутер ещё не активирован — учётки в панели тоже нет, синхронизировать нечего.
        return False

    owner = await session.get(User, subscription.user_id)
    if owner is None:
        return False

    username = username_for(owner, device.mac)
    try:
        panel = remnawave.client()
        account = await panel.find_user(username)
        if account is None:
            log.warning("activation.expiry_sync_no_account", username=username)
            return False
        await panel.update_expiry(uuid=account.uuid, expire_at=subscription.expires_at)
    except remnawave.RemnawaveError as exc:
        log.warning("activation.expiry_sync_failed", username=username, error=str(exc))
        return False

    routers.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="info",
        message=f"Срок в панели продлён до {subscription.expires_at:%d.%m.%Y}",
        payload={"username": username},
    )
    return True


async def activate(session: AsyncSession, *, user: User, raw_mac: str) -> Device:
    """Активация целиком. Любой отказ — `ActivationError` с текстом для клиента."""
    mac = normalize_mac(raw_mac)
    if not mac:
        raise ActivationError(
            "Не похоже на MAC-адрес. Он выглядит так: A0:B1:C2:D3:E4:F5 — "
            "12 знаков, найдите их на наклейке снизу роутера."
        )

    await _check_rate_limit(user)
    device = await _resolve_device(session, user, mac)
    subscription = await _pending_subscription(session, user)

    now = utcnow()
    username = username_for(user, mac)
    panel = remnawave.client()

    # Повторная активация того же роутера не должна плодить учётки.
    try:
        account = await panel.find_user(username)
        if account is None:
            account = await panel.create_user(
                username=username,
                expire_at=subscriptions.period_end_for(subscription.plan, start=now),
                telegram_id=user.tg_id,
                description=f"{user.display_name} · {mac}",
            )
    except remnawave.RemnawaveError as exc:
        log.warning("activation.panel_failed", mac=mac, error=str(exc))
        raise ActivationError(
            "Не получилось подготовить подписку на сервере. Мы уже видим проблему — "
            "попробуйте через несколько минут."
        ) from exc

    await _ensure_tunnel(session, device)

    try:
        output = await deliver_subscription(device, account.subscription_url)
    except router_shell.ShellError as exc:
        log.warning("activation.delivery_failed", mac=mac, error=str(exc))
        routers.add_event(
            session,
            device_id=device.id,
            mac=device.mac,
            level="error",
            message=f"Активация: роутер недоступен ({exc})",
        )
        raise ActivationError(
            "Роутер не отвечает. Включите его, дождитесь, пока загорится индикатор интернета, "
            "и попробуйте снова через пару минут."
        ) from exc

    device.user_id = user.id
    device.status = DeviceStatus.ACTIVE
    device.activated_at = device.activated_at or now

    if subscription.status is SubscriptionStatus.PENDING:
        subscriptions.activate(subscription, plan=subscription.plan, device_id=device.id, now=now)
    else:
        subscription.device_id = device.id

    routers.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="success",
        message=f"Активирован клиентом, учётка {username}",
        payload={"username": username, "output": output[:500]},
    )
    log.info("activation.done", mac=mac, user_id=user.id, username=username)
    return device
