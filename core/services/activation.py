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
from urllib.parse import urlsplit, urlunsplit

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import notifications, texts
from core.config import settings
from core.dates import utcnow
from core.enums import DeviceStatus, OrderStatus, SubscriptionStatus
from core.models import Device, Subscription, User
from core.redis_client import RateLimiter
from core.security import normalize_mac
from core.services import orders as order_service
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
        mac=mac.replace(":", "").upper(),
        user_id=user.id,
    )
    return _USERNAME_ALLOWED.sub("", raw)[:34]


def manual_username_for(mac: str) -> str:
    """Имя учётки при ручной активации из админки — сам MAC роутера.

    Клиента у такого устройства может не быть вовсе, и брать имя не от кого.
    Двоеточия панель не принимает, поэтому разделителем идёт дефис: так строка
    хотя бы читается как MAC, когда её ищут в панели глазами.

    Заглавными — тем же видом, каким MAC показан в админке и напечатан
    на наклейке. Оператор ищет в панели копипастой, и строчное имя по такому
    запросу не находилось: поиск в панели регистр не прощает.
    """
    cleaned = _USERNAME_ALLOWED.sub("", mac.replace(":", "-").upper())
    return cleaned[:34]


async def _find_account(panel: remnawave.RemnawaveClient, username: str):
    """Учётка по имени, без учёта регистра.

    Точного поиска мало: имена стали заглавными, а всё, что заведено раньше,
    осталось строчным. Не найдя, панель завела бы **вторую** учётку на тот же
    роутер — а он к этому моменту уже ходит по ссылке первой, и подписку
    продлевали бы не ту.
    """
    account = await panel.find_user(username)
    if account is not None:
        return account
    key = username.strip().lower()
    for existing in await panel.users():
        if existing.username.strip().lower() == key:
            return existing
    return None


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
        account = await _find_account(panel, username)
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

    # Клиентская активация ловит это ниже, а ручная — не ловила, и отказ SSH
    # уходил наружу пятисоткой: оператор видел «Основное приложение ответило
    # 500» и ни слова о причине. Отказ роутера — обычное дело (не включён,
    # туннель не поднялся, пароль сменили), и он обязан читаться словами.
    try:
        await _ensure_tunnel(session, device)
        output = await deliver_subscription(device, account.subscription_url)
    except (router_shell.ShellError, ActivationError) as exc:
        log.warning("activation.manual_delivery_failed", mac=device.mac, error=str(exc))
        routers.add_event(
            session,
            device_id=device.id,
            mac=device.mac,
            level="error",
            message=f"Ручная активация: роутер недоступен ({exc})",
        )
        raise ActivationError(
            f"Учётка в панели готова, но роутер не принял ссылку: {exc}. "
            "Проверьте, что он на связи, и нажмите «Активировать заново» — "
            "срок в панели уже проставлен, второй раз он не сдвинется."
        ) from exc
    except Exception as exc:
        # До сюда доходит всё, что ломается по дороге к роутеру, кроме отказа
        # SSH: подготовка туннеля пересобирает конфиг frpc и перезапускает
        # контейнер, а это файлы и docker — там свои способы отказать.
        #
        # Пятисотка здесь особенно вредна: учётка в панели уже заведена и срок
        # проставлен, то есть половина работы сделана, а оператор видит
        # «Основное приложение ответило 500» и не знает ни что случилось,
        # ни в каком состоянии остался роутер. Причина уходит в лог с полной
        # трассировкой, оператору — та же строка человеческим языком.
        log.exception("activation.manual_failed", mac=device.mac, error=str(exc))
        routers.add_event(
            session,
            device_id=device.id,
            mac=device.mac,
            level="error",
            message=f"Ручная активация сорвалась: {exc}"[:500],
        )
        raise ActivationError(
            f"Учётка в панели готова, но доставить ссылку не вышло: {exc}. "
            "Срок в панели уже проставлен — повторное нажатие его не сдвинет."
        ) from exc

    now = utcnow()
    device.status = DeviceStatus.ACTIVE
    device.activated_at = device.activated_at or now

    # Подписка заводится на **этот** роутер. До сих пор ручная активация
    # не оставляла в наших таблицах ничего: доступ в панели был, а парк писал
    # «подписка: нет» у роутера, который в эту секунду работает.
    #
    # Только когда у роутера есть владелец: подписка принадлежит клиенту,
    # и у служебного роутера её не к кому привязать. Такой так и останется
    # без подписки — это правда, а не пробел.
    if owner is not None:
        await subscriptions.grant_manual(
            session, user_id=owner.id, device_id=device.id, days=days, now=now
        )

    routers.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="success",
        message=f"Ручная активация на {days} дн., учётка {username}",
        payload={"username": username, "until": expire_at.isoformat(), "output": output[:500]},
    )
    # Ручная активация обычно про стенд и подменные, но если роутер привязан
    # к заказу — заказ тоже состоялся, и статус должен это показывать.
    await mark_order_activated(session, device)

    log.info("activation.manual_done", mac=device.mac, username=username, days=days)
    return expire_at


