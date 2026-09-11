"""Роутерному клиенту их бот подписку не выдаёт.

Срок роутерному клиенту ведёт магазин и приносит в их базу зеркалом. Учётки
в панели у него на их стороне нет, и `grant_subscription` уходил по ветке
«создать новую»: заводил клиенту вторую, телефонную, учётку. Бонусные дни
ложились на неё, роутер продолжал ходить по своей, а клиент читал «+7 дней»
и не находил их нигде. Следующий круг зеркала возвращал прежний срок.

Проверка стоит внутри самой выдачи, а не у зовущих: их два десятка —
пробные дни, админская правка, реферальный бонус, повтор платежа, — и
каждый следующий забыли бы прикрыть.

Реферальные модули получают зависимости параметрами, поэтому их зовём
по-настоящему. `db_helpers` и `subscription_manager` при импорте тянут
`config` и мигрируют боевую базу — их читаем исходником, как и остальные
тесты их стороны.
"""

from __future__ import annotations

import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

BOT = Path(__file__).resolve().parents[1] / "bot"


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


join_bonus = _module(BOT / "src" / "referral_join_bonus.py", "referral_join_bonus")
partner = _module(BOT / "src" / "pay" / "partner.py", "partner")


class _Bot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **_kwargs):
        self.sent.append((chat_id, text))


class _Helpers:
    """Столько их базы, сколько трогают эти два пути."""

    def __init__(self, *, inviter: int, method: str = "referral", shop: bool = False) -> None:
        self._inviter = inviter
        self._method = method
        self._shop = shop
        self.marked: list[int] = []
        self.balance_updates: list[tuple] = []

    async def get_invited_by(self, _user_id):
        return self._inviter

    async def get_invited_by_method(self, _user_id):
        return self._method

    async def get_user(self, _user_id):
        return {"telegram_id": self._inviter, "registration_type": "bot"}

    async def is_shop_client(self, _telegram_id):
        return self._shop

    async def is_referral_payment_bonus_given(self, *_args, **_kwargs):
        return False

    async def mark_referral_payment_bonus_given(self, inviter, *_args, **_kwargs):
        self.marked.append(inviter)

    async def get_partner_percent(self, _inviter):
        return 10

    async def log_partner_accrual(self, *_args, **_kwargs):
        return None

    @asynccontextmanager
    async def get_db_connection_safe(self):
        updates = self.balance_updates

        class _Db:
            async def execute(self, sql, params=()):
                updates.append((sql, params))

            async def commit(self):
                return None

        yield _Db()


def _world(**kwargs):
    helpers = _Helpers(**kwargs)
    bot = _Bot()
    granted: list[tuple] = []

    async def grant_subscription(user_id, days, **_kwargs):
        granted.append((user_id, days))
        return {"ok": True}

    async def resolve_limit_ip_for_user(_user_id):
        return 0

    return SimpleNamespace(
        helpers=helpers,
        bot=bot,
        granted=granted,
        app_conf={"ref_bonus_on_join_days": 3, "ref_bonus_on_payment_days": 7},
        keyboards=SimpleNamespace(get_back_to_main_keyboard=lambda: None),
        grant=grant_subscription,
        limit_ip=resolve_limit_ip_for_user,
    )


async def _join(world) -> bool:
    return await join_bonus.try_grant_referral_join_bonus(
        invited_user_id=222,
        bot=world.bot,
        db_helpers=world.helpers,
        app_conf=world.app_conf,
        keyboards=world.keyboards,
        grant_subscription=world.grant,
        resolve_limit_ip_for_user=world.limit_ip,
    )


async def _paid(world) -> None:
    await partner.credit_partner_and_referral(
        payer_user_id=222,
        payment_id="p-1",
        amount_rub=900.0,
        currency="RUB",
        bot=world.bot,
        db_helpers=world.helpers,
        app_conf=world.app_conf,
        keyboards=world.keyboards,
        grant_subscription=world.grant,
        resolve_limit_ip_for_user=world.limit_ip,
    )


@pytest.mark.asyncio
async def test_join_bonus_skips_a_router_inviter():
    world = _world(inviter=111, shop=True)

    assert await _join(world) is False
    assert world.granted == []
    # Ни отметки «выдан», ни сообщения: обещать дни, которых не будет,
    # хуже, чем промолчать.
    assert world.helpers.marked == []
    assert world.bot.sent == []


@pytest.mark.asyncio
async def test_join_bonus_still_works_for_an_ordinary_inviter():
    """Защита не должна задеть тех, ради кого бот и писан."""
    world = _world(inviter=111, shop=False)

    assert await _join(world) is True
    assert world.granted == [(111, 3)]
    assert world.helpers.marked == [111]
    assert len(world.bot.sent) == 1


@pytest.mark.asyncio
async def test_payment_bonus_skips_a_router_inviter():
    world = _world(inviter=111, shop=True)

    await _paid(world)

    assert world.granted == []
    assert world.helpers.marked == []
    assert world.bot.sent == []


@pytest.mark.asyncio
async def test_payment_bonus_still_works_for_an_ordinary_inviter():
    world = _world(inviter=111, shop=False)

    await _paid(world)

    assert world.granted == [(111, 7)]
    assert world.helpers.marked == [111]


@pytest.mark.asyncio
async def test_partner_money_reaches_a_router_client_all_the_same():
    """Партнёрский процент — деньги на баланс, а не дни в панели.

    Их клиент тратит этот баланс внутри их бота, и роутер тут ни при чём:
    запрет на выдачу дней не должен забирать у человека заработанное.
    """
    world = _world(inviter=111, method="partner", shop=True)

    await _paid(world)

    assert world.helpers.balance_updates, "начисление на баланс не дошло до базы"
    assert world.helpers.balance_updates[0][1] == (90.0, 111)
    assert len(world.bot.sent) == 1


class TestTheGuardSitsInOnePlace:
    """`db_helpers` и `subscription_manager` читаем исходником: их импорт
    тянет `config` и мигрирует боевую базу."""

    MANAGER = (BOT / "subscription_manager.py").read_text(encoding="utf-8")
    HELPERS = (BOT / "db_helpers.py").read_text(encoding="utf-8")

    def test_issuing_refuses_a_router_client(self):
        body = self.MANAGER[self.MANAGER.index("async def grant_subscription(") :]
        body = body[: body.index("\nasync def ", 1)]
        assert "is_shop_client" in body

    def test_it_refuses_before_creating_a_second_panel_account(self):
        """Порядок и есть смысл правки: отказ должен стоять до ветки
        «создать новую», иначе учётка уже заведена."""
        body = self.MANAGER[self.MANAGER.index("async def grant_subscription(") :]
        body = body[: body.index("\nasync def ", 1)]
        assert body.index("is_shop_client") < body.index("_create_remnawave_subscription")

    def test_the_flag_is_read_from_the_mirror_column(self):
        body = self.HELPERS[self.HELPERS.index("async def is_shop_client(") :]
        body = body[: body.index("\nasync def ", 1)]
        assert "COALESCE(shop_subscription, 0)" in body
