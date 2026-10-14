"""Отзыв о роутере через неделю после включения.

Раз в полчаса днём воркер находит роутеры, включённые семь–четырнадцать
дней назад, и просит клиента оценить от 1 до 5. Кнопки — ссылки на наш же
бот (`/start rate_<отзыв>_<оценка>`): одно нажатие, без экранов.

Дальше по оценке. Четыре-пять — просим пару слов: из них потом отзывы
на витрину. Один-три — просим рассказать, что не так, и сразу кладём
карточку в топик «Роутеры»: недовольный клиент, которому позвонили в тот
же день, остаётся клиентом, а не пишет отзыв на маркетплейсе.

Спрашиваем один раз на роутер. Не ответил — значит, не хочет; напоминать
о просьбе оценить — навязчивость, которая портит ровно эту оценку.
"""

from __future__ import annotations

import datetime as dt
import html

import structlog
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import texts
from core.dates import to_display, utcnow
from core.models import Device, RouterReview, User
from core.notifications import bot_button, send_message
from core.services import router_topics

log = structlog.get_logger("services.reviews")

ASK_AFTER = dt.timedelta(days=7)
ASK_UNTIL = dt.timedelta(days=14)
"""Окно, в которое спрашиваем. Позже двух недель «как вам первая неделя»
звучит странно, и такой роутер просто остаётся без вопроса — в том числе
весь парк, включённый до появления отзывов."""

DAY_HOURS = range(11, 21)
"""Часы по Москве, когда спрашиваем: в девять утра уведомление от бота
раздражает, в одиннадцать вечера — тем более."""

BATCH = 20
LOW = 3
"""Оценка до трёх включительно — повод для оператора, а не для витрины."""

TEXT_LIMIT = 2000


def stars(rating: int) -> str:
    return "★" * rating + "☆" * (5 - rating)


async def due(session: AsyncSession, now: dt.datetime, *, limit: int = BATCH) -> list[Device]:
    """Роутеры, которые пора спросить."""
    asked = exists().where(RouterReview.device_id == Device.id)
    return list(
        await session.scalars(
            select(Device)
            .where(
                Device.user_id.is_not(None),
                Device.activated_at.is_not(None),
                Device.activated_at <= now - ASK_AFTER,
                Device.activated_at > now - ASK_UNTIL,
                ~asked,
            )
            .order_by(Device.activated_at)
            .limit(limit)
        )
    )


def rating_buttons(review_id: int) -> list[dict]:
    return [bot_button(f"{n} ★", f"rate_{review_id}_{n}", row=0) for n in range(1, 6)]


async def ask(session: AsyncSession, device: Device, now: dt.datetime) -> bool:
    """Просит оценить. Запись заводится и тому, кому написать нельзя:
    иначе каждый круг находил бы его снова."""
    user = await session.get(User, device.user_id) if device.user_id else None
    review = RouterReview(user_id=device.user_id, device_id=device.id, asked_at=now)
    session.add(review)
    await session.flush()
    if user is None or not user.tg_id or user.bot_blocked or user.is_blocked:
        return False
    return await send_message(
        user.tg_id,
        texts.REVIEW_ASK,
        buttons=rating_buttons(review.id),
        session=session,
        kind="review",
    )


async def ask_due(session: AsyncSession, now: dt.datetime | None = None) -> int:
    now = now or utcnow()
    if to_display(now).hour not in DAY_HOURS:
        return 0
    sent = 0
    for device in await due(session, now):
        sent += int(await ask(session, device, now))
    if sent:
        log.info("reviews.asked", count=sent)
    return sent


async def own_review(session: AsyncSession, review_id: int, tg_id: int) -> RouterReview | None:
    """Отзыв этого клиента. Номер приходит из ссылки, а ссылку можно
    набрать руками с чужим номером — поэтому владельца сверяем здесь."""
    return await session.scalar(
        select(RouterReview)
        .join(User, User.id == RouterReview.user_id)
        .where(RouterReview.id == review_id, User.tg_id == tg_id)
    )


def _who(user: User | None, device: Device | None) -> str:
    client = html.escape(user.telegram_name) if user is not None else "клиент"
    return f"{client}" + (f" · роутер {device.mac}" if device is not None else "")