async def mark_order_activated(session: AsyncSession, device: Device) -> bool:
    """Переводит заказ роутера в «Активирован» и готовит письмо клиенту.

    Это последний статус живого заказа, и ставит его не оператор: только
    активация знает, что ссылка доехала до устройства. До неё «Отправлен»
    висел до тех пор, пока кто-нибудь не вспомнит закрыть заказ руками,
    а закрывать его никто не вспоминал.

    Тихо ничего не делаем, если заказа нет или он уже закрыт: активация —
    работа с роутером, и падать в ней из-за статуса заказа нельзя.
    """
    if device.order_id is None:
        return False
    order = await order_service.load_for_status(session, device.order_id)
    if order is None or not order_service.can_transition(order.status, OrderStatus.ACTIVATED):
        return False

    order_service.set_status(order, OrderStatus.ACTIVATED)
    user = await session.get(User, order.user_id)
    if user is not None:
        # Сообщение шлёт их бот: клиент разговаривает с ним. Мы кладём текст
        # в очередь — тем же способом, что и остальные наши уведомления.
        await notifications.send_message(
            user.tg_id,
            texts.ORDER_STATUS_TEXTS[OrderStatus.ACTIVATED].format(
                number=order.public_number, reason=""
            ),
            session=session,
            kind="order_status",
        )
    log.info("activation.order_activated", order=order.public_number, mac=device.mac)
    return True


SHIPPED_STATUSES = (
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
    OrderStatus.ACTIVATED,
    OrderStatus.DONE,
)
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

    order = await order_service.load_for_status(session, device.order_id)
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

    try:
        # По MAC, а не по имени ручной активации: роутер, проданный обычным
        # путём, носит учётку `tg{id}_{mac}`, и поиск одним именем отвечал
        # «сначала активируйте роутер» на работающем устройстве.
        accounts = await _router_accounts(device)
        if not accounts:
            raise ActivationError(
                f"В панели нет учётки роутера {device.mac} — сначала активируйте его."
            )
        account = accounts[0]
        now = utcnow()
        current = panel_expiry_of(account)
        expire_at = max(current or now, now) + dt.timedelta(days=days)
        await remnawave.client().update_expiry(uuid=account.uuid, expire_at=expire_at)
    except remnawave.RemnawaveError as exc:
        log.warning("activation.manual_extend_failed", mac=device.mac, error=str(exc))
        raise ActivationError(f"Панель не приняла запрос: {exc}") from exc
    username = account.username

    # Наша подписка двигается вместе с панелью: разъехавшись, они показывают
    # оператору один срок, а отключают доступ по другому.
    if device.user_id:
        # Срок берём тот же, что проставлен в панели, а не пересчитываем
        # днями: целые дни отбрасывают часы, и наша дата отставала от неё
        # почти на сутки после каждого продления.
        await subscriptions.grant_manual(
            session,
            user_id=device.user_id,
            device_id=device.id,
            days=days,
            until=expire_at,
        )

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


def _mac_key(value: str) -> str:
    """Только буквы и цифры в нижнем регистре: `A0:B1` и `a0-b1` — одно и то же."""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def account_of_router(username: str, mac: str) -> bool:
    """Учётка принадлежит этому роутеру, каким бы способом её ни завели.

    Общее у всех имён одно — MAC в конце: `tg{id}_{mac}` у клиентской
    активации, `id{user_id}_{mac}` у клиента без Telegram, сам MAC у ручной.
    Подписка для телефона (`tg{id}` без MAC) под правило не подходит — и не
    должна: к роутеру она отношения не имеет.
    """
    key = _mac_key(mac)
    return bool(key) and _mac_key(username).endswith(key)


