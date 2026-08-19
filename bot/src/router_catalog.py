"""Каталог роутеров и оформление заказа в боте.

Поток:
    [Главное меню] «🛒 Купить роутер»
        → shop_catalog          — список моделей
        → shop_item:{id}        — карточка: описание, характеристики, цена
        → shop_buy:{id}         — оформление: ФИО, телефон, город,
                                  доставка, промокод
        → shop_confirm          — суммы с расчёта основного приложения
                                  и ссылка на оплату
    [Главное меню] «📦 Мои заказы»
        → shop_orders / shop_order:{id} / shop_order_cancel:{id}

Товары, цены, промокоды и сам заказ живут в основном приложении: у него
таблица `products`, расчёт сумм со снимком цен и приёмник оплаты. Здесь
только экраны и сбор ответов — второй каталог в базе бота развёл бы цены
по двум местам. Всё общение через `src/shop_api.py`.

Проверка ФИО, телефона и адреса тоже там: правила одни на бота и на админку,
и разъехавшись, они пропустили бы телефон, на который не дозвонится курьер.
"""

from __future__ import annotations

import asyncio
import html
from decimal import Decimal, InvalidOperation

from aiogram import Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from app_config import app_conf
from button_helpers import btn
from src import shop_api
from src.shop_texts import CATALOG_TEXTS

_DEFAULT_TEXTS = {key: value for key, value, _ in CATALOG_TEXTS}

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

STATUS_LABELS = {
    "new": "🆕 Новый",
    "awaiting_payment": "⏳ Ждёт оплаты",
    "paid": "✅ Оплачен",
    "packing": "📦 Собираем",
    "shipped": "🚚 Отправлен",
    "delivered": "📬 Доставлен",
    "done": "✅ Завершён",
    "cancelled": "❌ Отменён",
    "refunded": "↩️ Возврат",
}

CARRIER_TITLES = {"cdek": "СДЭК", "post": "Почта России", "yandex": "Яндекс Go"}


class RouterOrder(StatesGroup):
    """Шаги оформления. Ответы копятся в данных состояния и уезжают
    в основное приложение одним запросом на последнем шаге."""

    name = State()
    phone = State()
    city = State()
    address = State()
    promo = State()


async def render_renew(query: CallbackQuery) -> None:
    """Экран продления. На уровне модуля, чтобы его мог позвать и `main.py`.

    Продление в боте одно и наше: родное двигает срок у учётки `tg{id}` —
    подписки для приложения на телефоне, — а роутеру доступ выдан учётке
    `tg{id}_{mac}`. Клиент заплатил бы, а срок на роутере не сдвинулся.
    """
    data, error = await shop_api.renew_state(query.from_user.id)
    if error:
        logger.warning(f"[CATALOG] {error}")
        await edit_screen(
            query.message,
            text("text_catalog_unavailable") + f"\n\n<i>{_esc(error)}</i>",
            reply_markup=InlineKeyboardBuilder()
            .row(btn("btn_back_to_main", callback_data="back_to_main"))
            .as_markup(),
        )
        return await query.answer()

    periods = data.get("plans", [])
    if not periods:
        return await query.answer(text("text_order_no_plans"), show_alert=True)
    await edit_screen(
        query.message,
        renew_text(data),
        reply_markup=renew_keyboard(periods),
        link_preview_options=NO_PREVIEW,
    )
    await query.answer()


def catalog_enabled() -> bool:
    return str(app_conf.get("catalog_enabled", "1")) == "1"


def text(key: str) -> str:
    return app_conf.get(key, _DEFAULT_TEXTS.get(key, key))


def _esc(value) -> str:
    """Экранируем всё, что пришло из каталога: одна угловая скобка в описании
    роняет отправку целиком — Telegram разбирает текст как HTML."""
    return html.escape(str(value or ""), quote=False)


def money(raw) -> str:
    """«6900.00» → «6 900 ₽». Копейки показываем, только если они есть."""
    try:
        value = Decimal(str(raw or "0"))
    except (InvalidOperation, ValueError):
        return f"{raw} ₽"
    if value == value.to_integral_value():
        body = f"{int(value):,}".replace(",", " ")
    else:
        # Тысячи отделяем пробелом, копейки — запятой: 6 900,50 ₽.
        body = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{body} ₽"


def is_positive(raw) -> bool:
    """Ноль приходит строкой «0.00» — сравнивать её с нулём как текст нельзя."""
    try:
        return Decimal(str(raw or "0")) > 0
    except (InvalidOperation, ValueError):
        return False


