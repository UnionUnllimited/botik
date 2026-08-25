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
    "new": "• Новый",
    "awaiting_payment": "○ Ждёт оплаты",
    "paid": "✓ Оплачен",
    "packing": "▸ Собираем",
    "shipped": "▸ В пути",
    "delivered": "✓ Доставлен",
    "activated": "✓ Роутер работает",
    "done": "✓ Завершён",
    "cancelled": "✕ Отменён",
    "refunded": "↩ Возврат",
}

HIDDEN_ORDER_STATUSES = ("cancelled", "refunded")
"""Свёрнутые статусы в списке заказов: отменённое не должно засорять
живые заказы, но достаётся кнопкой «Показать отменённые»."""

SPEED_TITLES = {"fast": "быстрая", "weekly": "раз в неделю"}
"""Короткие названия для строки подтверждения. Полные названия и описания
приходят с данными: их правит оператор, а не переписывает бот."""


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


def format_text(key: str, **values) -> str:
    """Шаблон из настроек правит оператор, и `{unknown}` или `{date:bogus}`
    в нём — вопрос времени. Экран из-за этого падать не должен: сломанный
    шаблон откатываем на дефолтный."""
    try:
        return text(key).format(**values)
    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
        logger.warning(f"[CATALOG] настройка {key}: шаблон не форматируется, взят дефолт")
        return _DEFAULT_TEXTS.get(key, key).format(**values)


def _mapping_setting(key: str) -> dict[str, str]:
    """Настройка-словарь: по строке «код: значение». Формат тот же, что у
    характеристик в админке, — его пишут люди, и JSON тут ронял бы экран
    из-за одной скобки."""
    result: dict[str, str] = {}
    for line in str(app_conf.get(key, "") or "").splitlines():
        code, sep, value = line.partition(":")
        if sep and code.strip() and value.strip():
            result[code.strip()] = value.strip()
    return result


def status_label(code: str) -> str:
    """Подпись статуса заказа: настройка `order_status_labels` поверх дефолтов."""
    overrides = _mapping_setting("order_status_labels")
    return overrides.get(code) or STATUS_LABELS.get(code, code)


def status_glyph(code: str) -> str:
    """Первый знак подписи — компактный бейдж для кнопок списка заказов."""
    label = status_label(code)
    return label.split()[0] if label.split() else ""


def router_label(position: int) -> str:
    """Как называется роутер для клиента: «Роутер 2», а не «Cudy TR3000».

    Модель железа клиенту не показываем — решение заказчика от 24 августа
    2026. Отличить свои роутеры друг от друга он может по номеру и MAC,
    а имя платы ему ничего не говорит и рекламирует чужого производителя.

    Оператору модель по-прежнему видна: она в карточке устройства и в списке
    парка, где как раз и нужна.
    """
    return f"Роутер {position}"


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
        return "✓ В наличии"
    if product.get("allow_preorder"):
        return "▸ Под заказ"
    return "✕ Нет в наличии"


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


CAPTION_LIMIT = 1024
"""Предел подписи к фото у Telegram. Длинную карточку обрезать нельзя —
уедет часть характеристик, поэтому такая уходит текстом с превью."""


