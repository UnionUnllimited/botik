"""Заказы в топиках рабочего чата: отправка карточки и работа кнопками.

Оператор работает с телефона. Веб-админка там открывается, но это форма,
в которую надо попасть пальцем, а потом ещё вернуться в список. Здесь иначе:
у каждого заказа свой топик, заказ приходит в него сам, вся работа — кнопками
под карточкой, ввод — обычным ответом в тот же топик.

Что делает этот модуль:
  * создаёт топик, когда основное приложение просит (право на топики есть
    только у бота) и возвращает его номер — иначе заказу завелись бы два;
  * обрабатывает нажатия `ord:<заказ>:<действие>`;
  * ловит ответ оператора текстом, когда кнопка чего-то ждёт.

Чего он не делает: не решает, что написано в карточке. Текст и кнопки
собирает основное приложение — они рисуются и здесь, и при обновлении после
каждого нажатия, и разъехавшись, показывали бы одно, а делали другое.
"""

from __future__ import annotations

import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

from src import shop_api

router = Router(name="order_topics")

PREFIX = "ord"

PENDING_TTL_SEC = 30 * 60
"""Столько ждём ответа на «пришлите трек-номер». Дольше держать нельзя:
оператор давно забыл, что нажимал, и его следующее сообщение в топике
уехало бы в поле, которого он не ждал."""

_pending: dict[tuple[int, int], tuple[str, int, float]] = {}
"""(чат, топик) → (действие, заказ, когда попросили).

В памяти, а не в базе: если бота перезапустили, ожидание правильнее забыть.
Восстановленное после перезапуска, оно приняло бы за трек-номер первое же
сообщение, которое оператор напишет коллеге.
"""

PROMPTS = {
    "track": "Пришлите трек-номер ответом в этот топик.",
    "dlv": "Пришлите цену доставки числом (можно с днями: «450 2-3 дня»).",
    "mac": "Пришлите MAC роутера: A0:B1:C2:D3:E4:F5",
    "note": "Пришлите заметку по заказу.",
    "dm": "Напишите сообщение — отправлю его клиенту от имени бота.",
}

STATUSES = [
    ("packing", "Собираем"),
    ("shipped", "Отправлен"),
    ("delivered", "Доставлен"),
    ("activated", "Активирован"),
    ("cancelled", "Отменён"),
]
"""Куда переводят руками. Порядок обычного хода, отмена последней — чтобы
не нажать её вслепую пальцем на телефоне."""


