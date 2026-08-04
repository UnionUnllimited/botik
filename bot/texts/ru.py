"""Все тексты клиентского бота. В хендлерах строк быть не должно."""

from __future__ import annotations

from decimal import Decimal

from core.config import settings
from core.dates import days_phrase, format_date_ru
from core.enums import OrderStatus

BRAND = settings.app.brand

# --- кнопки главного меню --------------------------------------------------
BTN_BUY = "🛒 Купить роутер"
BTN_MY_DEVICE = "📶 Мой роутер"
BTN_SUBSCRIPTION = "💎 Подписка"
BTN_GUIDES = "❓ Инструкции"
BTN_SUPPORT = "🎧 Поддержка"
BTN_REFERRAL = "👥 Пригласить друга"

BTN_BACK = "⬅️ Назад"
BTN_CANCEL = "✖️ Отмена"
BTN_MENU = "🏠 Главное меню"
BTN_SKIP = "Пропустить"
BTN_SHARE_PHONE = "📱 Отправить номер"
BTN_CHOOSE = "✅ Выбрать эту модель"
BTN_REFRESH = "🔄 Обновить"
BTN_ACTIVATE = "🚀 Активировать роутер"
BTN_ACTIVATE_RETRY = "🔄 Попробовать снова"

# --- приветствие -----------------------------------------------------------
START_NEW = (
    "👋 <b>{brand}</b>\n\n"
    "Здесь можно купить роутер с подпиской на сервис стабильного доступа "
    "к зарубежным ресурсам, активировать устройство и продлить подписку.\n\n"
    "Роутер приходит настроенным: включаете в розетку — всё работает.\n\n"
    "Выберите раздел ниже 👇"
)
START_RETURNING = "С возвращением, {name}! Выберите раздел 👇"

HELP = (
    "<b>Что умеет бот</b>\n\n"
    "🛒 <b>Купить роутер</b> — каталог, оформление и оплата заказа.\n"
    "📶 <b>Мой роутер</b> — статус устройства и ссылка на подписку.\n"
    "💎 <b>Подписка</b> — срок действия и продление.\n"
    "❓ <b>Инструкции</b> — подключение и решение частых проблем.\n"
    "🎧 <b>Поддержка</b> — живой человек ответит в этом же чате.\n"
    "👥 <b>Пригласить друга</b> — бонусные дни за каждого приглашённого.\n\n"
    "Команды: /start — меню, /help — эта справка."
)

FALLBACK = "Не понял сообщение 🤔 Выберите раздел кнопками ниже или напишите /help."
MENU_MOVED = "Меню переехало под сообщения — старая клавиатура больше не нужна."

SECTION_PENDING = {
    "guides": (
        "❓ <b>Инструкции</b>\n\n"
        "Раздел наполняется: подключение роутера, вход в панель, смена пароля Wi-Fi, "
        "что делать, если пропал интернет.\n\n"
        "Пока любой вопрос можно задать напрямую: {contact}"
    ),
    "support": (
        "🎧 <b>Поддержка</b>\n\n"
        "Работаем {hours}.\n\n"
        "Напишите нам: {contact}\n"
        "Скоро можно будет писать прямо сюда, в этот чат."
    ),
}
BLOCKED_USER = "Доступ к боту ограничен. Если это ошибка — напишите нам: {contact}"
REFERRAL_LINKED = "Вы пришли по приглашению от {name} — бонус начислим после первой покупки. 🎁"
ERROR_GENERIC = (
    "Что-то пошло не так. Мы уже знаем о проблеме и разбираемся.\n"
    "Попробуйте ещё раз через минуту или напишите в поддержку."
)
THROTTLED = "Слишком много сообщений подряд. Подождите пару секунд, пожалуйста."
CANCELLED = "Оформление отменено. Возвращаю в меню."
SECTION_SOON = "Раздел появится в ближайшем обновлении. Напишите в поддержку, если нужно прямо сейчас."


def start_text(*, name: str, is_new: bool) -> str:
    if is_new:
        return START_NEW.format(brand=BRAND)
    return START_RETURNING.format(name=name)


