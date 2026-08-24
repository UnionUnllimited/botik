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

import datetime as dt
import re

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.dates import utcnow
from core.enums import DeviceStatus, OrderStatus, SubscriptionStatus
from core.models import Device, Order, Subscription, User
from core.redis_client import RateLimiter
from core.security import normalize_mac
from core.services import remnawave, router_shell, routers, settings_service, subscriptions

log = structlog.get_logger("services.activation")

APPLY_SCRIPT = "/usr/bin/apply_sub.sh"
"""Скрипт прошивки: прописывает ссылку в passwall и перечитывает подписку."""

_USERNAME_ALLOWED = re.compile(r"[^A-Za-z0-9_-]")


class ActivationError(RuntimeError):
    """Понятная клиенту причина отказа: текст уходит прямо в бот."""


def username_for(user: User, mac: str) -> str:
    """Имя учётки в панели: кто клиент и с какого роутера.

    Панель принимает только латиницу, цифры, дефис и подчёркивание, поэтому
    разделители MAC убираем, а результат чистим на случай, если шаблон
    поменяют на что-то с пробелами.

    У клиента с сайта нет Telegram, и основной шаблон дал бы «tgNone_...» —
    одно и то же имя всем таким клиентам на одном роутере. Для них берётся
    отдельный шаблон с нашим id. Имя учётки — ключ поиска в панели, поэтому
    выбор шаблона обязан зависеть только от того, есть ли у клиента tg_id:
    иначе повторная активация не найдёт заведённую учётку и создаст вторую.
    """
    template = (
        settings.remnawave.username_template
        if user.tg_id is not None
        else settings.remnawave.username_template_no_tg
    )
    raw = template.format(
        tg_id=user.tg_id,
        mac=mac.replace(":", "").lower(),
        user_id=user.id,
    )
    return _USERNAME_ALLOWED.sub("", raw)[:34]


def manual_username_for(mac: str) -> str:
    """Имя учётки при ручной активации из админки — сам MAC роутера.

    Клиента у такого устройства может не быть вовсе, и брать имя не от кого.
    Двоеточия панель не принимает, поэтому разделителем идёт дефис: так строка
    хотя бы читается как MAC, когда её ищут в панели глазами.
    """
    cleaned = _USERNAME_ALLOWED.sub("", mac.replace(":", "-").lower())
    return cleaned[:34]


def panel_expiry_of(account: remnawave.RemnaUser | None) -> dt.datetime | None:
    """Срок учётки из ответа панели. Формат её версии нам не подконтролен."""
    if account is None or not account.expire_at:
        return None
    try:
        parsed = dt.datetime.fromisoformat(account.expire_at.replace("Z", "+00:00"))
    except ValueError:
        log.warning("activation.panel_expiry_unparsed", value=account.expire_at)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


async def activate_manually(session: AsyncSession, *, device: Device, days: int) -> dt.datetime:
    """Ручная активация роутера админом: учётка по MAC, срок на `days` и доставка ссылки.

    Отдельный путь от клиентской активации и намеренно не трогает наши подписки:
    здесь нет ни заказа, ни тарифа, а есть роутер, которому нужно выдать доступ —
    служебный, подменный или проданный вне сайта. Возвращает срок в панели.
    """
    if device.status is DeviceStatus.BLOCKED:
        raise ActivationError("Роутер заблокирован. Сначала снимите блокировку.")
    if days < 1:
        raise ActivationError("Срок должен быть хотя бы один день.")

    username = manual_username_for(device.mac)
    expire_at = utcnow() + dt.timedelta(days=days)

    # Владелец, если роутер уже привязан: с ним учётка находится обратно
    # по telegram_id, а не только по имени, и в панели видно, чья она.
    owner = await session.get(User, device.user_id) if device.user_id else None

    try:
        panel = remnawave.client()
        account = await panel.find_user(username)
        if account is None:
            account = await panel.create_user(
                username=username,
                expire_at=expire_at,
                telegram_id=owner.tg_id if owner else None,
                description=(
                    f"Ручная активация · {device.mac}"
                    + (f" · {owner.display_name}" if owner else "")
                ),
            )
        else:
            # Повторная активация того же роутера не должна плодить учётки:
            # переиспользуем и просто переставляем срок.
            await panel.update_expiry(uuid=account.uuid, expire_at=expire_at)
    except remnawave.RemnawaveError as exc:
        log.warning("activation.manual_panel_failed", mac=device.mac, error=str(exc))
        raise ActivationError(f"Панель не приняла запрос: {exc}") from exc

    await _ensure_tunnel(session, device)
    output = await deliver_subscription(device, account.subscription_url)

    now = utcnow()
    device.status = DeviceStatus.ACTIVE
    device.activated_at = device.activated_at or now
    routers.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="success",
        message=f"Ручная активация на {days} дн., учётка {username}",
        payload={"username": username, "until": expire_at.isoformat(), "output": output[:500]},
    )
    log.info("activation.manual_done", mac=device.mac, username=username, days=days)
    return expire_at


