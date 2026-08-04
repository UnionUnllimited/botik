"""Антифлуд: отсекаем мусорные апдейты до того, как они что-то стоят.

Это внешний middleware уровня update, и стоит он раньше сессии БД намеренно.
Раньше порядок был обратный: на каждый апдейт открывалась транзакция и читался
пользователь, и флуд с десятка аккаунтов выедал пул соединений задолго до того,
как срабатывал лимит. Теперь до Postgres доходит только то, что прошло отсев.

Три рубежа, от мягкого к жёсткому:

  * **лимит на пользователя** — обычный человек не жмёт кнопки чаще;
  * **тишина после предупреждения** — на флуд отвечать нельзя, ответ сам по себе
    вызов к Telegram API, то есть усиление атаки;
  * **общий лимит на бота** — защита от роя аккаунтов, каждый из которых
    по отдельности в норме. Лишнее просто отбрасывается.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update, User

from bot.texts import ru
from core.config import settings
from core.metrics import bot_errors_total
from core.redis_client import RateLimiter, get_redis

log = structlog.get_logger("bot.throttling")


def _user_of(event: TelegramObject) -> User | None:
    """Отправитель апдейта. На уровне update aiogram ещё не разложил его по данным."""
    if isinstance(event, Update):
        return event.event.from_user if hasattr(event.event, "from_user") else None
    return getattr(event, "from_user", None)


def _answerable(event: TelegramObject) -> Message | CallbackQuery | None:
    if isinstance(event, Update):
        return event.message or event.callback_query
    if isinstance(event, (Message, CallbackQuery)):
        return event
    return None


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(
        self,
        *,
        limit: int = 20,
        window_sec: int = 10,
        strikes_before_mute: int = 3,
        mute_sec: int = 300,
        global_limit: int = 600,
        global_window_sec: int = 10,
    ) -> None:
        self.limit = limit
        self.window_sec = window_sec
        self.strikes_before_mute = strikes_before_mute
        self.mute_sec = mute_sec
        self.global_limit = global_limit
        self.global_window_sec = global_window_sec

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = _user_of(event)
        if tg_user is None:
            return await handler(event, data)

        if settings.bot.is_admin(tg_user.id):
            return await handler(event, data)

        redis = get_redis()
        limiter = RateLimiter(redis)

        # Замолчавших не проверяем дальше: цель — не тратить на них ничего.
        if await redis.get(self._mute_key(tg_user.id)):
            bot_errors_total.labels(kind="throttled_muted").inc()
            return None

        allowed, _ = await limiter.hit(
            f"tg:{tg_user.id}", limit=self.limit, window_sec=self.window_sec
        )
        if not allowed:
            return await self._punish(redis, event, tg_user)

        # Общий лимит проверяем последним: он должен щадить тех, кто уже прошёл
        # персональный отсев, и срабатывать только на настоящей волне.
        allowed_globally, _ = await limiter.hit(
            "tg:all", limit=self.global_limit, window_sec=self.global_window_sec
        )
        if not allowed_globally:
            bot_errors_total.labels(kind="throttled_global").inc()
            log.warning("bot.throttled_global", tg_id=tg_user.id)
            return None

        return await handler(event, data)

    def _mute_key(self, tg_id: int) -> str:
        return settings.redis.key("throttle_mute", str(tg_id))

    async def _punish(self, redis: Any, event: TelegramObject, tg_user: User) -> None:
        """Считает нарушения и после нескольких подряд замолкает совсем."""
        strikes_key = settings.redis.key("throttle_strikes", str(tg_user.id))
        strikes = int(await redis.incr(strikes_key) or 1)
        await redis.expire(strikes_key, self.mute_sec)

        if strikes >= self.strikes_before_mute:
            await redis.set(self._mute_key(tg_user.id), "1", ex=self.mute_sec)
            log.warning("bot.throttle_muted", tg_id=tg_user.id, seconds=self.mute_sec)
            bot_errors_total.labels(kind="throttled_muted").inc()
            return None

        bot_errors_total.labels(kind="throttled").inc()
        log.warning("bot.throttled", tg_id=tg_user.id, strikes=strikes)

        # Предупреждаем один раз на окно: ответ на каждый флудящий апдейт
        # удвоил бы нагрузку нашими же запросами к Telegram.
        notified = await redis.set(
            settings.redis.key("throttle_notified", str(tg_user.id)),
            "1",
            ex=self.window_sec,
            nx=True,
        )
        if not notified:
            return None

        target = _answerable(event)
        try:
            if isinstance(target, Message):
                await target.answer(ru.THROTTLED)
            elif isinstance(target, CallbackQuery):
                await target.answer(ru.THROTTLED, show_alert=False)
        except Exception as exc:  # noqa: BLE001 — предупреждение не важнее защиты
            log.debug("bot.throttle_notify_failed", error=str(exc))
        return None