def money(value: Decimal | int | str) -> str:
    """1990.00 -> «1 990 ₽», 149.50 -> «149,50 ₽»."""
    amount = Decimal(str(value))
    if amount == amount.to_integral_value():
        return f"{amount:,.0f} ₽".replace(",", " ")
    return f"{amount:,.2f} ₽".replace(",", " ").replace(".", ",")


# --- каталог ---------------------------------------------------------------
CATALOG_TITLE = "🛒 <b>Выберите роутер</b>\n\nОба работают из коробки — включили и пользуетесь."
CATALOG_EMPTY = "Каталог временно пуст. Загляните позже или напишите в поддержку."
OUT_OF_STOCK = "Этой модели сейчас нет в наличии. Выберите другую или напишите в поддержку."
PLAN_GONE = "Этот тариф больше не доступен. Выберите другой."
DELIVERY_GONE = "Этот способ доставки сейчас недоступен. Выберите другой."


def product_card(*, title: str, subtitle: str, description: str, price: Decimal, specs: dict) -> str:
    lines = [f"<b>{title}</b>"]
    if subtitle:
        lines.append(subtitle)
    if description:
        lines.append(f"\n{description}")
    if specs:
        lines.append("")
        lines.extend(f"• {key}: {value}" for key, value in specs.items())
    lines.append(f"\n💰 Цена: <b>{money(price)}</b>")
    return "\n".join(lines)


PLAN_TITLE = (
    "💎 <b>Подписка на сервис доступа</b>\n\n"
    "Чем длиннее срок — тем выгоднее месяц. Отсчёт начнётся, когда вы включите роутер, "
    "а не в момент оплаты."
)


def plan_button(*, title: str, price: Decimal, months: int, discount: Decimal) -> str:
    per_month = (price / months).quantize(Decimal("1")) if months else price
    label = f"{title} — {money(price)}"
    if months > 1:
        label += f" ({money(per_month)}/мес)"
    if discount > 0:
        label += f" −{discount:.0f}%"
    return label


# --- данные покупателя -----------------------------------------------------
ASK_NAME = "👤 Как вас зовут? Напишите фамилию, имя и отчество — они нужны для доставки."
ASK_NAME_INVALID = "Похоже, это не имя. Напишите ФИО целиком, например: Иванов Иван Иванович."
ASK_PHONE = (
    "📱 Номер телефона для связи и доставки.\n\n"
    "Нажмите кнопку ниже или напишите номер вручную: +7 900 123-45-67"
)
ASK_PHONE_HINT = "Можно отправить номер кнопкой ниже 👇"
ASK_PHONE_INVALID = "Не похоже на российский номер. Пример: +7 900 123-45-67"
ASK_CITY = "🏙 В какой город доставляем?"
ASK_CITY_INVALID = "Напишите название города, например: Екатеринбург."

# --- доставка --------------------------------------------------------------
ASK_DELIVERY = "🚚 <b>Как доставить заказ?</b>"
ASK_DELIVERY_TARGET = "Куда доставить?"
ASK_ADDRESS = "📍 Напишите адрес: улица, дом, квартира, индекс."
ASK_ADDRESS_INVALID = "Адрес слишком короткий. Укажите улицу, дом и квартиру."
ASK_PVZ = (
    "📦 Напишите адрес или код пункта выдачи.\n\n"
    "Найти ближайший можно на сайте перевозчика — если не уверены, "
    "напишите район, и мы подберём сами."
)

# --- промокод --------------------------------------------------------------
ASK_PROMO = "🎟 Есть промокод? Введите его или нажмите «Пропустить»."
PROMO_APPLIED = "✅ Промокод «{code}» применён: скидка {discount}."
PROMO_FAILED = "❌ {reason}\n\nВведите другой промокод или нажмите «Пропустить»."

# --- подтверждение и оплата ------------------------------------------------
PAY_ONLINE = "💳 Оплатить онлайн"
PAY_ON_DELIVERY = "📦 Оплата при получении"