SHIPPED_STATUSES = (OrderStatus.SHIPPED, OrderStatus.DELIVERED, OrderStatus.DONE)
"""«Уже у клиента». До отгрузки роутер лежит на столе и его прошивают —
он тоже выходит на связь, и активировать его там нельзя: дни начнут гореть
у мастера, а подписка уедет на устройство, которое ещё никому не отдали."""


async def auto_activate_if_shipped(session: AsyncSession, device: Device) -> bool:
    """Активация отгруженного роутера, когда он впервые вышел на связь у клиента.

    Срок берётся не из настройки, а из того, что человек купил: оплата заводит
    подписку в ожидании активации, и здесь она включается на свой тариф. Так
    «купил три месяца» и означает три месяца, а отсчёт начинается с момента,
    когда роутер получил ссылку, — дни доставки не горят.

    Условие одно и жёсткое: устройство привязано к заказу, а заказ отгружен.
    До отгрузки роутер лежит на столе и прошивается — он тоже виден в туннеле,
    и активировать его там нельзя. Служебные, подменные и стенд активируются
    руками из карточки.

    Отказ не считаем поломкой: туннель после включения поднимается не мгновенно,
    и первая попытка часто приходится на момент, когда SSH ещё не отвечает.
    Следующий обход попробует снова.
    """
    if device.activated_at is not None or device.order_id is None:
        return False
    if not await settings_service.get_bool(session, "activation.auto_enabled"):
        return False

    order = await session.get(Order, device.order_id)
    if order is None or order.status not in SHIPPED_STATUSES:
        return False

    user = await session.get(User, device.user_id or order.user_id)
    if user is None:
        return False

    try:
        # Тот же путь, которым активировался клиент сам: учётка на купленный
        # срок, ссылка по SSH и старт отсчёта. Ограничение частоты здесь ни
        # к чему — это наш обход, а не человек, нажимающий кнопку.
        await activate(session, user=user, raw_mac=device.mac, rate_limited=False)
    except ActivationError as exc:
        log.info("activation.auto_postponed", mac=device.mac, error=str(exc))
        return False

    routers.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="success",
        message=f"Автоактивация после отгрузки, заказ {order.public_number}",
    )
    log.info("activation.auto_done", mac=device.mac, order=order.public_number)
    return True


async def extend_manually(session: AsyncSession, *, device: Device, days: int) -> dt.datetime:
    """Продлевает срок учётки, заведённой ручной активацией.

    Считаем от текущего срока, а не от сегодня: продление истёкшей учётки
    начинает отсчёт заново, а не дарит дни задним числом.
    """
    if days < 1:
        raise ActivationError("Срок должен быть хотя бы один день.")

    username = manual_username_for(device.mac)
    try:
        panel = remnawave.client()
        account = await panel.find_user(username)
        if account is None:
            raise ActivationError(
                f"В панели нет учётки {username} — сначала активируйте роутер."
            )
        now = utcnow()
        current = panel_expiry_of(account)
        expire_at = max(current or now, now) + dt.timedelta(days=days)
        await panel.update_expiry(uuid=account.uuid, expire_at=expire_at)
    except remnawave.RemnawaveError as exc:
        log.warning("activation.manual_extend_failed", mac=device.mac, error=str(exc))
        raise ActivationError(f"Панель не приняла запрос: {exc}") from exc

    routers.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="info",
        message=f"Срок продлён на {days} дн. до {expire_at:%d.%m.%Y}",
        payload={"username": username, "until": expire_at.isoformat()},
    )
    log.info("activation.manual_extended", mac=device.mac, username=username, days=days)
    return expire_at


async def panel_account_of(device: Device) -> remnawave.RemnaUser | None:
    """Учётка ручной активации этого роутера, если она заведена."""
    try:
        return await remnawave.client().find_user(manual_username_for(device.mac))
    except remnawave.RemnawaveError as exc:
        log.warning("activation.manual_lookup_failed", mac=device.mac, error=str(exc))
        return None


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

    # Роутер берём тот, к которому привязана эта подписка. Раньше брался
    # последний активированный у клиента — и у владельца двух роутеров
    # продление первой подписки уезжало на второй роутер: один получал
    # чужие дни, другой отключался в оплаченный срок.
    device = None
    if subscription.device_id is not None:
        device = await session.get(Device, subscription.device_id)

    if device is None:
        # Подписки, заведённые до привязки к устройству, роутера не помнят.
        # Для них остаётся прежний способ — последний активированный.
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


async def activate(
    session: AsyncSession, *, user: User, raw_mac: str, rate_limited: bool = True
) -> Device:
    """Активация целиком. Любой отказ — `ActivationError` с текстом для клиента.

    `rate_limited=False` — для нашего же обхода парка: ограничение частоты
    защищает от подбора MAC человеком, а обход и так приходит раз в минуту
    и по своему списку.
    """
    mac = normalize_mac(raw_mac)
    if not mac:
        raise ActivationError(
            "Не похоже на MAC-адрес. Он выглядит так: A0:B1:C2:D3:E4:F5 — "
            "12 знаков, найдите их на наклейке снизу роутера."
        )

    if rate_limited:
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
