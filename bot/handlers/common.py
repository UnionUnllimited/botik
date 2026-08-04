"""Старт, справка, разбор deep-link и навигация по главному меню.

Все переходы идут через `screen`: экран один, он правится на месте.
Поэтому «Главное меню» с любого экрана возвращает туда же, где клиент
уже был, а не добавляет в чат ещё одно сообщение.
"""

from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import inline
from bot.texts import ru
from bot.utils import screen
from bot.utils.deeplink import PayloadKind, parse_start_payload
from core.enums import ReferralStatus
from core.models import Referral, User

router = Router(name="common")
log = structlog.get_logger("bot.common")


async def _link_referrer(session: AsyncSession, user: User, referrer_tg_id: int) -> User | None:
    """Привязывает пригласившего. Награда начисляется после оплаты."""
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


async def show_menu(event: Message | CallbackQuery, user: User, *, is_new: bool = False) -> None:
    await screen.show(
        event,
        ru.start_text(name=user.display_name, is_new=is_new),
        markup=inline.main_menu(),
    )


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

    await show_menu(message, user, is_new=is_new_user)
    if referrer is not None:
        # Разовое сообщение: уедет из чата при следующем переходе по меню.
        await screen.notify(message, ru.REFERRAL_LINKED.format(name=referrer.display_name))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await screen.show(message, ru.HELP, markup=inline.back_to_menu())


@router.callback_query(inline.NavCB.filter(F.action == "menu"))
async def back_to_menu(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await show_menu(callback, user)


@router.message(Command("menu"))
async def cmd_menu(message: Message, user: User, state: FSMContext) -> None:
    await state.clear()
    await show_menu(message, user)


@router.message(F.text == ru.BTN_MENU)
async def legacy_menu_button(message: Message, user: User, state: FSMContext) -> None:
    """Совместимость со старой reply-клавиатурой: убираем её и показываем меню."""
    await state.clear()
    await screen.remove_reply_keyboard(message)
    await show_menu(message, user)


async def _pending_section_text(session: AsyncSession, section: str) -> str:
    from core.services import settings_service

    contact = await settings_service.get_str(session, "support.contact")
    hours = await settings_service.get_str(session, "support.working_hours")
    return ru.SECTION_PENDING[section].format(contact=contact or "напишите владельцу бота", hours=hours)


@router.callback_query(inline.MenuCB.filter(F.section.in_({"guides", "support"})))
async def section_in_progress(
    callback: CallbackQuery, callback_data: inline.MenuCB, session: AsyncSession
) -> None:
    """Разделы, которые появятся вместе с базой знаний и тикетами."""
    await callback.answer()
    text = await _pending_section_text(session, callback_data.section)
    await screen.show(callback, text, markup=inline.back_to_menu())


@router.message(F.text.in_({ru.BTN_GUIDES, ru.BTN_SUPPORT}))
async def legacy_section_button(message: Message, session: AsyncSession) -> None:
    """Те же разделы, нажатые на старой reply-клавиатуре."""
    section = {ru.BTN_GUIDES: "guides", ru.BTN_SUPPORT: "support"}[message.text or ""]
    await screen.remove_reply_keyboard(message)
    await screen.show(
        message, await _pending_section_text(session, section), markup=inline.back_to_menu()
    )