def order_summary(
    *,
    product_title: str | None,
    plan_title: str | None,
    subtotal: Decimal,
    discount: Decimal,
    delivery_price: Decimal,
    total: Decimal,
    name: str,
    phone: str,
    city: str,
    delivery_text: str,
) -> str:
    lines = ["🧾 <b>Проверьте заказ</b>\n"]
    if product_title:
        lines.append(f"📶 {product_title}")
    if plan_title:
        lines.append(f"💎 {plan_title}")
    lines.append("")
    lines.append(f"Товары: {money(subtotal)}")
    if discount > 0:
        lines.append(f"Скидка: −{money(discount)}")
    if delivery_price > 0:
        lines.append(f"Доставка: {money(delivery_price)}")
    elif product_title:
        lines.append("Доставка: бесплатно")
    lines.append(f"<b>Итого: {money(total)}</b>\n")
    lines.append(f"👤 {name}")
    lines.append(f"📱 {phone}")
    if city:
        lines.append(f"🏙 {city}")
    lines.append(f"🚚 {delivery_text}")
    return "\n".join(lines)


CHOOSE_PAYMENT = "Выберите способ оплаты:"
PAYMENT_LINK = (
    "💳 <b>Заказ {number} создан</b>\n\n"
    "Сумма к оплате: <b>{total}</b>\n"
    "Ссылка действует {minutes} минут — если не успеете, создадим новую.\n\n"
    "После оплаты вернитесь в бот, подтверждение придёт автоматически."
)
PAYMENT_BUTTON = "💳 Перейти к оплате"
PAYMENT_CHECK = "🔄 Проверить оплату"
PAYMENT_CONFIRMED = "Оплата подтверждена ✅"
PAYMENT_PENDING = "Платёж ещё не подтверждён. Если вы уже оплатили — подождите минуту, деньги идут."
PAYMENT_FAILED = "Оплата не прошла. Попробуйте ещё раз или выберите другой способ."
PAYMENT_EXPIRED = "Срок действия ссылки истёк. Нажмите «Оплатить заново», чтобы создать новую."
PAY_AGAIN = "🔄 Оплатить заново"
PAYMENT_UNAVAILABLE = (
    "Онлайн-оплата временно недоступна. Оформите заказ с оплатой при получении или напишите в поддержку."
)

ORDER_COD_CREATED = (
    "📦 <b>Заказ {number} принят</b>\n\n"
    "Сумма к оплате при получении: <b>{total}</b>\n"
    "Отправим в течение {shipping_days}, трек-номер пришлём сюда."
)


def payment_success(*, number: str, total: str, shipping_days: str, has_device: bool) -> str:
    lines = [
        "✅ <b>Оплата получена!</b>\n",
        f"Заказ {number} на сумму {total}.",
    ]
    if has_device:
        lines.append(f"Отправим в течение {shipping_days}, трек-номер пришлём в этот чат.")
        lines.append(
            "\nКогда роутер приедет — включите его и нажмите «📶 Мой роутер» → «Активировать». "
            "Подписка начнёт отсчёт с этого момента, дни доставки не сгорают."
        )
    else:
        lines.append(
            "\nПодписка ждёт активации: откройте «💎 Подписка» и следуйте инструкции, чтобы привязать роутер."
        )
    return "\n".join(lines)


# --- статусы заказа --------------------------------------------------------
ORDER_STATUS_TEXTS = {
    OrderStatus.PACKING: "📦 Заказ {number} собирается. Скоро передадим в доставку.",
    OrderStatus.SHIPPED: "🚚 Заказ {number} отправлен!",
    OrderStatus.DELIVERED: "📬 Заказ {number} доставлен. Включайте роутер и активируйте подписку.",
    OrderStatus.DONE: "✅ Заказ {number} закрыт. Спасибо, что выбрали нас!",
    OrderStatus.CANCELLED: "❌ Заказ {number} отменён. {reason}",
    OrderStatus.REFUNDED: "💸 По заказу {number} оформлен возврат. {reason}",
}

TRACK_INFO = "Трек-номер: <code>{track}</code>"
TRACK_LINK = "Отследить"

# --- подписка --------------------------------------------------------------
SUBSCRIPTION_NONE = (
    "💎 <b>Подписка</b>\n\n"
    "Активной подписки пока нет.\n\n"
    "Она входит в комплект при покупке роутера, либо её можно купить отдельно — "
    "если роутер у вас уже есть."
)
SUBSCRIPTION_BUY = "💳 Купить подписку"
SUBSCRIPTION_EXTEND = "🔄 Продлить"

