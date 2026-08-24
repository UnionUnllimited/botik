"""Содержимое витрины: каталог, сроки подписки и тексты «что, где и зачем».

Витрина ничего не продаёт сама: заказ оформляется в боте, туда и уводит
каждая кнопка «Купить». Иначе пришлось бы завести второе оформление заказа
рядом с тем, что уже работает пятью шагами, — и опознавать клиента без
Telegram, а привязка подписки у нас идёт по `tg_id`.

Данные берутся прямо из наших таблиц, а не через `/api/v1/catalog/*`:
ручка закрыта общим секретом, а витрину открывает случайный человек
из поиска. Ходить самому к себе по HTTP ради тех же строк — лишний круг.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.models import Plan, Product
from core.services import settings_service

# Значки той же типографской семьи, что в боте: ▣ ⊕ ↻ ▤ ◇ ◈ ⓘ.
# Цветные эмодзи не берём — они по-разному выглядят на разных устройствах,
# и витрина рядом с ботом выглядела бы чужой.
STEPS: list[dict[str, str]] = [
    {
        "glyph": "⊕",
        "title": "Выбираете модель и срок",
        "text": "В боте открывается карточка роутера: цена, характеристики, "
        "сроки подписки. Срок выбирается сразу — от него зависит цена.",
    },
    {
        "glyph": "▤",
        "title": "Оставляете данные и платите",
        "text": "Имя, телефон, город и способ доставки — пять коротких шагов. "
        "Оплата картой или через СБП по ссылке из бота.",
    },
    {
        "glyph": "◈",
        "title": "Получаем и отправляем",
        "text": "Цену доставки называет оператор — она зависит от города "
        "и веса посылки. Трек-номер приходит в бот.",
    },
    {
        "glyph": "▣",
        "title": "Включаете в розетку",
        "text": "Роутер настроен заранее. Подписка включается сама при первом "
        "выходе на связь — дни доставки не сгорают.",
    },
]

FEATURES: list[dict[str, str]] = [
    {
        "glyph": "◇",
        "title": "Работает вся домашняя сеть",
        "text": "Телевизор, приставка, ноутбук, телефоны гостей. Ничего "
        "не нужно ставить на каждое устройство и никуда входить.",
    },
    {
        "glyph": "↻",
        "title": "Настраивать нечего",
        "text": "Роутер приезжает готовым. Подключение к сервису прошито "
        "до отправки, вам остаётся кабель провайдера и розетка.",
    },
    {
        "glyph": "▣",
        "title": "Видно, что происходит",
        "text": "В боте — связь, число устройств в сети, загрузка, аптайм "
        "и срок подписки. Продление там же, в два касания.",
    },
    {
        "glyph": "ⓘ",
        "title": "Поддержка людьми",
        "text": "Пишете в бот — отвечает оператор. Роутер можно перезагрузить "
        "удалённо, не выезжая и не объясняя по телефону, где кнопка.",
    },
]

FAQ: list[dict[str, str]] = [
    {
        "question": "Что именно я покупаю?",
        "answer": "Роутер и подписку на сервис стабильного доступа к зарубежным "
        "ресурсам. Роутер ваш навсегда, подписка продлевается по сроку, "
        "который вы выбрали при покупке.",
    },
    {
        "question": "Нужно ли что-то устанавливать на телефон?",
        "answer": "Нет. Доступ настроен на самом роутере, поэтому он работает "
        "для всего, что подключено к домашней сети, — включая устройства, "
        "куда вообще ничего нельзя установить.",
    },
    {
        "question": "Сколько устройств можно подключить?",
        "answer": "Сколько выдержит домашняя сеть. Мы не считаем устройства "
        "поштучно и не продаём слоты: за роутером стоит квартира, а не один "
        "телефон.",
    },
    {
        "question": "Как продлить подписку?",
        "answer": "Кнопкой «Продлить подписку» в боте. Срок добавляется к тому "
        "же роутеру; если роутеров несколько — к выбранному, у каждого свой срок.",
    },
    {
        "question": "Сколько стоит доставка?",
        "answer": "Считается по каждому заказу отдельно: цена зависит от города, "
        "веса и перевозчика. Оператор называет её после оформления, оплатить "
        "можно кнопкой в «Моих заказах».",
    },
    {
        "question": "А если роутер перестанет отвечать?",
        "answer": "Напишите в поддержку. Мы видим состояние роутера и можем "
        "перезагрузить его удалённо — чаще всего этого хватает.",
    },
]


INSTRUCTION_STEPS: list[dict[str, str]] = [
    {
        "glyph": "⊕",
        "title": "Распакуйте и включите",
        "text": "Блок питания в розетку, кабель провайдера — в порт WAN "
        "(он выделен цветом и подписан). Дождитесь, пока индикаторы перестанут мигать.",
    },
    {
        "glyph": "◇",
        "title": "Подключитесь к его сети",
        "text": "Имя сети и пароль напечатаны на наклейке снизу роутера. "
        "С телефона или ноутбука — как к обычному Wi-Fi.",
    },
    {
        "glyph": "↻",
        "title": "Подождите пару минут",
        "text": "Роутер сам выйдет на связь и получит подписку — вводить ничего "
        "не нужно. Отсчёт срока начинается с этого момента, дни доставки не сгорают.",
    },
    {
        "glyph": "▣",
        "title": "Проверьте в боте",
        "text": "Раздел «Мой роутер» покажет связь, число устройств в сети и срок "
        "подписки. Как только там появились показания — всё работает.",
    },
    {
        "glyph": "◈",
        "title": "Если что-то не так",
        "text": "Напишите в поддержку в боте. Мы видим состояние роутера и можем "
        "перезагрузить его удалённо — чаще всего этого хватает.",
    },
]
"""Заглушка на месте инструкции.