async def photo_screen(message, photo: str, text: str, *, reply_markup=None) -> bool:
    """Показывает экран фото-сообщением: картинка сверху, текст подписью.

    Так карточка выглядит обычным постом, а не ссылкой с превью. Цена —
    экран переезжает вниз чата: превратить текстовое сообщение в сообщение
    с фото Telegram не даёт, и старое приходится удалять.

    Сначала шлём новое, потом удаляем старое: в обратном порядке сбой
    отправки оставил бы клиента с пустым местом вместо экрана.

    `False` — фото показать не вышло, зовущий рисует экран текстом.
    """
    if not photo or len(text) > CAPTION_LIMIT:
        return False
    try:
        await message.answer_photo(photo, caption=text, reply_markup=reply_markup)
    except Exception as exc:  # noqa: BLE001 — причина в журнал, клиенту экран
        logger.warning(f"[CATALOG] фото не отправилось ({photo}): {exc}")
        return False
    try:
        await message.delete()
    except Exception:
        # Сообщение старше двух суток удалить нельзя — не беда,
        # новый экран уже отправлен.
        pass
    return True


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
    # Цена в заголовке: это первое, что ищут в карточке, и прятать её под
    # список характеристик значит заставлять листать.
    title = f"<b>{_esc(product.get('title'))} — {money(product.get('price'))}</b>"
    if product.get("old_price"):
        title += f"  <s>{money(product['old_price'])}</s>"
    lines = [title, stock_line(product)]
    if product.get("subtitle"):
        # Подзаголовок — одна ключевая выгода, цитатой: так она читается
        # раньше характеристик и не сливается с описанием.
        lines += ["", f"<blockquote>{_esc(product['subtitle'])}</blockquote>"]
    if product.get("description"):
        lines += ["", _esc(product["description"])]

    specs = product.get("specs") or {}
    if specs:
        limit = max(int(app_conf.get("catalog_specs_limit", 8) or 8), 1)
        lines += ["", "<b>Характеристики</b>"]
        for name, value in list(specs.items())[:limit]:
            # Характеристика без значения — «Поддержка Wi-Fi 6»: двоеточие
            # в конце строки читается как оборванная мысль.
            line = f"• {_esc(name)}: {_esc(value)}" if value else f"• {_esc(name)}"
            lines.append(line)
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


def speed_text(options: list[dict]) -> str:
    """Экран выбора скорости.

    Описания разворачиваем в тексте, а не прячем в кнопки: разница между
    «выедет завтра» и «выедет на неделе» — единственное, что клиенту
    здесь надо понять, а в подпись кнопки она не влезает.
    """
    lines = [text("text_order_ask_speed")]
    for option in options:
        lines += ["", f"<b>{_esc(option.get('title'))}</b>", _esc(option.get("description"))]
    return "\n".join(lines)