SUBSCRIPTION_PENDING = (
    "💎 <b>Подписка оплачена</b>\n\n"
    "Тариф: {plan}\n"
    "Статус: ждёт активации роутера\n\n"
    "Отсчёт начнётся, когда вы активируете устройство — до {deadline} включительно."
)


def subscription_active(*, plan: str, expires_at, days: int, in_grace: bool) -> str:
    header = "💎 <b>Подписка активна</b>" if not in_grace else "⚠️ <b>Подписка истекла</b>"
    lines = [header, "", f"Тариф: {plan}", f"Действует до: {format_date_ru(expires_at)}"]
    if in_grace:
        lines.append(f"\nДоступ ещё работает {days_phrase(max(days, 0))} — продлите, чтобы он не отключился.")
    else:
        lines.append(f"Осталось: {days_phrase(max(days, 0))}")
    return "\n".join(lines)


SUBSCRIPTION_EXPIRED = (
    "🔴 <b>Подписка закончилась</b>\n\n"
    "Доступ к зарубежным сервисам через роутер отключён. "
    "Продлите подписку — всё восстановится в течение пары минут."
)

# --- мой роутер ------------------------------------------------------------
DEVICE_NONE = (
    "📶 <b>Мой роутер</b>\n\n"
    "К вашему профилю пока не привязано устройство.\n\n"
    "Роутер с подпиской можно купить прямо здесь — он приходит настроенным, "
    "включаете в розетку и пользуетесь."
)
DEVICE_WAITING = (
    "📶 <b>Мой роутер</b>\n\n"
    "Подписка оплачена, роутер едет к вам. Показания появятся здесь, "
    "как только устройство первый раз выйдет на связь.\n\n"
    "Трек-номер пришлём в этот чат."
)
DEVICE_READY_TO_ACTIVATE = (
    "📶 <b>Мой роутер</b>\n\n"
    "Подписка оплачена, роутер за вами закреплён — осталось его включить.\n\n"
    "<b>Что сделать:</b>\n"
    "1. Подключите кабель провайдера в порт <b>WAN</b>.\n"
    "2. Включите питание и дайте роутеру минуту на запуск.\n"
    "3. Нажмите кнопку ниже и введите MAC с наклейки.\n\n"
    "Отсчёт подписки начнётся только после успешной настройки — дни доставки не сгорают."
)
ACTIVATION_ASK_MAC = (
    "🚀 <b>Активация</b>\n\n"
    "Введите MAC-адрес с наклейки на дне роутера. Выглядит он так:\n"
    "<code>A0:B1:C2:D3:E4:F5</code>\n\n"
    "Двоеточия и регистр не важны — можно просто 12 знаков подряд."
)
ACTIVATION_WORKING = "⏳ Настраиваю роутер, это займёт до минуты. Не выключайте его."
ACTIVATION_FAILED = "❌ {reason}"


def activation_done(*, model: str, plan: str, until: str, days: str) -> str:
    return (
        "✅ <b>Роутер активирован!</b>\n\n"
        f"Устройство: {model}\n"
        f"Тариф: {plan}\n"
        f"Подписка действует до {until} — это {days}.\n\n"
        "Всё уже работает: подключайтесь к Wi-Fi роутера, ничего настраивать не нужно. "
        "Состояние устройства всегда видно в разделе «Мой роутер»."
    )


DEVICE_OFFLINE_HINT = (
    "\n\nЕсли роутер включён, проверьте кабель провайдера и питание. "
    "Не помогло — напишите в поддержку, мы посмотрим со своей стороны."
)