Написана по тому, что роутер делает на самом деле: клиент включает его
в розетку, подписка приезжает сама. Заменить её настоящей — правка этого
списка, адрес страницы при этом не меняется."""


def money(value: Decimal | None) -> str:
    """«6 900 ₽» — без копеек, если их нет: витрина не бухгалтерия."""
    if value is None:
        return ""
    quantized = Decimal(value).quantize(Decimal("0.01"))
    whole = int(quantized)
    tail = quantized - whole
    grouped = f"{whole:,}".replace(",", " ")
    if tail:
        return f"{grouped},{int(tail * 100):02d} ₽"
    return f"{grouped} ₽"


def period_text(plan: Plan) -> str:
    """«12 мес. +30 дн.» — тот же способ назвать срок, что и в боте."""
    parts = []
    if plan.months:
        parts.append(f"{plan.months} мес.")
    if plan.extra_days:
        parts.append(f"+{plan.extra_days} дн.")
    return " ".join(parts) or plan.title


def photo_url(product: Product) -> str:
    """Абсолютный адрес картинки: витрину открывают и по другому домену."""
    raw = product.photo_url or ""
    if not raw or raw.startswith("http"):
        return raw
    return f"{settings.api.public_base_url.rstrip('/')}{raw}"


def bot_link(payload: str = "") -> str:
    """Ссылка в бота, при необходимости — сразу на нужный экран.

    Пустой `APP_BOT_USERNAME` значит, что ссылку собрать не из чего:
    вместо неработающей кнопки витрина покажет подсказку про Telegram.
    """
    username = settings.app.bot_username.strip().lstrip("@")
    if not username:
        return ""
    if payload:
        return f"https://t.me/{username}?start={payload}"
    return f"https://t.me/{username}"


def product_card(product: Product) -> dict[str, Any]:
    """Карточка для витрины.

    `model_code` наружу не идёт: клиенту показывается название товара,
    а не модель железа — то же решение, что и на экране «Мой роутер».
    """
    return {
        "id": product.id,
        "slug": product.slug,
        "title": product.title,
        "subtitle": product.subtitle or "",
        "description": product.description or "",
        "price": money(product.price),
        "old_price": money(product.old_price) if product.old_price else "",
        "in_stock": product.in_stock,
        "preorder": product.stock <= 0 and product.allow_preorder,
        "specs": list((product.specs or {}).items()),
        "photo_url": photo_url(product),
        "buy_url": bot_link(f"buy_{product.id}"),
    }


def plan_card(plan: Plan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "title": plan.title,
        "period": period_text(plan),
        "price": money(plan.price),
        "per_month": money(plan.price_per_month) if plan.months > 1 else "",
        "description": plan.description or "",
    }


async def page_content(session: AsyncSession) -> dict[str, Any]:
    """Всё, что нужно шаблону витрины, одним запросом на раздел."""
    products = list(
        await session.scalars(
            select(Product)
            .where(Product.is_active.is_(True))
            .order_by(Product.sort_order, Product.id)
        )
    )
    plans = list(
        await session.scalars(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.months)
        )
    )

    hero_title = await settings_service.get_str(session, "landing.hero_title")
    hero_subtitle = await settings_service.get_str(session, "landing.hero_subtitle")
    support = await settings_service.get_str(session, "support.contact")

    return {
        "brand": settings.app.brand,
        "hero_title": hero_title or settings_service.DEFAULTS["landing.hero_title"],
        "hero_subtitle": hero_subtitle or settings_service.DEFAULTS["landing.hero_subtitle"],
        "products": [product_card(product) for product in products],
        "plans": [plan_card(plan) for plan in plans],
        "steps": STEPS,
        "features": FEATURES,
        "faq": FAQ,
        "bot_url": bot_link(),
        "support_contact": support,
    }


async def is_enabled(session: AsyncSession) -> bool:
    return await settings_service.get_bool(session, "landing.enabled")
