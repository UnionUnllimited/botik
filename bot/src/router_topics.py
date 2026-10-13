"""Роутеры в рабочем чате: кнопки под карточкой в топике «Роутеры».

Карточку и её кнопки собирает основное приложение (`core/services/router_topics.py`):
она рисуется первым сообщением и обновлением после каждого нажатия, и два
источника однажды показали бы одно, а делали другое. Здесь — только нажатия
`rtr:<роутер>:<действие>`.

Ответ на «Написать клиенту» ловит тот же приём ответов, что у заказов
(`order_topics.on_reply`): второй обработчик всех сообщений группы бот
не вызвал бы — первый забирает их все. Ожидание кладётся в его же память
с действием `rdm`.
"""

from __future__ import annotations

import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from src import order_topics, shop_api

router = Router(name="router_topics")

PREFIX = "rtr"
DM_ACTION = "rdm"
DM_PROMPT = "Напишите сообщение — отправлю его клиенту от имени бота."


async def _refresh(query: CallbackQuery, device_id: int, note: str = "") -> None:
    """Карточка по свежим данным — на месте той же, а не новым сообщением."""
    data, error = await shop_api.router_topic_card(device_id)
    if error:
        await query.answer(error[:190], show_alert=True)
        return
    text = data.get("text", "")
    if note:
        text = f"{note}\n\n{text}"
    try:
        await query.message.edit_text(
            text,
            reply_markup=order_topics.markup(data.get("buttons") or []),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001 — «message is not modified» и подобное
        logger.debug(f"[ROUTER TOPICS] карточка не изменилась: {exc}")


@router.callback_query(F.data.startswith(f"{PREFIX}:"))
async def on_button(query: CallbackQuery) -> None:
    parts = (query.data or "").split(":")
    if len(parts) < 3:
        await query.answer()
        return
    try:
        device_id = int(parts[1])
    except ValueError:
        await query.answer()
        return
    action = parts[2]
    chat_id = query.message.chat.id
    thread_id = query.message.message_thread_id or 0

    if action == "card":
        await query.answer("Проверяю…")
        await _refresh(query, device_id)
        return

    if action == "reboot":
        # Перезагрузка — клиент без связи на пару минут. С телефона кнопку
        # жмут вслепую, поэтому сперва спрашиваем.
        await query.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Да, перезагрузить", callback_data=f"{PREFIX}:{device_id}:rbok")],
                [InlineKeyboardButton(text="‹ Назад", callback_data=f"{PREFIX}:{device_id}:card")],
            ])
        )
        await query.answer("Клиент потеряет связь на пару минут")
        return

    if action == "rbok":
        # Гасим нажатие до похода к роутеру: он идёт по SSH через туннель,
        # и Telegram держал бы кнопку «нажатой» всё это время.
        await query.answer("Перезагружаю…")
        _, error = await shop_api.router_reboot(device_id)
        note = f"Не вышло: {error}" if error else "↻ Перезагрузка отправлена — роутер вернётся через пару минут."
        await _refresh(query, device_id, note)
        return

    if action == "act":
        await query.answer("Пробую включить подписку…")
        data, error = await shop_api.router_retry_activation(device_id)
        if error:
            note = f"Не вышло: {error}"
        elif data.get("activated"):
            note = f"✅ {data.get('message') or 'Подписка включена.'}"
        else:
            note = f"Не включилась: {data.get('message') or 'причина неизвестна'}"
        await _refresh(query, device_id, note)
        return

    if action == "cancel":
        await order_topics._forget(query.bot, (chat_id, thread_id))
        await query.answer("Отменено")
        return

    if action == "dm":
        await order_topics._forget(query.bot, (chat_id, thread_id))
        await query.answer()
        try:
            prompt = await query.bot.send_message(
                chat_id=chat_id,
                text=DM_PROMPT,
                message_thread_id=thread_id or None,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="‹ Отмена", callback_data=f"{PREFIX}:{device_id}:cancel")
                ]]),
            )
        except Exception as exc:  # noqa: BLE001 — оператор должен увидеть причину
            logger.warning(f"[ROUTER TOPICS] подсказка не ушла: {exc}")
            await query.answer(f"Не могу написать в этот чат: {exc}"[:190], show_alert=True)
            return
        order_topics._pending[(chat_id, thread_id)] = (
            DM_ACTION, device_id, time.monotonic(), prompt.message_id, query.message.message_id
        )
        return

    await query.answer()


async def send_to_client(bot: Bot, device_id: int, value: str) -> str:
    """Пишет клиенту роутера от имени бота. Возвращает текст ошибки или пусто."""
    if not value.strip():
        return "Пустое сообщение отправлять не буду."
    data, error = await shop_api.router_topic_card(device_id)
    if error:
        return error
    tg_id = data.get("client_tg_id") or 0
    if not tg_id:
        return "У этого роутера нет клиента в Telegram."
    try:
        await bot.send_message(tg_id, value)
    except TelegramForbiddenError:
        return "Клиент закрылся от бота — написать ему нельзя."
    except Exception as exc:  # noqa: BLE001 — причину должен увидеть оператор
        return f"Не отправилось: {exc}"
    return ""