def traffic(value: int) -> str:
    """Байты в привычных единицах: точное число клиенту ничего не даёт."""
    amount = float(value or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if amount < 1024 or unit == "ТБ":
            # Десятая доля важна только на мелких числах: «1,5 ГБ», но «128 ГБ».
            precision = 0 if unit == "Б" or amount >= 10 else 1
            return f"{amount:.{precision}f} {unit}".replace(".", ",")
        amount /= 1024
    return f"{amount:.0f} ТБ"


def device_card(
    *,
    title: str,
    mac: str,
    online: bool,
    last_seen,
    service_ok: bool,
    uptime_sec: int,
    clients_wifi: int,
    clients_dhcp: int,
    cpu_pct: int | None,
    ram_pct: int | None,
    rx_bytes: int,
    tx_bytes: int,
    subscription_line: str,
) -> str:
    """Карточка устройства. Показываем только то, что реально знаем."""
    from core.dates import ago_phrase, uptime_phrase

    lines = ["📶 <b>Мой роутер</b>", "", f"<b>{title}</b>", f"Идентификатор: <code>{mac}</code>", ""]

    if online:
        seen = f" · показания {ago_phrase(last_seen)}" if last_seen else ""
        lines.append(f"🟢 <b>На связи</b>{seen}")
        lines.append(f"Доступ к зарубежным сервисам: {'работает' if service_ok else 'не запущен'}")
    else:
        seen = (
            f"последний раз был на связи {ago_phrase(last_seen)}"
            if last_seen
            else "ещё не выходил на связь"
        )
        lines.append(f"🔴 <b>Нет связи</b> — {seen}")

    if online:
        clients = clients_wifi + clients_dhcp
        if clients:
            lines.append(f"Устройств в сети: {clients} (по Wi-Fi {clients_wifi})")
        if uptime_sec > 0:
            lines.append(f"Работает без перезагрузки: {uptime_phrase(uptime_sec)}")
        if cpu_pct is not None or ram_pct is not None:
            load = " · ".join(
                part
                for part in (
                    f"процессор {cpu_pct}%" if cpu_pct is not None else "",
                    f"память {ram_pct}%" if ram_pct is not None else "",
                )
                if part
            )
            lines.append(f"Загрузка: {load}")
        if rx_bytes or tx_bytes:
            lines.append(f"Трафик: ↓ {traffic(rx_bytes)} · ↑ {traffic(tx_bytes)}")

    lines.append("")
    lines.append(subscription_line)
    if not online:
        lines.append(DEVICE_OFFLINE_HINT.strip())
    return "\n".join(lines)


DEVICE_SUB_NONE = "💎 Подписка: не оформлена — доступ работать не будет."
DEVICE_SUB_PENDING = "💎 Подписка: {plan}, ждёт активации роутера."
DEVICE_SUB_ACTIVE = "💎 Подписка: {plan}, до {until} (осталось {days})."
DEVICE_SUB_GRACE = "⚠️ Подписка истекла, доступ работает ещё {days}. Продлите, чтобы не отключился."
DEVICE_SUB_EXPIRED = "🔴 Подписка закончилась — доступ отключён. Продлите, и всё восстановится."

REMINDER_BEFORE = "⏳ Подписка заканчивается через {days}.\nПродлите заранее, чтобы доступ не прерывался."
REMINDER_LAST_DAY = "⏳ Сегодня последний день подписки. Продлите, чтобы доступ не отключился."
REMINDER_AFTER = "⚠️ Подписка закончилась {days} назад. Доступ работает ещё {grace}, потом отключится."


# --- рефералы --------------------------------------------------------------
def referral_text(*, link: str, invited: int, rewarded: int, bonus_days: int) -> str:
    return (
        "👥 <b>Пригласите друга</b>\n\n"
        f"За каждого, кто купит роутер или подписку по вашей ссылке, "
        f"дарим <b>{days_phrase(bonus_days)}</b> подписки.\n\n"
        f"Ваша ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: {invited}\nС покупкой: {rewarded}"
    )


REFERRAL_SHARE = "Поделиться ссылкой"
REFERRAL_SHARE_TEXT = (
    "Купил роутер с подпиской на сервис стабильного доступа к зарубежным сервисам — "
    "работает сразу из коробки, ничего настраивать не надо."
)

# --- уведомления админам ---------------------------------------------------
ADMIN_NEW_ORDER = (
    "🆕 <b>Новый заказ {number}</b>\n"
    "Клиент: {customer} ({user_link})\n"
    "Состав: {items}\n"
    "Сумма: {total}\n"
    "Оплата: {payment}\n"
    "Доставка: {delivery}"
)
ADMIN_PAYMENT_OK = "💰 <b>Оплата заказа {number}</b>\nСумма: {total}\nКлиент: {customer}"
ADMIN_PAYMENT_MISMATCH = (
    "🚨 <b>Расхождение суммы платежа</b>\n"
    "Платёж #{payment_id}: ожидали {expected}, получили {received}.\n"
    "Платёж не зачислен, проверьте вручную."
)
