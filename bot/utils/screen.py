"""Один активный экран в чате вместо ленты сообщений.

Переход по меню правит уже отправленное сообщение, а не шлёт следующее.
То, что править нельзя — карточку с фото, ответ пользователя, старый экран —
удаляем. Иначе после десятка нажатий чат превращается в простыню, в которой
клиент листает вверх и жмёт кнопки прошлых состояний.

Идентификатор экрана лежит в Redis, а не в данных FSM: хендлеры регулярно
зовут `state.clear()`, и экран пережил бы это разве что случайно.

Telegram разрешает боту удалять сообщения не старше 48 часов — по этому же
сроку живут ключи. Любая ошибка удаления не важна: сообщения могло уже
не быть, а сорвать из-за этого сценарий нельзя.
"""

from __future__ import annotations

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReplyKeyboardRemove

from core.config import settings
from core.redis_client import get_redis

log = structlog.get_logger("bot.screen")

TTL_SEC = 47 * 3600
"""Чуть меньше 48 часов: дальше Telegram всё равно не даст удалить."""

KIND_TEXT = "text"
KIND_PHOTO = "photo"


def _anchor_key(chat_id: int) -> str:
    return settings.redis.key("screen", str(chat_id))


def _extra_key(chat_id: int) -> str:
    return settings.redis.key("screen_extra", str(chat_id))


def context_of(event: Message | CallbackQuery) -> tuple[Bot, int] | None:
    """Бот и чат из любого события. None — событие без чата (inline-режим)."""
    message = event if isinstance(event, Message) else event.message
    if message is None or event.bot is None:
        return None
    return event.bot, message.chat.id


async def _delete(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError as exc:
        log.debug("screen.delete_skipped", message_id=message_id, error=str(exc))


async def _load_anchor(chat_id: int) -> tuple[int, str] | None:
    try:
        raw = await get_redis().get(_anchor_key(chat_id))
    except Exception as exc:  # noqa: BLE001 — без Redis просто отправим новое сообщение
        log.warning("screen.anchor_read_failed", error=str(exc))
        return None
    if not raw:
        return None
    message_id, _, kind = str(raw).partition(":")
    return (int(message_id), kind or KIND_TEXT) if message_id.isdigit() else None


async def _save_anchor(chat_id: int, message_id: int, kind: str) -> None:
    try:
        await get_redis().set(_anchor_key(chat_id), f"{message_id}:{kind}", ex=TTL_SEC)
    except Exception as exc:  # noqa: BLE001
        log.warning("screen.anchor_write_failed", error=str(exc))


async def forget(chat_id: int) -> None:
    """Забыть экран, не удаляя сообщение: оно должно остаться в переписке."""
    try:
        await get_redis().delete(_anchor_key(chat_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("screen.anchor_forget_failed", error=str(exc))


async def remember_extra(chat_id: int, message: Message) -> None:
    """Дополнительное сообщение, которое надо убрать при следующем переходе."""
    try:
        redis = get_redis()
        await redis.rpush(_extra_key(chat_id), message.message_id)  # type: ignore[misc]
        await redis.expire(_extra_key(chat_id), TTL_SEC)
    except Exception as exc:  # noqa: BLE001
        log.warning("screen.extra_write_failed", error=str(exc))


async def drop_extra(bot: Bot, chat_id: int) -> None:
    try:
        redis = get_redis()
        ids = await redis.lrange(_extra_key(chat_id), 0, -1)  # type: ignore[misc]
        await redis.delete(_extra_key(chat_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("screen.extra_read_failed", error=str(exc))
        return
    for raw in ids or []:
        if str(raw).isdigit():
            await _delete(bot, chat_id, int(raw))


async def show(
    event: Message | CallbackQuery,
    text: str,
    *,
    markup: InlineKeyboardMarkup | None = None,
    photo: str | None = None,
    drop_incoming: bool = False,
    persist: bool = False,
) -> None:
    """Показать экран: по возможности правкой текущего сообщения.

    Сообщения клиента по умолчанию не трогаем. Раньше их удаляли ради чистоты
    переписки, но в пустом чате это ломало вход: `/start` исчезал, отвечать
    было не на что, и Telegram снова показывал кнопку START. Ленту сдерживает
    замена собственного экрана, а не удаление чужих сообщений.

    `drop_incoming=True` — точечно там, где ответ клиента не должен остаться
    в переписке.

    `persist=True` — сообщение должно остаться в переписке навсегда: ссылка
    на оплату, номер заказа, трек. Такое нельзя затирать следующим экраном,
    клиент вернётся к нему через час и не найдёт.
    """
    context = context_of(event)
    if context is None:
        return
    bot, chat_id = context

    await drop_extra(bot, chat_id)

    if drop_incoming and isinstance(event, Message) and not (event.from_user and event.from_user.is_bot):
        await _delete(bot, chat_id, event.message_id)

    kind = KIND_PHOTO if photo else KIND_TEXT
    anchor = await _load_anchor(chat_id)

    if persist:
        if anchor is not None:
            await _delete(bot, chat_id, anchor[0])
        await forget(chat_id)
        if photo:
            await bot.send_photo(chat_id, photo=photo, caption=text, reply_markup=markup)
        else:
            await bot.send_message(chat_id, text, reply_markup=markup)
        return

    if anchor is not None and anchor[1] == kind:
        message_id = anchor[0]
        try:
            if kind == KIND_TEXT:
                await bot.edit_message_text(
                    text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup
                )
            else:
                await bot.edit_message_caption(
                    chat_id=chat_id, message_id=message_id, caption=text, reply_markup=markup
                )
            return
        except TelegramAPIError as exc:
            # «message is not modified» — экран уже такой, всё в порядке.
            if "not modified" in str(exc).lower():
                return
            log.debug("screen.edit_failed", message_id=message_id, error=str(exc))
            await _delete(bot, chat_id, message_id)
    elif anchor is not None:
        await _delete(bot, chat_id, anchor[0])

    if photo:
        sent = await bot.send_photo(chat_id, photo=photo, caption=text, reply_markup=markup)
    else:
        sent = await bot.send_message(chat_id, text, reply_markup=markup)
    await _save_anchor(chat_id, sent.message_id, kind)


async def remove_reply_keyboard(event: Message | CallbackQuery) -> None:
    """Снять старую reply-клавиатуру.

    Telegram убирает её только вместе с сообщением, поэтому шлём служебное
    и сразу удаляем: клавиатуры не станет, а следа в переписке не останется.
    """
    context = context_of(event)
    if context is None:
        return
    bot, chat_id = context
    try:
        sent = await bot.send_message(chat_id, "⌛", reply_markup=ReplyKeyboardRemove())
    except TelegramAPIError as exc:
        log.debug("screen.keyboard_remove_failed", error=str(exc))
        return
    await _delete(bot, chat_id, sent.message_id)


async def notify(event: Message | CallbackQuery, text: str) -> None:
    """Разовое сообщение рядом с экраном: уйдёт при следующем переходе."""
    await notify_with_keyboard(event, text)


async def notify_with_keyboard(
    event: Message | CallbackQuery, text: str, *, markup: object | None = None
) -> None:
    """То же, но с reply-клавиатурой.

    Отдельным сообщением, потому что reply-клавиатуру нельзя приложить
    к экрану с инлайн-кнопками: Telegram разрешает что-то одно.
    """
    context = context_of(event)
    if context is None:
        return
    bot, chat_id = context
    sent = await bot.send_message(chat_id, text, reply_markup=markup)  # type: ignore[arg-type]
    await remember_extra(chat_id, sent)
