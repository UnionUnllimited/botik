"""Роутеры в рабочем чате: карточка, когда нужна рука, и «вернулся», когда нет.

Поводы те же, что в утренней сводке и на главной, — их считает тот же
`monitoring.collect`: роутер у клиента молчит больше суток, роутер на связи,
а оплаченная подписка на нём не включилась. Сводка о них говорит раз в сутки
списком; здесь — карточка с кнопками сразу, и отдельное сообщение, когда
повод прошёл.

О каждом поводе — один раз: отметка `chat_alert_kind` у роутера. Без неё
молчащий роутер слал бы карточку каждые десять минут, и топик перестали бы
читать через день.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select

from core.dates import ensure_utc, utcnow
from core.db import session_scope
from core.models import Device
from core.services import monitoring, router_topics

log = structlog.get_logger("worker.router_alerts")


def _phrase(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    return f"{hours} ч" if hours < 48 else f"{hours // 24} дн"


async def watch_router_alerts() -> int:
    """Один круг. Возвращает, сколько сообщений поставлено в очередь."""
    now = utcnow()
    queued = 0
    async with session_scope() as session:
        # Сначала — кто вернулся: отметку снимаем раньше, чем ставим новые,
        # иначе роутер, вернувшийся и снова пропавший между кругами, остался
        # бы со старой отметкой и о второй пропаже никто бы не узнал.
        for device in await session.scalars(select(Device).where(Device.chat_alert_kind.is_not(None))):
            since = (
                int((now - ensure_utc(device.chat_alert_at)).total_seconds() // 60)
                if device.chat_alert_at
                else 0
            )
            if device.chat_alert_kind == router_topics.SILENT and router_topics.is_online(device, now):
                text = f"✅ <b>{device.mac}</b> снова на связи. Тревога висела {_phrase(since)}."
            elif device.chat_alert_kind == router_topics.STUCK and device.activated_at is not None:
                text = f"✅ <b>{device.mac}</b>: подписка включилась."
            elif device.user_id is None:
                # Роутер отвязали или сбросили на склад — повод снят руками.
                text = ""
            else:
                continue
            if text:
                await router_topics.say(session, text)
                queued += 1
            device.chat_alert_kind = None
            device.chat_alert_at = None

        digest = await monitoring.collect(session, now=now)
        found = [(device, router_topics.SILENT) for device in digest.silent if device.user_id]
        found += [(device, router_topics.STUCK) for _order, device in digest.stuck]
        for device, kind in found:
            if device.chat_alert_kind == kind:
                continue
            if await router_topics.push(session, device) is None:
                # Чат не задан или топик ещё заводится — попробуем следующим кругом.
                continue
            device.chat_alert_kind = kind
            device.chat_alert_at = now
            queued += 1

    if queued:
        log.info("router_alerts.queued", count=queued)
    return queued
