"""Доставка сообщений из очереди основного приложения.

Оно умеет считать, когда кончается подписка и что написать клиенту, но
отправить не может: токен есть только у нас, и клиент разговаривает с нами.
Поэтому оно кладёт готовый текст в очередь, а мы раз в несколько секунд
забираем пачку, отправляем и отчитываемся о каждом сообщении.

Отчёт обязателен: без него оно не узнает, что сообщение дошло, и пришлёт его
снова. Отдельно отмечаем закрывшегося от бота клиента — ему очередь копить
незачем.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from src import order_topics, shop_api

POLL_INTERVAL_SEC = 10
"""Напоминания и подтверждения оплаты не срочные до секунды, а частый опрос
чужого сервиса — лишний шум в его логах."""

BATCH = 20


def _markup(buttons: list[dict]) -> InlineKeyboardMarkup | None:
    """Только ссылки: callback обрабатывал бы наш код, а сообщение пришло извне."""
    rows = [
        [InlineKeyboardButton(text=item["text"], url=item["url"])]
        for item in buttons
        if item.get("text") and item.get("url")
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def deliver_once(bot: Bot) -> int:
    """Одна пачка. Возвращает число отправленных сообщений."""
    data, error = await shop_api.outbox(limit=BATCH)
    if error:
        logger.debug(f"[OUTBOX] очередь недоступна: {error}")
        return 0

    sent = 0
    for message in data.get("messages", []):
        message_id = message.get("id")
        # `chat_id` заполнен — это карточка заказа в рабочий чат, а не письмо
        # клиенту: там свой топик, свои кнопки и номер топика в отчёте.
        topic = 0
        try:
            if message.get("chat_id"):
                topic = await order_topics.send_card(bot, message)
            else:
                await bot.send_message(
                    message["tg_id"],
                    message.get("text", ""),
                    reply_markup=_markup(message.get("buttons") or []),
                    disable_web_page_preview=True,
                )
        except TelegramForbiddenError:
            await shop_api.outbox_ack(message_id, ok=False, error="бот заблокирован", blocked=True)
            logger.info(f"[OUTBOX] клиент {message.get('tg_id')} закрылся от бота")
        except TelegramRetryAfter as exc:
            # Лимит Telegram: остальные из этой пачки тоже не пройдут.
            await shop_api.outbox_ack(message_id, ok=False, error=f"retry after {exc.retry_after}")
            logger.warning(f"[OUTBOX] лимит Telegram, ждём {exc.retry_after} с")
            await asyncio.sleep(exc.retry_after)
            break
        except Exception as exc:  # noqa: BLE001 — одно сообщение не должно ронять цикл
            await shop_api.outbox_ack(message_id, ok=False, error=str(exc)[:300])
            logger.warning(f"[OUTBOX] не отправлено {message_id}: {exc}")
        else:
            await shop_api.outbox_ack(message_id, ok=True, thread_id=topic)
            sent += 1

    if sent:
        logger.info(f"[OUTBOX] отправлено сообщений: {sent}")
    return sent


async def outbox_loop(bot: Bot) -> None:
    """Вечный цикл. Любая ошибка — пауза и следующий круг: очередь подождёт."""
    logger.info("[OUTBOX] доставка сообщений из очереди запущена")
    while True:
        try:
            await deliver_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — цикл переживает что угодно
            logger.error(f"[OUTBOX] круг доставки упал: {exc}")
        await asyncio.sleep(POLL_INTERVAL_SEC)


def start_outbox(bot: Bot) -> asyncio.Task:
    return asyncio.create_task(outbox_loop(bot))