def stock_line(product: dict) -> str:
    if product.get("stock", 0) > 0:
        return "✅ В наличии"
    if product.get("allow_preorder"):
        return "📦 Под заказ"
    return "⛔️ Нет в наличии"


async def edit_screen(message, text, *, reply_markup=None, link_preview_options=NO_PREVIEW):
    """Правит экран на месте.

    Повторное нажатие той же кнопки даёт тот же текст, и Telegram отвечает
    «message is not modified». Это не поломка, а щелчок мимо: экран уже такой,
    какой просили, — молчим. Остальные отказы пропускаем наверх.
    """
    try:
        await message.edit_text(
            text, reply_markup=reply_markup, link_preview_options=link_preview_options
        )
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


# --- Экраны каталога ---------------------------------------------------------


def catalog_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for product in products:
        builder.row(
            btn(
                "btn_shop_item",
                text=f"{product.get('title', '')} · {money(product.get('price'))}",
                callback_data=f"shop_item:{product.get('id')}",
            )
        )
    builder.row(btn("btn_my_orders", callback_data="shop_orders"))
    builder.row(btn("btn_back_to_main", callback_data="back_to_main"))
    return builder.as_markup()


def card_text(product: dict) -> str:
    lines = [f"<b>{_esc(product.get('title'))}</b>"]
    if product.get("subtitle"):
        lines.append(_esc(product["subtitle"]))
    if product.get("description"):
        lines += ["", _esc(product["description"])]

    specs = product.get("specs") or {}
    if specs:
        limit = max(int(app_conf.get("catalog_specs_limit", 8) or 8), 1)
        lines += ["", "<b>Характеристики</b>"]
        for name, value in list(specs.items())[:limit]:
            lines.append(f"• {_esc(name)}: {_esc(value)}")

    price = f"💰 <b>{money(product.get('price'))}</b>"
    if product.get("old_price"):
        price += f"  <s>{money(product['old_price'])}</s>"
    lines += ["", price, stock_line(product)]
    return "\n".join(lines)


