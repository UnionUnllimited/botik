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
import re

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from src import order_topics, shop_api

POLL_INTERVAL_SEC = 10
"""Напоминания и подтверждения оплаты не срочные до секунды, а частый опрос
чужого сервиса — лишний шум в его логах."""

BATCH = 20


_START = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _url_of(item: dict, username: str) -> str:
    """Адрес кнопки. `start` — ссылка на нас же с командой: имя бота знаем
    только мы, поэтому и ссылку собираем здесь, а не в очереди."""
    if item.get("url"):
        return str(item["url"])
    payload = str(item.get("start") or "")
    if payload and username and _START.match(payload):
        return f"https://t.me/{username}?start={payload}"
    return ""


def _markup(buttons: list[dict], username: str = "") -> InlineKeyboardMarkup | None:
    """Только ссылки: callback обрабатывал бы наш код, а сообщение пришло извне.

    Кнопки с одинаковым `row` встают в один ряд (пять звёзд оценки), без
    него каждая — отдельным рядом, как было всегда."""
    rows: list[list[InlineKeyboardButton]] = []
    by_row: dict[int, list[InlineKeyboardButton]] = {}
    for item in buttons:
        url = _url_of(item, username)
        if not item.get("text") or not url:
            continue
        button = InlineKeyboardButton(text=item["text"], url=url)
        row = item.get("row")
        if isinstance(row, int):
            if row not in by_row:
                by_row[row] = []
                rows.append(by_row[row])
            by_row[row].append(button)
        else:
            rows.append([button])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _username(bot: Bot) -> str:
    try:
        return (await bot.me()).username or ""
    except Exception as exc:  # noqa: BLE001 — без имени ждут только сообщения с кнопкой «открыть бота»
        logger.warning(f"[OUTBOX] имя бота не получено: {exc}")
        return ""


async def deliver_once(bot: Bot) -> int:
    """Одна пачка. Возвращает число отправленных сообщений."""
    data, error = await shop_api.outbox(limit=BATCH)
    if error:
        logger.debug(f"[OUTBOX] очередь недоступна: {error}")
        return 0

    sent = 0
    username = await _username(bot) if data.get("messages") else ""
    for message in data.get("messages", []):
        message_id = message.get("id")
        # `chat_id` заполнен — это карточка заказа в рабочий чат, а не письмо
        # клиенту: там свой топик, свои кнопки и номер топика в отчёте.
        topic = 0
        buttons = message.get("buttons") or []
        if not username and not message.get("chat_id") and any(item.get("start") for item in buttons):
            # Без своего имени ссылку «открыть бота» не собрать, и сообщение
            # ушло бы без кнопок: звёзды оценки или «Продлить» пропали бы
            # навсегда — повторно его никто не пришлёт. Не отчитываемся,
            # и оно вернётся на следующем круге.
            logger.warning(f"[OUTBOX] имя бота неизвестно — {message_id} ждёт следующего круга")
            continue
        try:
            if message.get("chat_id"):
                topic = await order_topics.send_card(bot, message)
            else:
                await bot.send_message(
                    message["tg_id"],
                    message.get("text", ""),
                    reply_markup=_markup(buttons, username),
                    disable_web_page_preview=True,
                )
        except TelegramForbiddenError:
            await shop_api.outbox_ack(message_id, ok=False, error="бот заблокирован", blocked=True)
            logger.info(f"[OUTBOX] клиент {message.get('tg_id')} закрылся от бота")
        except TelegramRetryAfter as exc:
            # Лимит Telegram: остальные из этой пачки тоже не пройдут.
            #
            # Отчёта об отказе не шлём намеренно. Он засчитал бы попытку, а
            # попыток у сообщения пять: полоса лимитов на утренней рассылке
            # съедала бы их одну за другой, и клиент не получал бы «оплата
            # получена» уже никогда. Лимит временный — сообщение просто ждёт
            # следующего круга, и счётчик остаётся нетронутым.
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
