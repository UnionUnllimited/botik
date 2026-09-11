"""Сторож очереди сообщений клиентам.

Своего бота у нас нет: мы кладём текст в `notifications`, а забирает и
отправляет его бот стороннего продукта. Пока он забирает — всё хорошо.
Перестанет — встанет вся переписка разом: напоминания о сроке, статусы
заказов, счета на доставку, инструкция к приехавшему роутеру. Заказы при
этом продолжают оформляться, деньги — приходить, и снаружи ничего не видно.
Узнать об этом можно было только от клиента, который не дождался ответа.

Сказать об этом самим сообщением нельзя: оно уйдёт в ту же вставшую
очередь и будет ждать вместе с остальными. Поэтому сторож говорит двумя
способами, которые от бота не зависят: числом в метриках и записью уровня
«ошибка» в журнале — её подхватывает Sentry, если он включён.

Сообщение оператору всё-таки ставим: оно придёт, когда бот вернётся, и
скажет, сколько он молчал. Это не тревога, а отчёт — но лучше, чем ничего.
"""

from __future__ import annotations

import structlog
from sqlalchemy import func, select

from core.dates import ensure_utc, utcnow
from core.db import session_scope
from core.metrics import outbox_oldest_seconds, outbox_pending
from core.models import Notification
from core.notifications import OUTBOX_MAX_ATTEMPTS, notify_admins

log = structlog.get_logger("worker.outbox")

STUCK_AFTER_MIN = 20
"""Сколько сообщение может ждать, прежде чем это считается простоем.

Бот забирает пачку раз в несколько секунд, так что в норме очередь пуста.
Двадцать минут — это заведомо не задержка, а остановка, и при этом запас на
перезапуск и на выкат: поднимать из-за них тревогу не хочется.
"""

ALERT_KIND = "outbox_stuck"
"""Отдельная метка у тревоги — чтобы узнать свою же в очереди.

Сторож ходит по кругу, а очередь стоит: без метки он подкладывал бы в неё
новую тревогу каждые четверть часа, и вернувшийся бот вывалил бы оператору
десяток одинаковых сообщений подряд.
"""


def _waiting():
    """Сообщения, которые ещё ждут бота.

    Исчерпавшие попытки лежат в той же таблице, но ждут не бота, а человека:
    они уже не уйдут никогда. Считать их простоем значит поднимать тревогу
    из-за одного клиента, заблокировавшего бота полгода назад.
    """
    return select(Notification).where(
        Notification.sent_at.is_(None),
        Notification.attempts < OUTBOX_MAX_ATTEMPTS,
    )


async def watch_outbox() -> int:
    """Меряет очередь. Возвращает возраст самого старого сообщения в секундах.

    Ноль — очередь пуста: отправлять нечего либо бот как раз работает.
    """
    now = utcnow()
    async with session_scope() as session:
        waiting = _waiting().subquery()
        pending = int(await session.scalar(select(func.count()).select_from(waiting)) or 0)
        oldest = await session.scalar(select(func.min(waiting.c.created_at)))

        age = int((now - ensure_utc(oldest)).total_seconds()) if oldest else 0
        outbox_pending.set(pending)
        outbox_oldest_seconds.set(age)

        if age < STUCK_AFTER_MIN * 60:
            log.debug("outbox.alive", pending=pending, oldest_sec=age)
            return age

        minutes = age // 60
        log.error("outbox.stuck", pending=pending, oldest_sec=age, minutes=minutes)

        already = await session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.sent_at.is_(None),
                Notification.kind == ALERT_KIND,
            )
        )
        if already:
            return age

        await notify_admins(
            f"⚠️ Сообщения клиентам не уходят {_phrase(minutes)}.\n"
            f"В очереди: {pending}. Проверьте, жив ли бот и ходит ли он за очередью.",
            session=session,
            kind=ALERT_KIND,
        )
    return age


def _phrase(minutes: int) -> str:
    """«36 мин» или «2 ч 15 мин» — в часах читается быстрее."""
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    return f"{hours} ч {rest:02d} мин"