def markup(buttons: list[dict]) -> InlineKeyboardMarkup | None:
    """Кнопки под карточкой: по одной в ряд — их жмут пальцем.

    Здесь, в отличие от сообщений клиенту, callback допустим: чат наш,
    обработчик наш, и сломанную кнопку видит тот же, кто её чинит.
    """
    rows = []
    for item in buttons:
        text = item.get("text")
        if not text:
            continue
        if item.get("url"):
            rows.append([InlineKeyboardButton(text=text, url=item["url"])])
        elif item.get("data"):
            rows.append([InlineKeyboardButton(text=text, callback_data=item["data"])])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def send_card(bot: Bot, message: dict) -> int:
    """Шлёт карточку заказа. Возвращает номер топика, если создал его.

    Топик создаётся только когда основное приложение прислало название:
    оно же и решает, есть топик у заказа или ещё нет. Решай это мы —
    после перезапуска бота заказ получил бы второй.
    """
    chat_id = message["chat_id"]
    thread_id = message.get("thread_id") or 0
    created = 0

    title = (message.get("topic_title") or "").strip()
    if title and not thread_id:
        topic = await bot.create_forum_topic(chat_id=chat_id, name=title)
        thread_id = topic.message_thread_id
        created = thread_id
        logger.info(f"[TOPICS] заведён топик {thread_id} — {title}")

    await bot.send_message(
        chat_id,
        message.get("text", ""),
        message_thread_id=thread_id or None,
        reply_markup=markup(message.get("buttons") or []),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return created


async def _refresh(query: CallbackQuery, order_id: int, note: str = "") -> None:
    """Перерисовывает карточку по свежим данным.

    Правим то же сообщение, а не шлём новое: топик заказа иначе за день
    зарастает десятком почти одинаковых карточек, и последняя теряется.
    """
    data, error = await shop_api.order_topic_card(order_id)
    if error:
        await query.answer(error[:190], show_alert=True)
        return
    text = data.get("text", "")
    if note:
        text = f"{note}\n\n{text}"
    try:
        await query.message.edit_text(
            text,
            reply_markup=markup(data.get("buttons") or []),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001 — «message is not modified» и подобное
        logger.debug(f"[TOPICS] карточка не изменилась: {exc}")


@router.callback_query(F.data.startswith(f"{PREFIX}:"))
async def on_button(query: CallbackQuery) -> None:
    """Нажатие под карточкой заказа."""
    parts = (query.data or "").split(":")
    if len(parts) < 3:
        await query.answer()
        return
    _, raw_id, action = parts[0], parts[1], parts[2]
    try:
        order_id = int(raw_id)
    except ValueError:
        await query.answer()
        return

    chat_id = query.message.chat.id
    thread_id = query.message.message_thread_id or 0

    if action == "card":
        await _refresh(query, order_id)
        await query.answer("Обновлено")
        return

    if action == "status":
        # Список статусов теми же кнопками: выпадающих списков в чате нет,
        # а набирать статус текстом с телефона — верный способ опечататься.
        rows = [
            [InlineKeyboardButton(text=title, callback_data=f"{PREFIX}:{order_id}:st:{code}")]
            for code, title in STATUSES
        ]
        rows.append([InlineKeyboardButton(text="‹ Назад", callback_data=f"{PREFIX}:{order_id}:card")])
        await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await query.answer()
        return

    if action == "st" and len(parts) > 3:
        _, error = await shop_api.set_order_status(order_id, parts[3], "")
        if error:
            await query.answer(error[:190], show_alert=True)
            return
        await _refresh(query, order_id)
        await query.answer("Статус изменён")
        return

    if action in PROMPTS:
        _pending[(chat_id, thread_id)] = (action, order_id, time.monotonic())
        # Гасим нажатие до отправки подсказки. Не ответишь на callback —
        # Telegram держит кнопку «нажатой» до таймаута, и оператор смотрит
        # на крутящийся кружок, не понимая, дошло или нет. А отправка ниже
        # может и не пройти: прав на запись в топик может не быть.
        await query.answer()
        try:
            await query.message.answer(PROMPTS[action], message_thread_id=thread_id or None)
        except Exception as exc:  # noqa: BLE001 — оператор должен увидеть причину
            _pending.pop((chat_id, thread_id), None)
            logger.warning(f"[TOPICS] подсказка не ушла: {exc}")
            await query.answer(f"Не могу написать в этот чат: {exc}"[:190], show_alert=True)
        return

    await query.answer()


async def _send_to_client(bot: Bot, order_id: int, value: str) -> str:
    """Пишет клиенту от имени бота. Возвращает текст ошибки или пусто.

    Именно от бота, а не из личного аккаунта оператора: клиент разговаривает
    с ботом, и сообщение от незнакомого человека он в лучшем случае
    не узнает. Заодно оператор не светит свой аккаунт каждому покупателю.
    """
    if not value.strip():
        return "Пустое сообщение отправлять не буду."

    data, error = await shop_api.order_topic_card(order_id)
    if error:
        return error
    tg_id = data.get("client_tg_id") or 0
    if not tg_id:
        return "У этого заказа нет клиента в Telegram."

    try:
        await bot.send_message(tg_id, value)
    except TelegramForbiddenError:
        return "Клиент закрылся от бота — написать ему нельзя."
    except Exception as exc:  # noqa: BLE001 — причину должен увидеть оператор
        return f"Не отправилось: {exc}"
    return ""


async def _apply_text(action: str, order_id: int, value: str) -> str:
    """Выполняет то, чего ждала кнопка. Возвращает текст ошибки или пусто."""
    if action == "track":
        _, error = await shop_api.set_order_tracking(order_id, value)
        return error
    if action == "note":
        _, error = await shop_api.set_order_note(order_id, value)
        return error
    if action == "mac":
        _, error = await shop_api.attach_order_device(order_id, value, "")
        return error
    if action == "dlv":
        # «450 2-3 дня»: первое слово — цена, остальное — срок. Разбираем
        # здесь, а не просим двумя сообщениями: с телефона это два лишних шага.
        price, _, days = value.partition(" ")
        _, error = await shop_api.quote_delivery(order_id, price.strip(), days.strip())
        return error
    return "Неизвестное действие."


@router.message(F.text, F.chat.type.in_({"group", "supergroup"}))
async def on_reply(message: Message) -> None:
    """Ответ оператора в рабочем чате — если карточка чего-то ждала.

    Ничего не ждала — молчим: в топике заказа люди и просто переписываются,
    и бот, отвечающий на каждую реплику, оттуда всех выгонит.

    Отбор по чату, а не по топику. Раньше стоял фильтр `F.message_thread_id`,
    и он отсекал главную тему форума: там `message_thread_id` пуст, а карточка
    попадает туда всякий раз, когда топик завести не вышло. Оператор жал
    «Трек-номер», получал подсказку — и его ответ до бота уже не доходил.
    """
    key = (message.chat.id, message.message_thread_id or 0)
    waiting = _pending.get(key)
    if waiting is None:
        return

    action, order_id, asked_at = waiting
    if time.monotonic() - asked_at > PENDING_TTL_SEC:
        _pending.pop(key, None)
        return
    _pending.pop(key, None)

    value = (message.text or "").strip()
    if action == "dm":
        # Ответ клиенту не меняет заказ, поэтому карточку не перерисовываем:
        # вместо неё отмечаем, что письмо ушло, — иначе оператор не поймёт,
        # отправилось оно или он написал в пустоту.
        error = await _send_to_client(message.bot, order_id, value)
        await message.reply(f"Не вышло: {error}" if error else "Отправлено клиенту.")
        return

    error = await _apply_text(action, order_id, value)
    if error:
        await message.reply(f"Не вышло: {error}")
        return

    data, card_error = await shop_api.order_topic_card(order_id)
    if card_error:
        await message.reply("Готово.")
        return
    await message.answer(
        data.get("text", ""),
        message_thread_id=message.message_thread_id,
        reply_markup=markup(data.get("buttons") or []),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
