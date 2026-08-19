"""Тексты уведомлений, которые шлёт не бот.

Оплату подтверждает вебхук, напоминания об окончании шлёт воркер, о расхождении
суммы узнаёт админ-канал — всё это живёт вне пакета бота и не должно от него
зависеть. Формулировки переехали сюда из `bot/texts/ru.py`, когда основой стал
сторонний бот: `core/` обязан работать независимо от того, какой бот сегодня
подключён и подключён ли вообще.

Тексты без упоминания кнопок конкретного бота — они переживут его замену.
"""

from __future__ import annotations

from decimal import Decimal

from core.enums import OrderStatus

ADMIN_PAYMENT_OK = "💰 <b>Оплата заказа {number}</b>\nСумма: {total}\nКлиент: {customer}"

ADMIN_PAYMENT_MISMATCH = (
    "🚨 <b>Расхождение суммы платежа</b>\n"
    "Платёж #{payment_id}: ожидали {expected}, получили {received}.\n"
    "Платёж не зачислен, проверьте вручную."
)

TRACK_INFO = "Трек-номер: <code>{track}</code>"
TRACK_LINK = "Отследить посылку"

REMINDER_BEFORE = "⏳ Подписка заканчивается через {days}.\nПродлите заранее, чтобы доступ не прерывался."
REMINDER_LAST_DAY = "⏳ Сегодня последний день подписки. Продлите, чтобы доступ не отключился."
REMINDER_AFTER = "⚠️ Подписка закончилась {days} назад. Доступ работает ещё {grace}, потом отключится."

SUBSCRIPTION_OPEN = "Открыть кабинет"

ORDER_STATUS_TEXTS = {
    OrderStatus.PACKING: "📦 Заказ {number} собирается. Скоро передадим в доставку.",
    OrderStatus.SHIPPED: "🚚 Заказ {number} отправлен!",
    OrderStatus.DELIVERED: "📬 Заказ {number} доставлен. Включайте роутер и активируйте подписку.",
    OrderStatus.DONE: "✅ Заказ {number} закрыт. Спасибо, что выбрали нас!",
    OrderStatus.CANCELLED: "❌ Заказ {number} отменён. {reason}",
    OrderStatus.REFUNDED: "💸 По заказу {number} оформлен возврат. {reason}",
}

ORDER_STATUS_TITLES = {
    OrderStatus.NEW: "Новый",
    OrderStatus.AWAITING_PAYMENT: "Ждёт оплаты",
    OrderStatus.PAID: "Оплачен",
    OrderStatus.PACKING: "Собираем",
    OrderStatus.SHIPPED: "Отправлен",
    OrderStatus.DELIVERED: "Доставлен",
    OrderStatus.DONE: "Завершён",
    OrderStatus.CANCELLED: "Отменён",
    OrderStatus.REFUNDED: "Возврат",
}
"""Названия статусов для выгрузок и писем. У админки бота свой такой же
словарь: она в другом процессе, с другим venv, и до `core` не дотягивается."""


def money(value: Decimal | int | str) -> str:
    """1990.00 -> «1 990 ₽», 149.50 -> «149,50 ₽»."""
    amount = Decimal(str(value))
    if amount == amount.to_integral_value():
        return f"{amount:,.0f} ₽".replace(",", " ")
    return f"{amount:,.2f} ₽".replace(",", " ").replace(".", ",")


def payment_success(*, number: str, total: str, shipping_days: str, has_device: bool) -> str:
    lines = [
        "✅ <b>Оплата получена!</b>\n",
        f"Заказ {number} на сумму {total}.",
    ]
    if has_device:
        lines.append(f"Отправим в течение {shipping_days}, трек-номер пришлём сюда же.")
        lines.append(
            "\nКогда роутер приедет — включите его и активируйте по MAC-адресу с наклейки. "
            "Подписка начнёт отсчёт с этого момента, дни доставки не сгорают."
        )
    else:
        lines.append("\nПодписка ждёт активации: привяжите роутер, и отсчёт начнётся.")
    return "\n".join(lines)


def _plural_days(days: int) -> str:
    if days % 10 == 1 and days % 100 != 11:
        return f"{days} день"
    if days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14):
        return f"{days} дня"
    return f"{days} дней"


def fleet_digest(
    *,
    silent: list[tuple[str, str, str]],
    shipped_silent: list[tuple[str, str, int]],
    expiring: list[tuple[str, str, int]],
) -> str:
    """Сводка оператору: что в парке требует внимания.

    Каждая строка — повод кому-то позвонить, поэтому в ней есть за что
    зацепиться: MAC, имя клиента, номер заказа. Списки режутся: сводка
    на тридцать строк не читается, а первые десять отвечают на вопрос
    «стало хуже или как вчера».
    """
    limit = 10
    blocks: list[str] = ["🛠 <b>Парк роутеров: на что посмотреть</b>"]

    if silent:
        blocks.append(f"\n<b>Молчат больше суток</b> — {len(silent)}")
        for mac, client, since in silent[:limit]:
            who = f" · {client}" if client else ""
            blocks.append(f"<code>{mac}</code>{who} — с {since}")
        if len(silent) > limit:
            blocks.append(f"…и ещё {len(silent) - limit}")

    if shipped_silent:
        blocks.append(f"\n<b>Отгружены, но не включались</b> — {len(shipped_silent)}")
        for number, mac, days in shipped_silent[:limit]:
            blocks.append(f"Заказ <b>{number}</b> · <code>{mac}</code> — {_plural_days(days)} в пути")
        if len(shipped_silent) > limit:
            blocks.append(f"…и ещё {len(shipped_silent) - limit}")

    if expiring:
        blocks.append(f"\n<b>Подписка кончается, продления нет</b> — {len(expiring)}")
        for client, until, days in expiring[:limit]:
            blocks.append(f"{client} — до {until} ({_plural_days(days)})")
        if len(expiring) > limit:
            blocks.append(f"…и ещё {len(expiring) - limit}")

    return "\n".join(blocks)
