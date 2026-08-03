"""Старт, справка и разбор deep-link."""

from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.reply import main_menu
from bot.texts import ru
from bot.utils.deeplink import PayloadKind, parse_start_payload
from core.enums import ReferralStatus
from core.models import Referral, User

router = Router(name="common")
log = structlog.get_logger("bot.common")


async def _link_referrer(session: AsyncSession, user: User, referrer_tg_id: int) -> User | None:
    """Привязывает пригласившего. Награда начисляется после оплаты (этап 2)."""
    if user.referrer_id is not None or referrer_tg_id == user.tg_id:
        return None
    referrer = await session.scalar(select(User).where(User.tg_id == referrer_tg_id))
    if referrer is None or referrer.id == user.id or referrer.is_blocked:
        return None
    existing = await session.scalar(select(Referral).where(Referral.referred_id == user.id))
    if existing is not None:
        return None
    user.referrer_id = referrer.id
    session.add(Referral(referrer_id=referrer.id, referred_id=user.id, status=ReferralStatus.PENDING))
    log.info("referral.linked", user_id=user.id, referrer_id=referrer.id)
    return referrer


@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    is_new_user: bool,
) -> None:
    await state.clear()
    payload = parse_start_payload(command.args)
    referrer: User | None = None

    if payload is not None:
        if user.start_payload is None:
            user.start_payload = payload.raw
        if payload.kind is PayloadKind.UTM and not user.utm_source:
            user.utm_source = payload.value[:64]
        elif payload.kind is PayloadKind.REFERRAL and payload.as_int:
            referrer = await _link_referrer(session, user, payload.as_int)
        log.info("start.payload", kind=str(payload.kind), value=payload.value)

    await message.answer(
        ru.start_text(name=user.display_name, is_new=is_new_user),
        reply_markup=main_menu(),
    )
    if referrer is not None:
        await message.answer(ru.REFERRAL_LINKED.format(name=referrer.display_name))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(ru.HELP, reply_markup=main_menu())


@router.message(F.text == ru.BTN_MENU)
async def show_menu(message: Message, user: User, state: FSMContext) -> None:
    await state.clear()
    await message.answer(ru.start_text(name=user.display_name, is_new=False), reply_markup=main_menu())