def speed_keyboard(options: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for option in options:
        builder.row(
            btn(
                "btn_shop_speed",
                text=option.get("title") or option.get("speed", ""),
                callback_data=f"shop_speed:{option.get('speed')}",
            )
        )
    builder.row(btn("btn_shop_cancel_order", callback_data="shop_cancel"))
    return builder.as_markup()


def where_keyboard() -> InlineKeyboardMarkup:
    """Пункт выдачи или курьер. Без цен: их называют после оформления,
    а разница между ними всё равно есть, и оператору её надо знать."""
    builder = InlineKeyboardBuilder()
    builder.row(btn("btn_shop_to_pvz", callback_data="shop_where:pvz"))
    builder.row(btn("btn_shop_to_door", callback_data="shop_where:door"))
    builder.row(btn("btn_shop_back_to_speed", callback_data="shop_speeds"))
    return builder.as_markup()


def renew_text(state: dict) -> str:
    subscription = state.get("subscription") or {}
    until = subscription.get("until")
    if until:
        return format_text("text_renew_active", date=human_date(until))
    return text("text_renew_pending")


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
    lines += [
        "",
        f"<b>Итого: {money(quote.get('total'))}</b>",
        "",
        f"Получатель: {_esc(data.get('name'))}",
        f"Телефон: {_esc(data.get('phone'))}",
        f"Адрес: {_esc(data.get('city'))}, {_esc(data.get('address'))}",
    ]
    # Доставка в сумму не входит и стоит отдельным абзацем — иначе «Итого»
    # прочитается как окончательное, а к нему придёт ещё один счёт.
    if data.get("delivery_speed"):
        speed = SPEED_TITLES.get(data["delivery_speed"], data["delivery_speed"])
        where = "в пункт выдачи" if data.get("delivery_to_pvz") else "курьером до двери"
        lines += ["", f"Доставка: {_esc(speed)}, {where}.", text("text_order_delivery_later")]
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
            lines += [
                "",
                f"Заказ <b>{_esc(order.get('number'))}</b> — "
                f"{_esc(status_label(order.get('status', '')))}",
            ]
            if order.get("tracking_number"):
                lines.append(f"Трек: <code>{_esc(order['tracking_number'])}</code>")
        return "\n".join(lines)

    routers = data.get("routers") or []
    position = next(
        (i + 1 for i, item in enumerate(routers) if item.get("id") == router.get("id")), 1
    )

    if not router.get("activated"):
        # Подписки на этом роутере ещё нет. Раньше тут в любом случае писалось
        # «ещё не выходил на связь» — на роутере, который в эту секунду на
        # связи, это читается как сломанный экран, а не как ответ. И понять,
        # о каком из двух роутеров речь, было нельзя: заголовок с номером
        # до этой ветки не доходил.
        lines = [
            text("text_my_router_not_activated" if router.get("online") else "text_my_router_waiting")
        ]
        if len(routers) > 1:
            lines += [
                "",
                f"{router_label(position)} из {len(routers)}",
                f"MAC: <tg-spoiler><code>{_esc(router.get('mac'))}</code></tg-spoiler>",
            ]
        return "\n".join(lines)

    heading = "<b>Мой роутер</b>"
    if len(routers) > 1:
        # Который из. Без этого на экране два одинаковых заголовка, и понять,
        # чьи показания перед тобой, можно только по MAC ниже.
        heading = f"<b>Мой роутер {position} из {len(routers)}</b>"

    # Вся суть экрана — одной строкой под заголовком: связь и срок подписки.
    link = "✓ На связи" if router.get("online") else "○ Не отвечает"
    if router.get("active"):
        sub = f"подписка до <b>{human_date(router.get('until'))}</b>"
    elif router.get("until"):
        sub = f"⚠ подписка закончилась {human_date(router.get('until'))}"
    else:
        sub = "подписка настраивается, загляните через пару минут"
    lines = [heading, f"{link} · {sub}", ""]

    # Дальше только строки, за которыми есть данные: пустое поле — это не
    # информация, а вопрос «а что тут должно быть?».
    if router.get("online"):
        lines.append(f"В сети: {router.get('clients', 0)} устр.")
    if router.get("online") and (router.get("rx_bytes") or router.get("tx_bytes")):
        lines.append(
            f"Трафик: ↓ {human_bytes(router.get('rx_bytes'))}"
            f" · ↑ {human_bytes(router.get('tx_bytes'))}"
        )
    # MAC нужен только при разговоре с поддержкой — под спойлером он не
    # мозолит глаза, но копируется одним нажатием.
    lines.append(f"MAC: <tg-spoiler><code>{_esc(router.get('mac'))}</code></tg-spoiler>")
    # Адрес пишем и текстом: часть клиентов Telegram не открывает ссылки
    # на адреса домашней сети, а скопировать строку можно всегда.
    panel_url = (app_conf.get("router_panel_url", "") or "").strip()
    if panel_url:
        lines.append(f"Админка: <code>{_esc(panel_url)}</code> — из домашней сети.")
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

    # Подписки нет — рядом с «Обновить» должен стоять ответ на вопрос,
    # который в этот момент задаёт себе клиент. Длинное объяснение живёт
    # на отдельном экране, а не четырьмя абзацами здесь.
    router = data.get("router")
    if router is not None and not router.get("activated") and router.get("online"):
        builder.row(
            btn(
                "btn_shop_why_no_sub",
                callback_data=f"shop_why_no_sub:{current}" if current else "shop_why_no_sub",
            )
        )

    # Переключатель — только когда роутеров правда несколько: у большинства
    # клиентов он один, и лишний ряд кнопок им ни о чём не говорит.
    if len(routers) > 1:
        for item in routers:
            if item.get("id") == current:
                continue
            mark = "✓" if item.get("online") else "○"
            builder.row(
                btn(
                    "btn_my_router_switch",
                    text=f"{mark} {router_label(routers.index(item) + 1)}",
                    callback_data=f"shop_my_router:{item.get('id')}",
                )
            )

    if data.get("router") is None:
        builder.row(btn("btn_catalog", callback_data="shop_catalog"))
    else:
        # Продлевать приходят сюда чаще, чем в главное меню: тут виден срок.
        builder.row(btn("btn_renew_sub", callback_data="shop_renew"))

    # Инструкция и админка роутера — два внешних адреса в домашней сети
    # клиента. Ставим их одним рядом: по отдельности они растягивали экран
    # на девять кнопок, среди которых не видно главной.
    #
    # Ссылками, а не обработчиками: открывать их должен браузер клиента,
    # мы до этой сети не дотягиваемся.
    links = []
    instruction_url = (data.get("instruction_url") or "").strip()
    if instruction_url:
        links.append(btn("btn_router_instruction", url=instruction_url))
    panel_url = (app_conf.get("router_panel_url", "") or "").strip()
    if panel_url and data.get("router") is not None:
        links.append(btn("btn_router_panel", url=panel_url))
    if links:
        builder.row(*links)

    # «Мои заказы» отсюда убраны: они в главном меню, а здесь занимали
    # строку между роутером и выходом, ни к тому ни к другому не относясь.
    builder.row(btn("btn_back_to_main", callback_data="back_to_main"))
    return builder.as_markup()


def order_model(order: dict) -> str:
    """Модель из состава заказа — ей заказ и называется для клиента."""
    items = order.get("items") or []
    return (items[0].get("title") or "").strip() if items else ""


DELIVERY_AWAITING_PAYMENT = "awaiting_payment"
"""Доставка посчитана, но не оплачена. Состояние приходит с ручки — считать
его в боте по цене и отметке значило бы завести второе правило рядом с тем,
что уже есть в основном приложении."""


def orders_keyboard(orders: list[dict], *, show_all: bool = False) -> InlineKeyboardMarkup:
    """Список заказов — только кнопками: текст с теми же номерами и суммами
    над ними дублировал каждую строку дважды."""
    builder = InlineKeyboardBuilder()
    visible = (
        orders
        if show_all
        else [o for o in orders if o.get("status") not in HIDDEN_ORDER_STATUSES]
    )

    # Неоплаченная доставка — первой строкой, отдельной кнопкой на заказ.
    # Счёт со ссылкой живёт пятнадцать минут и в переписке протухает, а эта
    # кнопка есть всегда: клиент платит из «Моих заказов» в одно нажатие,
    # не открывая карточку и не разыскивая старое сообщение.
    pay_label = app_conf.get("btn_shop_pay_delivery", "Оплатить доставку")
    for order in visible:
        if order.get("delivery_state") != DELIVERY_AWAITING_PAYMENT:
            continue
        builder.row(
            btn(
                "btn_shop_pay_delivery",
                text=f"{pay_label} · #{order.get('id')} · {money(order.get('delivery_price'))}",
                callback_data=f"shop_pay_delivery:{order.get('id')}",
            )
        )

    for order in visible:
        name = f"#{order.get('id')} {order_model(order)}".strip()
        builder.row(
            btn(
                "btn_shop_order",
                # Подпись не длиннее 22 знаков — дальше Telegram режет её сам,
                # и бейдж статуса пропадает первым.
                text=f"{name[:18]} · {status_glyph(order.get('status', ''))}",
                callback_data=f"shop_order:{order.get('id')}",
            )
        )
    hidden = len(orders) - len(visible)
    if hidden > 0:
        label = app_conf.get("btn_shop_orders_all", "Показать отменённые")
        builder.row(
            btn("btn_shop_orders_all", text=f"{label} ({hidden})", callback_data="shop_orders_all")
        )
    builder.row(btn("btn_catalog", callback_data="shop_catalog"))
    builder.row(btn("btn_back_to_main", callback_data="back_to_main"))
    return builder.as_markup()


def order_text(order: dict) -> str:
    # «Заказ #12 — Cudy TR3000» вместо машинного «R-260823-0012»: номер для
    # поддержки остаётся внизу, но заказ клиент узнаёт по модели.
    heading = f"Заказ #{order.get('id')}"
    model = order_model(order)
    if model:
        heading += f" — {_esc(model)}"
    lines = [f"<b>{heading}</b>", _esc(status_label(order.get("status", ""))), ""]
    for item in order.get("items", []):
        lines.append(f"• {_esc(item.get('title'))} — {money(item.get('total'))}")
    if is_positive(order.get("discount")):
        lines.append(f"Скидка: −{money(order['discount'])}")
    lines.append(f"<b>Итого: {money(order.get('total'))}</b>")
    if order.get("delivery_summary") and order["delivery_summary"] != "—":
        lines += ["", f"Доставка: {_esc(order['delivery_summary'])}"]
    # Доставка живёт отдельно от «Итого»: её считают после оформления
    # и оплачивают вторым счётом. Молчать о ней нельзя — клиент решит,
    # что заплатил за всё.
    if order.get("delivery_state") == "not_quoted":
        lines.append(text("text_order_delivery_counting"))
    elif order.get("delivery_state") == DELIVERY_AWAITING_PAYMENT:
        lines.append(
            format_text("text_order_delivery_unpaid", price=money(order.get("delivery_price")))
        )
    elif is_positive(order.get("delivery_price")):
        lines.append(f"Доставка: {money(order['delivery_price'])} — оплачена")
    if order.get("tracking_number"):
        lines.append(f"Трек: <code>{_esc(order['tracking_number'])}</code>")
    lines += ["", f"№ для поддержки: <code>{_esc(order.get('number'))}</code>"]
    return "\n".join(lines)


def order_keyboard(order: dict, cancellable: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    # Ссылку выдаём по нажатию, а не заранее: платёжная ссылка PLATEGA живёт
    # пятнадцать минут, и вшитая в экран через час была бы уже мёртвой.
    if order.get("delivery_state") == DELIVERY_AWAITING_PAYMENT:
        builder.row(
            btn("btn_shop_pay_delivery", callback_data=f"shop_pay_delivery:{order.get('id')}")
        )
    # «Как подключить» нужна ровно между «посылка едет» и «роутер ожил»:
    # до отправки читать её нечего, после активации всё уже работает.
    # Промежуток считает ручка — она же знает статус заказа.
    #
    # Это не та инструкция, что в «Моём роутере»: там постоянная — пароль
    # от Wi-Fi, срок, продление, — и она у клиента всегда.
    setup_url = (order.get("instruction_url") or "").strip()
    if setup_url:
        builder.row(btn("btn_router_setup", url=setup_url))
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
        "delivery_speed": data.get("delivery_speed", ""),
        "delivery_to_pvz": bool(data.get("delivery_to_pvz", True)),
        "address": data.get("address", ""),
        "promo_code": data.get("promo_code", ""),
    }


async def send_product_card(message, product_id: int) -> str:
    """Шлёт карточку модели новым сообщением. Возвращает текст ошибки.

    Отдельно от `cq_item`: сюда приходят с витрины по ссылке `?start=buy_<id>`,
    и править нечего — экрана в чате ещё нет, а команду клиента бот не удаляет.
    """
    product, error = await shop_api.product(product_id)
    if error:
        return error

    body = card_text(product)
    markup = card_keyboard(product)
    photo = product.get("photo_url") or ""
    if photo and len(body) <= CAPTION_LIMIT:
        try:
            await message.answer_photo(photo, caption=body, reply_markup=markup)
            return ""
        except Exception as exc:  # noqa: BLE001 — причина в журнал, клиенту карточка
            logger.warning(f"[CATALOG] фото карточки не отправилось ({photo}): {exc}")

    await message.answer(body, reply_markup=markup, link_preview_options=card_preview(product))
    return ""


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

        # Карточку с фото показываем постом: картинка сверху, описание
        # подписью. Не вышло — рисуем текстом, картинка идёт превью.
        shown = await photo_screen(
            query.message,
            product.get("photo_url") or "",
            card_text(product),
            reply_markup=card_keyboard(product),
        )
        if not shown:
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

    async def ask_speed(target, state: FSMContext, *, edit: bool):
        """Экран выбора скорости доставки.

        Цен здесь нет: их называет оператор после оформления. Обещать сумму
        заранее нечестно — она зависит от города и габаритов, а обещанная
        и не сошедшаяся цена хуже честного «посчитаем и напишем».

        Вариантов может не оказаться вовсе — тогда заказ оформляется без
        доставки, а не упирается в пустой экран.
        """
        data, error = await shop_api.delivery_options()
        options = data.get("options", [])
        if error or not options:
            if error:
                logger.warning(f"[CATALOG] доставка недоступна: {error}")
            await state.update_data(delivery_speed="", address="")
            return await ask_promo(target, state, edit=edit)

        await state.set_state(None)
        payload = {
            "text": speed_text(options),
            "reply_markup": speed_keyboard(options),
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
        await ask_speed(message, state, edit=False)

    @dp.callback_query(F.data == "shop_speeds")
    async def cq_speeds(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        await ask_speed(query, state, edit=True)
        await query.answer()

    @dp.callback_query(F.data.startswith("shop_speed:"))
    async def cq_speed(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        await state.update_data(delivery_speed=query.data.split(":")[1])
        await edit_screen(
            query.message,
            text("text_order_ask_where"),
            reply_markup=where_keyboard(),
            link_preview_options=NO_PREVIEW,
        )
        await query.answer()

    @dp.callback_query(F.data.startswith("shop_where:"))
    async def cq_where(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        to_pvz = query.data.split(":")[1] == "pvz"
        await state.update_data(delivery_to_pvz=to_pvz)
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

    @dp.callback_query(F.data.startswith("shop_why_no_sub"))
    async def cq_why_no_sub(query: CallbackQuery, state: FSMContext):
        """Отдельный экран с объяснением вместо четырёх абзацев в «Моём
        роутере»: длинный текст нужен тому, кто задал вопрос, а не всем."""
        if await blocked(query):
            return
        await state.clear()
        device_id = ""
        if ":" in query.data:
            device_id = query.data.split(":", 1)[1]
        back_cb = f"shop_my_router:{device_id}" if device_id else "shop_my_router"
        await edit_screen(
            query.message,
            text("text_my_router_why_no_sub"),
            reply_markup=InlineKeyboardBuilder()
            .row(btn("btn_renew_sub", callback_data="shop_renew"))
            .row(btn("btn_back", callback_data=back_cb))
            .as_markup(),
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
            format_text("text_renew_selected", plan=_esc(plan.get("title")))
            + "\n\n"
            + (
                text("text_order_pay_hint")
                if pay_url
                else text("text_order_pay_later")
            ),
            reply_markup=created_keyboard(pay_url),
            link_preview_options=NO_PREVIEW,
        )

    async def show_orders(query: CallbackQuery, *, show_all: bool):
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
            text("text_orders_intro"),
            reply_markup=orders_keyboard(orders, show_all=show_all),
            link_preview_options=NO_PREVIEW,
        )
        await query.answer()

    @dp.callback_query(F.data == "shop_orders")
    async def cq_orders(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        await state.clear()
        await show_orders(query, show_all=False)

    @dp.callback_query(F.data == "shop_orders_all")
    async def cq_orders_all(query: CallbackQuery, state: FSMContext):
        """Тот же список, но вместе со свёрнутыми отменёнными и возвратами."""
        if await blocked(query):
            return
        await state.clear()
        await show_orders(query, show_all=True)

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

    @dp.callback_query(F.data.startswith("shop_pay_delivery:"))
    async def cq_pay_delivery(query: CallbackQuery):
        """Свежая ссылка на оплату доставки."""
        if await blocked(query):
            return
        order_id = int(query.data.split(":")[1])
        data, error = await shop_api.delivery_payment_link(order_id, query.from_user.id)
        if error:
            return await query.answer(error[:200], show_alert=True)

        pay_url = data.get("pay_url") or ""
        if not pay_url:
            return await query.answer("Ссылка не пришла, попробуйте позже.", show_alert=True)
        await edit_screen(
            query.message,
            format_text("text_order_pay_delivery", price=money(data.get("price"))),
            reply_markup=InlineKeyboardBuilder()
            .row(btn("btn_payment_pay_link", url=pay_url))
            .row(btn("btn_shop_back_to_orders", callback_data="shop_orders"))
            .as_markup(),
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