def _buttons(review: RouterReview) -> list[dict]:
    return [{"text": "✉ Написать клиенту", "data": router_topics.callback(review.device_id, "dm")}]


async def rate(
    session: AsyncSession, review: RouterReview, rating: int, now: dt.datetime | None = None
) -> None:
    """Оценка. Можно передумать: вторая ссылка из того же сообщения
    перезаписывает первую — человек промахнулся по звезде, а не голосует.

    Оператору — при первой оценке и ещё раз, только если передумал вниз,
    за порог: «5», исправленная на «2», — тот же повод позвонить, что и «2»
    сразу. Остальные исправления молча."""
    if rating < 1 or rating > 5:
        raise ValueError("rating must be 1..5")
    previous = review.rating
    review.rating = rating
    review.rated_at = now or utcnow()
    if previous is not None and not (previous > LOW >= rating):
        return
    user = await session.get(User, review.user_id)
    device = await session.get(Device, review.device_id)
    head = f"{stars(rating)} <b>Оценка {rating} из 5</b>\n{_who(user, device)}"
    if rating <= LOW:
        await router_topics.post(
            session,
            f"{head}\n\nОценка низкая — клиенту стоит написать. Если он объяснит, "
            "что не так, его слова придут сюда же.",
            _buttons(review),
            client_tg_id=(user.tg_id or 0) if user else 0,
        )
    else:
        await router_topics.say(session, head)
    log.info("reviews.rated", review_id=review.id, rating=rating)


async def add_text(session: AsyncSession, review: RouterReview, text: str) -> None:
    """Слова к оценке. Оператору — один раз: повторная ссылка и новый текст
    перезаписывают отзыв молча, иначе ими можно было бы засыпать топик,
    где живут тревоги «роутер молчит»."""
    first = not review.text
    review.text = (text or "").strip()[:TEXT_LIMIT]
    if not review.text or not first:
        return
    user = await session.get(User, review.user_id)
    device = await session.get(Device, review.device_id)
    rating = review.rating or 0
    body = (
        f"💬 <b>Отзыв{' · ' + stars(rating) if rating else ''}</b>\n{_who(user, device)}\n\n"
        f"«{html.escape(review.text)}»"
    )
    if rating and rating > LOW:
        await router_topics.say(session, body)
    else:
        await router_topics.post(
            session, body, _buttons(review), client_tg_id=(user.tg_id or 0) if user else 0
        )


async def recent(session: AsyncSession, *, limit: int = 100) -> list[dict]:
    rows = list(
        await session.scalars(
            select(RouterReview)
            .where(RouterReview.rated_at.is_not(None))
            .order_by(RouterReview.rated_at.desc())
            .limit(limit)
        )
    )
    users = (
        {
            user.id: user
            for user in await session.scalars(select(User).where(User.id.in_({row.user_id for row in rows})))
        }
        if rows
        else {}
    )
    devices = (
        {
            device.id: device
            for device in await session.scalars(
                select(Device).where(Device.id.in_({row.device_id for row in rows}))
            )
        }
        if rows
        else {}
    )
    items = []
    for row in rows:
        user = users.get(row.user_id)
        device = devices.get(row.device_id)
        items.append(
            {
                "id": row.id,
                "rating": row.rating,
                "text": row.text,
                "rated_at": row.rated_at.isoformat() if row.rated_at else None,
                "client": user.telegram_name if user else "",
                "client_tg_id": (user.tg_id or 0) if user else 0,
                "device_id": row.device_id,
                "mac": device.mac if device else "",
            }
        )
    return items


async def summary(session: AsyncSession) -> dict:
    """Сколько спросили, сколько ответили и средняя — для шапки страницы."""
    rows = list(await session.scalars(select(RouterReview)))
    rated = [row.rating for row in rows if row.rating]
    return {
        "asked": len(rows),
        "rated": len(rated),
        "average": round(sum(rated) / len(rated), 1) if rated else None,
        "low": sum(1 for value in rated if value <= LOW),
        "by_rating": {n: sum(1 for value in rated if value == n) for n in range(1, 6)},
    }