def card_keyboard(product: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if product.get("in_stock"):
        builder.row(btn("btn_shop_buy", callback_data=f"shop_buy:{product.get('id')}"))
    builder.row(btn("btn_shop_back_to_list", callback_data="shop_catalog"))
    builder.row(btn("btn_back_to_main", callback_data="back_to_main"))
    return builder.as_markup()


def card_preview(product: dict) -> LinkPreviewOptions:
    """Фото показываем ссылкой на картинку, а не отдельным сообщением:
    экран правится на месте, а превратить текст в фото Telegram не даёт."""
    photo = product.get("photo_url") or ""
    if not photo:
        return NO_PREVIEW
    return LinkPreviewOptions(url=photo, prefer_large_media=True, show_above_text=True)


def period_text(plan: dict) -> str:
    """«12 мес + 30 дней» — срок словами, как его выбирает клиент."""
    parts = []
    months = int(plan.get("months") or 0)
    days = int(plan.get("extra_days") or 0)
    if months:
        parts.append(f"{months} мес.")
    if days:
        parts.append(f"+{days} дн.")
    return " ".join(parts) or plan.get("title", "")


def plans_keyboard(plans: list[dict], product_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for plan in plans:
        builder.row(
            btn(
                "btn_shop_plan",
                text=f"{plan.get('title', '')} · {money(plan.get('price'))}",
                callback_data=f"shop_plan:{product_id}:{plan.get('id')}",
            )
        )
    builder.row(btn("btn_shop_back_to_list", callback_data=f"shop_item:{product_id}"))
    builder.row(btn("btn_back_to_main", callback_data="back_to_main"))
    return builder.as_markup()


def cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(btn("btn_shop_cancel_order", callback_data="shop_cancel"))
    return builder.as_markup()


# --- Экраны заказа -----------------------------------------------------------


def carrier_keyboard(options: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for option in options:
        method = option.get("method", "")
        title = option.get("title") or CARRIER_TITLES.get(method, method)
        days = f" · {option['days']}" if option.get("days") else ""
        builder.row(
            btn("btn_shop_carrier", text=f"{title}{days}", callback_data=f"shop_carrier:{method}")
        )
    builder.row(btn("btn_shop_cancel_order", callback_data="shop_cancel"))
    return builder.as_markup()


def where_keyboard(method: str, option: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        btn(
            "btn_shop_to_pvz",
            text=f"🏬 В пункт выдачи · {money(option.get('pvz_price'))}",
            callback_data=f"shop_where:{method}:pvz",
        )
    )
    builder.row(
        btn(
            "btn_shop_to_door",
            text=f"🚪 Курьером до двери · {money(option.get('courier_price'))}",
            callback_data=f"shop_where:{method}:door",
        )
    )
    builder.row(btn("btn_shop_back_to_carrier", callback_data="shop_carriers"))
    return builder.as_markup()


def renew_text(state: dict) -> str:
    subscription = state.get("subscription") or {}
    lines = ["🔄 <b>Продление подписки</b>", ""]
    until = subscription.get("until")
    if until:
        lines.append(f"Сейчас оплачено до {human_date(until)}.")
        lines.append("Новый срок прибавится к этой дате, а не начнётся с сегодня.")
    else:
        lines.append("Срок ещё не идёт — он начнётся, когда роутер первый раз выйдет на связь.")
    lines += ["", "Выберите, на сколько продлить:"]
    return "\n".join(lines)


def renew_keyboard(plans: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for plan in plans:
        builder.row(
            btn(
                "btn_shop_renew_plan",
                text=f"{plan.get('title', '')} · {money(plan.get('price'))}",
                callback_data=f"shop_renew_plan:{plan.get('id')}",
            )
        )
    builder.row(btn("btn_back_to_main", callback_data="back_to_main"))
    return builder.as_markup()


def promo_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(btn("btn_shop_promo_skip", callback_data="shop_promo_skip"))
    builder.row(btn("btn_shop_cancel_order", callback_data="shop_cancel"))
    return builder.as_markup()


def confirm_text(quote: dict, data: dict) -> str:
    product = quote.get("product") or {}
    lines = [
        "🧾 <b>Проверьте заказ</b>",
        "",
        # Цена самой модели, а не общая сумма: срок идёт отдельной строкой ниже,
        # и одинаковые числа рядом читались бы как ошибка счёта.
        f"<b>{_esc(product.get('title'))}</b> — {money(product.get('price'))}",
    ]
    plan = quote.get("plan") or {}
    if plan:
        lines.append(f"Подписка: {_esc(plan.get('title'))} — {money(plan.get('price'))}")
    if quote.get("promo"):
        lines.append(f"Промокод {_esc(quote['promo'].get('code'))} — −{money(quote.get('discount'))}")
    if data.get("delivery_method"):
        carrier = CARRIER_TITLES.get(data["delivery_method"], data["delivery_method"])
        where = "пункт выдачи" if data.get("delivery_to_pvz") else "курьером"
        price = money(quote.get("delivery")) if is_positive(quote.get("delivery")) else "бесплатно"
        lines.append(f"Доставка {_esc(carrier)}, {where} — {price}")
    lines += [
        "",
        f"<b>Итого: {money(quote.get('total'))}</b>",
        "",
        f"Получатель: {_esc(data.get('name'))}",
        f"Телефон: {_esc(data.get('phone'))}",
        f"Адрес: {_esc(data.get('city'))}, {_esc(data.get('address'))}",
    ]
    return "\n".join(lines)


def confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(btn("btn_shop_confirm", callback_data="shop_confirm"))
    builder.row(btn("btn_shop_cancel_order", callback_data="shop_cancel"))
    return builder.as_markup()


def created_text(order: dict, pay_url: str) -> str:
    lines = [
        f"✅ <b>Заказ {_esc(order.get('number'))} принят</b>",
        "",
        f"Сумма: <b>{money(order.get('total'))}</b>",
        f"Доставка: {_esc(order.get('delivery_summary'))}",
        "",
        text("text_order_pay_hint") if pay_url else text("text_order_pay_later"),
    ]
    return "\n".join(lines)


def created_keyboard(pay_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if pay_url:
        builder.row(btn("btn_payment_pay_link", url=pay_url))
    builder.row(btn("btn_my_orders", callback_data="shop_orders"))
    builder.row(btn("btn_back_to_main", callback_data="back_to_main"))
    return builder.as_markup()


# --- Экран «Мой роутер» ------------------------------------------------------


def human_bytes(value) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "0 Б"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if number < 1024 or unit == "ТБ":
            return f"{number:.0f} {unit}" if unit in ("Б", "КБ") else f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} ТБ"


def human_uptime(seconds) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return "—"
    if total < 60:
        return "меньше минуты"
    hours, minutes = divmod(total // 60, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} дн. {hours} ч."
    return f"{hours} ч. {minutes} мин." if hours else f"{minutes} мин."


def human_date(iso: str | None) -> str:
    """Дата из ISO без разбора часовых поясов: показываем как есть, до дня."""
    if not iso:
        return "—"
    head = str(iso)[:10]
    parts = head.split("-")
    return f"{parts[2]}.{parts[1]}.{parts[0]}" if len(parts) == 3 else head


def my_router_text(data: dict) -> str:
    router = data.get("router")
    order = data.get("order")

    if router is None:
        lines = [text("text_my_router_none")]
        if order:
            status = STATUS_LABELS.get(order.get("status", ""), order.get("status", ""))
            lines += ["", f"Заказ <b>{_esc(order.get('number'))}</b> — {status}"]
            if order.get("tracking_number"):
                lines.append(f"Трек-номер: <code>{_esc(order['tracking_number'])}</code>")
        return "\n".join(lines)

    if not router.get("activated"):
        return text("text_my_router_waiting")

    routers = data.get("routers") or []
    heading = "📡 <b>Мой роутер</b>"
    if len(routers) > 1:
        # Который из. Без этого на экране два одинаковых заголовка, и понять,
        # чьи показания перед тобой, можно только по MAC ниже.
        position = next(
            (i + 1 for i, item in enumerate(routers) if item.get("id") == router.get("id")), 1
        )
        heading = f"📡 <b>Мой роутер {position} из {len(routers)}</b>"

    lines = [
        heading,
        "",
        f"Модель: {_esc(router.get('model') or '—')}",
        f"MAC: <code>{_esc(router.get('mac'))}</code>",
        f"Связь: {'✅ на связи' if router.get('online') else '○ не отвечает'}",
    ]
    if router.get("online"):
        lines += [
            f"Устройств в сети: {router.get('clients', 0)}",
            f"Работает без перезагрузки: {human_uptime(router.get('uptime_sec'))}",
            f"Трафик: ↓ {human_bytes(router.get('rx_bytes'))} · ↑ {human_bytes(router.get('tx_bytes'))}",
        ]
    lines += [""]
    if router.get("active"):
        lines.append(f"Подписка активна до <b>{human_date(router.get('until'))}</b>")
    elif router.get("until"):
        lines.append(f"⚠️ Подписка закончилась {human_date(router.get('until'))}")
    else:
        lines.append("Подписка настраивается — загляните через пару минут.")
    # Адрес пишем и текстом: часть клиентов Telegram не открывает ссылки
    # на адреса домашней сети, а скопировать строку можно всегда.
    panel_url = (app_conf.get("router_panel_url", "") or "").strip()
    if panel_url:
        lines += ["", f"Админка роутера: <code>{_esc(panel_url)}</code> — из домашней сети."]
    return "\n".join(lines)


def my_router_keyboard(data: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    routers = data.get("routers") or []
    current = (data.get("router") or {}).get("id")

    builder.row(
        btn(
            "btn_my_router_refresh",
            callback_data=f"shop_my_router:{current}" if current else "shop_my_router",
        )
    )

    # Переключатель — только когда роутеров правда несколько: у большинства
    # клиентов он один, и лишний ряд кнопок им ни о чём не говорит.
    if len(routers) > 1:
        for item in routers:
            if item.get("id") == current:
                continue
            mark = "🟢" if item.get("online") else "○"
            label = item.get("model") or item.get("mac", "")
            builder.row(
                btn(
                    "btn_my_router_switch",
                    text=f"{mark} {label}",
                    callback_data=f"shop_my_router:{item.get('id')}",
                )
            )

    if data.get("router") is None:
        builder.row(btn("btn_catalog", callback_data="shop_catalog"))
    else:
        # Продлевать приходят сюда чаще, чем в главное меню: тут виден срок.
        builder.row(btn("btn_renew_sub", callback_data="shop_renew"))
        # Админка роутера — адрес в домашней сети клиента, снаружи он не
        # открывается. Ссылкой, а не кнопкой с обработчиком: открывать её
        # должен браузер клиента, мы до этой сети не дотягиваемся.
        panel_url = (app_conf.get("router_panel_url", "") or "").strip()
        if panel_url:
            builder.row(btn("btn_router_panel", url=panel_url))
    builder.row(btn("btn_my_orders", callback_data="shop_orders"))
    builder.row(btn("btn_back_to_main", callback_data="back_to_main"))
    return builder.as_markup()


def orders_text(orders: list[dict]) -> str:
    lines = ["📦 <b>Мои заказы</b>", ""]
    for order in orders:
        status = STATUS_LABELS.get(order.get("status", ""), order.get("status", ""))
        lines.append(f"<b>{_esc(order.get('number'))}</b> — {money(order.get('total'))} · {status}")
    return "\n".join(lines)


def orders_keyboard(orders: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for order in orders:
        builder.row(
            btn(
                "btn_shop_order",
                text=f"{order.get('number')} · {money(order.get('total'))}",
                callback_data=f"shop_order:{order.get('id')}",
            )
        )
    builder.row(btn("btn_catalog", callback_data="shop_catalog"))
    builder.row(btn("btn_back_to_main", callback_data="back_to_main"))
    return builder.as_markup()


def order_text(order: dict) -> str:
    status = STATUS_LABELS.get(order.get("status", ""), order.get("status", ""))
    lines = [f"📦 <b>Заказ {_esc(order.get('number'))}</b>", "", f"Состояние: {status}", ""]
    for item in order.get("items", []):
        lines.append(f"• {_esc(item.get('title'))} — {money(item.get('total'))}")
    if is_positive(order.get("discount")):
        lines.append(f"Скидка: −{money(order['discount'])}")
    lines += ["", f"<b>Итого: {money(order.get('total'))}</b>"]
    if order.get("delivery_summary") and order["delivery_summary"] != "—":
        lines.append(f"Доставка: {_esc(order['delivery_summary'])}")
    if order.get("tracking_number"):
        lines.append(f"Трек-номер: <code>{_esc(order['tracking_number'])}</code>")
    return "\n".join(lines)


def order_keyboard(order: dict, cancellable: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if cancellable:
        builder.row(
            btn("btn_shop_order_cancel", callback_data=f"shop_order_cancel:{order.get('id')}")
        )
    builder.row(btn("btn_shop_back_to_orders", callback_data="shop_orders"))
    builder.row(btn("btn_back_to_main", callback_data="back_to_main"))
    return builder.as_markup()


def remember_client(user) -> None:
    """Отмечает клиента в основном приложении при входе в бот.

    Фоном и без ожидания: роутер привязывает оператор по MAC при отгрузке,
    и строка клиента нужна там раньше заказа — но экран `/start` не должен
    зависеть от чужого сервиса. Не отметился сейчас — отметится на первом
    заходе в каталог или «Мой роутер».
    """

    async def run() -> None:
        try:
            _, error = await shop_api.register_client(
                user.id, user.username or "", user.first_name or ""
            )
            if error:
                logger.warning(f"[CATALOG] клиент {user.id} не отметился: {error}")
        except Exception as exc:  # noqa: BLE001 — вход в бот важнее нашей отметки
            logger.warning(f"[CATALOG] клиент {user.id} не отметился: {exc}")

    asyncio.create_task(run())


def draft_payload(user, data: dict) -> dict:
    """Черновик заказа в том виде, в каком его ждёт основное приложение."""
    return {
        "tg_id": user.id,
        "username": user.username or "",
        "first_name": user.first_name or "",
        "product_id": data.get("product_id"),
        "plan_id": data.get("plan_id"),
        "name": data.get("name", ""),
        "phone": data.get("phone", ""),
        "city": data.get("city", ""),
        "delivery_method": data.get("delivery_method", ""),
        "delivery_to_pvz": bool(data.get("delivery_to_pvz", True)),
        "address": data.get("address", ""),
        "promo_code": data.get("promo_code", ""),
    }


# --- Регистрация -------------------------------------------------------------


def register_router_catalog_handlers(dp: Dispatcher, check_user_blocked_func, send_blocked_message_func):
    """Обработчики каталога.

    Args:
        dp: Dispatcher для регистрации обработчиков
        check_user_blocked_func: Функция проверки блокировки пользователя
        send_blocked_message_func: Функция отправки сообщения о блокировке
    """

    async def blocked(query_or_message, is_query: bool = True) -> bool:
        user_id = query_or_message.from_user.id
        if not await check_user_blocked_func(user_id):
            return False
        await send_blocked_message_func(user_id, query_or_message if is_query else None)
        return True

    async def show_error(query: CallbackQuery, error: str):
        logger.warning(f"[CATALOG] {error}")
        await edit_screen(
            query.message,
            f"{text('text_catalog_unavailable')}\n\n<i>{_esc(error)}</i>",
            reply_markup=InlineKeyboardBuilder()
            .row(btn("btn_back_to_main", callback_data="back_to_main"))
            .as_markup(),
            link_preview_options=NO_PREVIEW,
        )
        await query.answer()

    async def show_catalog(query: CallbackQuery):
        products, error = await shop_api.products()
        if error:
            return await show_error(query, error)
        if not products:
            await edit_screen(
                query.message,
                text("text_catalog_empty"),
                reply_markup=InlineKeyboardBuilder()
                .row(btn("btn_back_to_main", callback_data="back_to_main"))
                .as_markup(),
                link_preview_options=NO_PREVIEW,
            )
            return await query.answer()
        await edit_screen(
            query.message,
            text("text_catalog_intro"),
            reply_markup=catalog_keyboard(products),
            link_preview_options=NO_PREVIEW,
        )
        await query.answer()

    @dp.callback_query(F.data == "shop_catalog")
    async def cq_catalog(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        await state.clear()
        await show_catalog(query)

    @dp.callback_query(F.data.startswith("shop_item:"))
    async def cq_item(query: CallbackQuery):
        if await blocked(query):
            return
        product_id = query.data.split(":")[1]
        product, error = await shop_api.product(int(product_id))
        if error:
            return await show_error(query, error)
        await edit_screen(
            query.message,
            card_text(product),
            reply_markup=card_keyboard(product),
            link_preview_options=card_preview(product),
        )
        await query.answer()

    @dp.callback_query(F.data.startswith("shop_buy:"))
    async def cq_buy(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        product_id = int(query.data.split(":")[1])
        product, error = await shop_api.product(product_id)
        if error:
            return await show_error(query, error)
        if not product.get("in_stock"):
            return await query.answer("Этой модели сейчас нет в наличии", show_alert=True)

        await state.clear()
        await state.update_data(product_id=product_id, product_title=product.get("title", ""))

        # Срок выбирается вместе с роутером: от него зависит и цена, и то,
        # на сколько включится подписка, когда роутер доедет.
        periods, error = await shop_api.plans()
        if error:
            return await show_error(query, error)
        if not periods:
            return await query.answer(text("text_order_no_plans"), show_alert=True)

        await edit_screen(
            query.message,
            text("text_order_ask_plan"),
            reply_markup=plans_keyboard(periods, product_id),
            link_preview_options=NO_PREVIEW,
        )
        await query.answer()

    @dp.callback_query(F.data.startswith("shop_plan:"))
    async def cq_plan(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        _, product_id, plan_id = query.data.split(":")
        await state.update_data(product_id=int(product_id), plan_id=int(plan_id))
        await state.set_state(RouterOrder.name)
        await edit_screen(
            query.message,
            text("text_order_ask_name"),
            reply_markup=cancel_keyboard(),
            link_preview_options=NO_PREVIEW,
        )
        await query.answer()

    async def ask_carrier(target, state: FSMContext, *, edit: bool):
        """Экран выбора перевозчика с ценами для города клиента.

        Способов может не быть вовсе — тогда заказ оформляется без доставки,
        а не упирается в пустой список. А вот незнакомый город оформление
        останавливает: см. ниже.
        """
        saved = await state.get_data()
        user_id = getattr(getattr(target, "from_user", None), "id", 0)
        data, error = await shop_api.delivery_options(saved.get("city", ""), user_id)

        # Города нет ни в одной зоне. Цену наугад не называем: промахнёшься вверх —
        # отпугнёшь клиента, вниз — повезёшь через полстраны себе в убыток.
        # Основное приложение уже записало город оператору, он посчитает руками.
        if data.get("unknown_city"):
            await state.clear()
            payload = {
                "text": text("text_order_unknown_city"),
                "reply_markup": InlineKeyboardBuilder()
                .row(btn("btn_back_to_main", callback_data="back_to_main"))
                .as_markup(),
                "link_preview_options": NO_PREVIEW,
            }
            if edit:
                await edit_screen(target.message, **payload)
            else:
                await target.answer(**payload)
            return None

        options = data.get("options", [])
        if error or not options:
            if error:
                logger.warning(f"[CATALOG] доставка недоступна: {error}")
            await state.update_data(delivery_method="", address="")
            return await ask_promo(target, state, edit=edit)

        await state.set_state(None)
        payload = {
            "text": text("text_order_ask_carrier"),
            "reply_markup": carrier_keyboard(options),
            "link_preview_options": NO_PREVIEW,
        }
        if edit:
            await edit_screen(target.message, **payload)
        else:
            await target.answer(**payload)

    async def ask_promo(target, state: FSMContext, *, edit: bool):
        await state.set_state(RouterOrder.promo)
        payload = {
            "text": text("text_order_ask_promo"),
            "reply_markup": promo_keyboard(),
            "link_preview_options": NO_PREVIEW,
        }
        if edit:
            await edit_screen(target.message, **payload)
        else:
            await target.answer(**payload)

    async def show_confirm(target, state: FSMContext, user, *, edit: bool):
        data = await state.get_data()
        quote, error = await shop_api.quote(draft_payload(user, data))
        if error:
            payload = {
                "text": f"❌ {_esc(error)}",
                "reply_markup": cancel_keyboard(),
                "link_preview_options": NO_PREVIEW,
            }
        else:
            payload = {
                "text": confirm_text(quote, data),
                "reply_markup": confirm_keyboard(),
                "link_preview_options": NO_PREVIEW,
            }
        await state.set_state(None)
        if edit:
            await edit_screen(target.message, **payload)
        else:
            await target.answer(**payload)

    async def check_field(message: Message, field: str) -> str:
        """Проверка значения на стороне основного приложения. Пустой ответ —
        клиенту уже объяснили, что не так, и шаг остался прежним."""
        value, error = await shop_api.validate_field(field, message.text or "")
        if error:
            await message.answer(f"❌ {_esc(error)}", reply_markup=cancel_keyboard())
            return ""
        return value

    @dp.message(RouterOrder.name)
    async def on_name(message: Message, state: FSMContext):
        if await blocked(message, is_query=False):
            await state.clear()
            return
        value = await check_field(message, "name")
        if not value:
            return
        await state.update_data(name=value)
        await state.set_state(RouterOrder.phone)
        await message.answer(text("text_order_ask_phone"), reply_markup=cancel_keyboard())

    @dp.message(RouterOrder.phone)
    async def on_phone(message: Message, state: FSMContext):
        if await blocked(message, is_query=False):
            await state.clear()
            return
        value = await check_field(message, "phone")
        if not value:
            return
        await state.update_data(phone=value)
        await state.set_state(RouterOrder.city)
        await message.answer(text("text_order_ask_city"), reply_markup=cancel_keyboard())

    @dp.message(RouterOrder.city)
    async def on_city(message: Message, state: FSMContext):
        if await blocked(message, is_query=False):
            await state.clear()
            return
        value = await check_field(message, "city")
        if not value:
            return
        await state.update_data(city=value)
        await ask_carrier(message, state, edit=False)

    @dp.callback_query(F.data == "shop_carriers")
    async def cq_carriers(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        await ask_carrier(query, state, edit=True)
        await query.answer()

    @dp.callback_query(F.data.startswith("shop_carrier:"))
    async def cq_carrier(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        method = query.data.split(":")[1]
        data, error = await shop_api.delivery_options(
            (await state.get_data()).get("city", ""), query.from_user.id
        )
        if error:
            return await show_error(query, error)
        if data.get("unknown_city"):
            # Город убрали из зоны, пока клиент выбирал перевозчика.
            return await ask_carrier(query, state, edit=True)
        option = next((o for o in data.get("options", []) if o.get("method") == method), None)
        if option is None:
            return await query.answer("Этот способ доставки выключен", show_alert=True)

        await edit_screen(
            query.message,
            text("text_order_ask_where"),
            reply_markup=where_keyboard(method, option),
            link_preview_options=NO_PREVIEW,
        )
        await query.answer()

    @dp.callback_query(F.data.startswith("shop_where:"))
    async def cq_where(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        _, method, where = query.data.split(":")
        to_pvz = where == "pvz"
        await state.update_data(delivery_method=method, delivery_to_pvz=to_pvz)
        await state.set_state(RouterOrder.address)
        await edit_screen(
            query.message,
            text("text_order_ask_pvz") if to_pvz else text("text_order_ask_address"),
            reply_markup=cancel_keyboard(),
            link_preview_options=NO_PREVIEW,
        )
        await query.answer()

    @dp.message(RouterOrder.address)
    async def on_address(message: Message, state: FSMContext):
        if await blocked(message, is_query=False):
            await state.clear()
            return
        data = await state.get_data()
        value = await check_field(message, "pvz" if data.get("delivery_to_pvz") else "address")
        if not value:
            return
        await state.update_data(address=value)
        await ask_promo(message, state, edit=False)

    @dp.message(RouterOrder.promo)
    async def on_promo(message: Message, state: FSMContext):
        if await blocked(message, is_query=False):
            await state.clear()
            return
        await state.update_data(promo_code=(message.text or "").strip())
        await show_confirm(message, state, message.from_user, edit=False)

    @dp.callback_query(F.data == "shop_promo_skip")
    async def cq_promo_skip(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        await state.update_data(promo_code="")
        await show_confirm(query, state, query.from_user, edit=True)
        await query.answer()

    @dp.callback_query(F.data == "shop_confirm")
    async def cq_confirm(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        data = await state.get_data()
        if not data.get("product_id"):
            return await query.answer("Заказ уже оформлен", show_alert=True)

        await query.answer("Оформляем…")
        result, error = await shop_api.create_order(draft_payload(query.from_user, data))
        if error:
            return await edit_screen(
                query.message,
                f"❌ {_esc(error)}",
                reply_markup=cancel_keyboard(),
                link_preview_options=NO_PREVIEW,
            )

        await state.clear()
        order = result.get("order", {})
        pay_url = result.get("pay_url", "")
        if result.get("payment_error"):
            logger.warning(f"[CATALOG] заказ {order.get('number')} без оплаты: {result['payment_error']}")
        logger.info(f"[CATALOG] заказ {order.get('number')} от {query.from_user.id}")
        await edit_screen(
            query.message,
            created_text(order, pay_url),
            reply_markup=created_keyboard(pay_url),
            link_preview_options=NO_PREVIEW,
        )

    @dp.callback_query(F.data == "shop_cancel")
    async def cq_cancel(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        await state.clear()
        await query.answer(text("text_order_cancelled"))
        await show_catalog(query)

    @dp.callback_query(F.data.startswith("shop_my_router"))
    async def cq_my_router(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        await state.clear()
        # Заодно отмечаем клиента: роутер привязывает оператор по MAC, и строка
        # в базе должна существовать до отгрузки, а не появляться с заказом.
        await shop_api.register_client(
            query.from_user.id, query.from_user.username or "", query.from_user.first_name or ""
        )
        # `shop_my_router:{id}` — выбор роутера, когда их у клиента несколько.
        # Без номера — первый по списку, как было до появления второго.
        device_id = 0
        if ":" in query.data:
            try:
                device_id = int(query.data.split(":", 1)[1])
            except ValueError:
                device_id = 0

        data, error = await shop_api.my_router(query.from_user.id, device_id)
        if error:
            return await show_error(query, error)
        await edit_screen(
            query.message,
            my_router_text(data),
            reply_markup=my_router_keyboard(data),
            link_preview_options=NO_PREVIEW,
        )
        await query.answer()

    @dp.callback_query(F.data == "shop_renew")
    async def cq_renew(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        await state.clear()
        await render_renew(query)

    @dp.callback_query(F.data.startswith("shop_renew_plan:"))
    async def cq_renew_plan(query: CallbackQuery):
        if await blocked(query):
            return
        plan_id = int(query.data.split(":")[1])
        await query.answer("Готовим оплату…")
        result, error = await shop_api.renew_start(query.from_user.id, plan_id)
        if error:
            return await edit_screen(
                query.message,
                f"❌ {_esc(error)}",
                reply_markup=created_keyboard(""),
                link_preview_options=NO_PREVIEW,
            )

        plan = result.get("plan") or {}
        pay_url = result.get("pay_url", "")
        logger.info(f"[CATALOG] продление {plan.get('title')} для {query.from_user.id}")
        await edit_screen(
            query.message,
            "🔄 <b>Продление на "
            + _esc(plan.get("title"))
            + "</b>\n\n"
            + (
                text("text_order_pay_hint")
                if pay_url
                else text("text_order_pay_later")
            ),
            reply_markup=created_keyboard(pay_url),
            link_preview_options=NO_PREVIEW,
        )

    @dp.callback_query(F.data == "shop_orders")
    async def cq_orders(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        await state.clear()
        orders, error = await shop_api.orders_of(query.from_user.id)
        if error:
            return await show_error(query, error)
        if not orders:
            await edit_screen(
                query.message,
                text("text_orders_empty"),
                reply_markup=orders_keyboard([]),
                link_preview_options=NO_PREVIEW,
            )
            return await query.answer()
        await edit_screen(
            query.message,
            orders_text(orders),
            reply_markup=orders_keyboard(orders),
            link_preview_options=NO_PREVIEW,
        )
        await query.answer()

    @dp.callback_query(F.data.startswith("shop_order:"))
    async def cq_order(query: CallbackQuery):
        if await blocked(query):
            return
        order_id = int(query.data.split(":")[1])
        data, error = await shop_api.order_card(order_id, query.from_user.id)
        if error:
            return await show_error(query, error)
        order = data.get("order", {})
        await edit_screen(
            query.message,
            order_text(order),
            reply_markup=order_keyboard(order, bool(data.get("cancellable"))),
            link_preview_options=NO_PREVIEW,
        )
        await query.answer()

    @dp.callback_query(F.data.startswith("shop_order_cancel:"))
    async def cq_order_cancel(query: CallbackQuery):
        if await blocked(query):
            return
        order_id = int(query.data.split(":")[1])
        _, error = await shop_api.cancel_order(order_id, query.from_user.id)
        if error:
            return await query.answer(error[:190], show_alert=True)

        await query.answer("Заказ отменён")
        data, error = await shop_api.order_card(order_id, query.from_user.id)
        if error:
            return await show_error(query, error)
        order = data.get("order", {})
        await edit_screen(
            query.message,
            order_text(order),
            reply_markup=order_keyboard(order, bool(data.get("cancellable"))),
            link_preview_options=NO_PREVIEW,
        )

    logger.info("[CATALOG] обработчики каталога роутеров зарегистрированы")