async def _router_accounts(device: Device) -> list[remnawave.RemnaUser]:
    """Учётки этого роутера в панели. Бросает `RemnawaveError`, как и панель.

    Ищем по MAC, а не по имени: имя зависит от того, каким путём роутер
    активировали, и поиск одним именем не находил половину парка — экран
    клиента бесконечно писал «подписка настраивается» у работающего роутера.

    Заведённая руками идёт первой: если учёток две (роутер активировали
    из карточки поверх клиентской), ссылку он получил от неё — она доставлена
    последней.
    """
    accounts = [
        account
        for account in await remnawave.client().users()
        if account_of_router(account.username, device.mac)
    ]
    manual = manual_username_for(device.mac).lower()
    # Сравниваем в нижнем регистре с обеих сторон: имена теперь заглавные,
    # а заведённые раньше остались строчными — сравнение как есть перестало
    # бы узнавать ручную учётку, и первой в списке вставала бы клиентская.
    accounts.sort(key=lambda account: account.username.strip().lower() != manual)
    return accounts


async def panel_account_of(device: Device) -> remnawave.RemnaUser | None:
    """Учётка этого роутера, если она заведена.

    Молчащая панель — не ошибка: экран клиента и карточка парка не должны
    падать из-за неё, они показывают срок, а не выдают доступ.
    """
    try:
        accounts = await _router_accounts(device)
    except remnawave.RemnawaveError as exc:
        log.warning("activation.panel_lookup_failed", mac=device.mac, error=str(exc))
        return None
    return accounts[0] if accounts else None


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


async def _pending_subscription(
    session: AsyncSession, user: User, device: Device | None = None
) -> Subscription:
    """Подписка, которую включает этот роутер.

    Сперва — подписка его заказа: у клиента, купившего два роутера, ожидающих
    подписки две, и приезжают устройства в разные дни. Без этого роутер,
    купленный на месяц, включал бы годовой срок соседа.
    """
    subscription = await subscriptions.get_pending(
        session, user.id, order_id=device.order_id if device else None
    )
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


def public_subscription_url(url: str) -> str:
    """Подменяет хост в ссылке подписки на прикрытие, если оно задано.

    Панель отдаёт ссылку на свой домен, и роутер идёт прямо к ней: адрес
    панели оказывается прописан в прошивке у каждого клиента и достижим
    из любой домашней сети. С заданным `REMNAWAVE_SUB_PUBLIC_HOST` роутер
    ходит через него.

    Путь и токен не трогаем — их выдала панель, по ним она узнаёт клиента.
    Меняется только то, к какой двери роутер подходит.
    """
    host = settings.remnawave.sub_public_host.strip()
    if not host:
        return url
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        # Не ссылка, а что-то другое: молча подменять хост тут не в чем,
        # и лучше отдать как есть, чем собрать битый адрес.
        return url
    cover = urlsplit(host if "://" in host else f"https://{host}")
    return urlunsplit(
        (cover.scheme or "https", cover.netloc, parsed.path, parsed.query, parsed.fragment)
    )


async def deliver_subscription(device: Device, url: str) -> str:
    """Отдаёт роутеру ссылку подписки скриптом прошивки."""
    url = public_subscription_url(url)
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

    try:
        # По MAC: учётку мог завести и клиент (`tg{id}_{mac}`), и оператор
        # руками (сам MAC). Поиск одним именем оставлял половину роутеров
        # без переноса срока — клиент оплачивал, а доступ отключался в старую дату.
        accounts = await _router_accounts(device)
        if not accounts:
            log.warning("activation.expiry_sync_no_account", mac=device.mac)
            return False
        account = accounts[0]
        await remnawave.client().update_expiry(
            uuid=account.uuid, expire_at=subscription.expires_at
        )
    except remnawave.RemnawaveError as exc:
        log.warning("activation.expiry_sync_failed", mac=device.mac, error=str(exc))
        return False
    username = account.username

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
    subscription = await _pending_subscription(session, user, device)

    now = utcnow()
    username = username_for(user, mac)
    panel = remnawave.client()

    # Повторная активация того же роутера не должна плодить учётки.
    try:
        account = await _find_account(panel, username)
        expire_at = subscriptions.period_end_for(subscription.plan, start=now)
        if account is None:
            account = await panel.create_user(
                username=username,
                expire_at=expire_at,
                telegram_id=user.tg_id,
                description=f"{user.display_name} · {mac}",
            )
        else:
            # Учётка осталась с прошлой активации, и срок в ней прежний —
            # роутер сбрасывали на склад и активируют заново. Без переноса
            # клиент получает ссылку, по которой доступ уже кончился.
            await panel.update_expiry(uuid=account.uuid, expire_at=expire_at)
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
    # Заказ закрывается сам: ссылка доехала до устройства, и ждать, пока
    # кто-нибудь вспомнит нажать «Доставлен», больше не нужно.
    await mark_order_activated(session, device)

    log.info("activation.done", mac=mac, user_id=user.id, username=username)
    return device
