"""Наполнение базы стартовыми данными.

    python -m scripts.seed            # товары, тарифы, узлы, настройки
    python -m scripts.seed --admin    # плюс учётка администратора

Скрипт идемпотентен: повторный запуск ничего не дублирует.
Цены — заглушки для теста, реальные проставляются в админке.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
from decimal import Decimal

from sqlalchemy import select

from core.config import settings
from core.db import dispose_engine, session_scope
from core.enums import AdminRole, NodeProtocol, NodeStatus
from core.logging import configure_logging, get_logger
from core.models import AdminUser, Node, NodeAssignment, NodeGroup, Plan, Product
from core.security import hash_password
from core.services import settings_service

log = get_logger("seed")

PRODUCTS = [
    {
        "slug": "router-basic",
        "title": "Роутер Basic",
        "subtitle": "Для квартиры и небольшого офиса",
        "description": (
            "Работает сразу после включения: подключите кабель провайдера — "
            "и все устройства в доме получат стабильный доступ к зарубежным сервисам."
        ),
        "model_code": "basic",
        "price": Decimal("6900.00"),
        "stock": 25,
        "sort_order": 10,
        "specs": {
            "Wi-Fi": "до 1,8 Гбит/с, две частоты",
            "Порты": "3 LAN + 1 WAN",
            "Устройств": "до 25 одновременно",
            "Питание": "адаптер в комплекте",
        },
    },
    {
        "slug": "router-pro",
        "title": "Роутер Pro",
        "subtitle": "Для большой квартиры и дома",
        "description": (
            "Мощнее базовой модели: больше портов, USB для накопителя и поддержка "
            "подключения по логину и паролю провайдера."
        ),
        "model_code": "pro",
        "price": Decimal("9900.00"),
        "stock": 15,
        "sort_order": 20,
        "specs": {
            "Wi-Fi": "до 3 Гбит/с, две частоты",
            "Порты": "4 LAN + 1 WAN, USB 3.0",
            "Устройств": "до 60 одновременно",
            "Подключение": "кабель, в том числе с логином и паролем",
        },
    },
]

PLANS = [
    {"slug": "m1", "title": "1 месяц", "months": 1, "price": Decimal("399.00"), "sort_order": 10},
    {
        "slug": "m3",
        "title": "3 месяца",
        "months": 3,
        "price": Decimal("1090.00"),
        "discount_percent": Decimal("9.00"),
        "sort_order": 20,
    },
    {
        "slug": "m6",
        "title": "6 месяцев",
        "months": 6,
        "price": Decimal("1990.00"),
        "discount_percent": Decimal("17.00"),
        "sort_order": 30,
    },
    {
        "slug": "m12",
        "title": "12 месяцев",
        "months": 12,
        "extra_days": 30,
        "price": Decimal("3490.00"),
        "discount_percent": Decimal("27.00"),
        "is_default": True,
        "sort_order": 40,
    },
]

NODES = [
    {
        "remarks": f"{settings.subscription.node_prefix}Amsterdam-1",
        "location": "Амстердам",
        "country_code": "NL",
        "host": "nl1.example.net",
        "port": 443,
        "priority": 10,
    },
    {
        "remarks": f"{settings.subscription.node_prefix}Frankfurt-1",
        "location": "Франкфурт",
        "country_code": "DE",
        "host": "de1.example.net",
        "port": 443,
        "priority": 20,
    },
]


async def seed_catalog(session) -> None:
    existing = set((await session.scalars(select(Product.slug))).all())
    for data in PRODUCTS:
        if data["slug"] in existing:
            continue
        session.add(Product(**data))
        log.info("seed.product", slug=data["slug"])

    existing_plans = set((await session.scalars(select(Plan.slug))).all())
    for data in PLANS:
        if data["slug"] in existing_plans:
            continue
        session.add(Plan(**data))
        log.info("seed.plan", slug=data["slug"])


async def seed_nodes(session) -> None:
    group = await session.scalar(select(NodeGroup).where(NodeGroup.slug == "default"))
    if group is None:
        group = NodeGroup(
            slug="default",
            title="Базовый набор",
            description="Узлы, которые получают все тарифы по умолчанию",
            is_default=True,
        )
        session.add(group)
        await session.flush()
        log.info("seed.node_group", slug="default")

    existing = set((await session.scalars(select(Node.remarks))).all())
    for data in NODES:
        if data["remarks"] in existing:
            continue
        node = Node(
            **data,
            protocol=NodeProtocol.VLESS_REALITY,
            status=NodeStatus.ACTIVE,
            config={
                "uuid": "00000000-0000-0000-0000-000000000000",
                "flow": "xtls-rprx-vision",
                "sni": "www.microsoft.com",
                "public_key": "ЗАМЕНИТЕ_НА_РЕАЛЬНЫЙ_КЛЮЧ",
                "short_id": "0123abcd",
                "fingerprint": "chrome",
            },
        )
        session.add(node)
        await session.flush()
        session.add(NodeAssignment(node_id=node.id, group_id=group.id))
        log.info("seed.node", remarks=data["remarks"])

    for plan in await session.scalars(select(Plan).where(Plan.node_group_id.is_(None))):
        plan.node_group_id = group.id


async def seed_admin(session) -> str | None:
    if await session.scalar(select(AdminUser).limit(1)) is not None:
        log.info("seed.admin_exists")
        return None
    password = secrets.token_urlsafe(12)
    session.add(
        AdminUser(
            login="owner",
            password_hash=hash_password(password),
            full_name="Владелец",
            tg_id=settings.bot.owner_id or None,
            role=AdminRole.OWNER,
            is_active=True,
        )
    )
    return password


async def main() -> int:
    parser = argparse.ArgumentParser(description="Наполнение базы стартовыми данными")
    parser.add_argument("--admin", action="store_true", help="создать учётку администратора")
    args = parser.parse_args()

    configure_logging("seed")
    password: str | None = None
    async with session_scope() as session:
        await seed_catalog(session)
        await session.flush()
        await seed_nodes(session)
        created = await settings_service.ensure_defaults(session)
        log.info("seed.settings", created=created)
        if args.admin:
            password = await seed_admin(session)

    await dispose_engine()

    print("Готово: товары, тарифы, узлы и настройки на месте.")
    if password:
        print(f"Администратор: логин owner, пароль {password}")
        print("Сохраните пароль — он больше не будет показан.")
    print("Реальные цены и параметры узлов проставьте в админке.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
