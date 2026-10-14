"""Помощь клиенту: «Не открывается сайт», просьба оператору, отзыв.

База — SQLite в памяти, панель и роутер подменены. Стерегутся:

* порядок проверки сайта — от того, что ломает всё (роутер молчит,
  подписка кончилась), к частному (сайта нет в списках);
* просьба не дублируется, а решение оператора доходит до клиента и до
  своего списка;
* отзыв спрашивается один раз, в своё окно, и чужую оценку не поставить.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi import HTTPException
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core import texts
from core.enums import SubscriptionStatus
from core.models import (
    Device,
    DeviceEvent,
    ManualList,
    ManualListRevision,
    Notification,
    Plan,
    RouterReview,
    SiteRequest,
    Subscription,
    User,
)
from core.models.base import Base
from core.services import reviews, router_topics, site_help

NOW = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.UTC)


@compiles(JSONB, "sqlite")
def _jsonb(_type, _compiler, **_kwargs):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint(_type, _compiler, **_kwargs):
    return "INTEGER"


TABLES = [
    User.__table__,
    Plan.__table__,
    Device.__table__,
    DeviceEvent.__table__,
    Subscription.__table__,
    SiteRequest.__table__,
    RouterReview.__table__,
    Notification.__table__,
    ManualList.__table__,
    ManualListRevision.__table__,
]


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


@pytest.fixture
def topic(monkeypatch):
    """Топик «Роутеры»: что туда ушло. Чат и настройки здесь ни при чём."""
    posted: list[dict] = []

    async def _post(_session, text, buttons, *, client_tg_id=0):
        posted.append({"text": text, "buttons": buttons, "kind": "post"})
        return True

    async def _say(_session, text):
        posted.append({"text": text, "buttons": [], "kind": "say"})
        return True

    monkeypatch.setattr(router_topics, "post", _post)
    monkeypatch.setattr(router_topics, "say", _say)
    return posted


async def _client(session, tg_id=7, *, activated_days=30, subscription_days=60):
    user = User(tg_id=tg_id, username=f"client{tg_id}")
    session.add(user)
    await session.flush()
    device = Device(
        mac=f"AA:BB:CC:DD:EE:{tg_id:02d}",
        user_id=user.id,
        frp_online=True,
        activated_at=NOW - dt.timedelta(days=activated_days) if activated_days is not None else None,
    )
    session.add(device)
    await session.flush()
    if subscription_days is not None:
        session.add(
            Subscription(
                user_id=user.id,
                device_id=device.id,
                status=SubscriptionStatus.ACTIVE if subscription_days > 0 else SubscriptionStatus.EXPIRED,
                expires_at=NOW + dt.timedelta(days=subscription_days),
            )
        )
        await session.flush()
    return user, device


def _lookup(covered: bool, size: int = 2000):
    async def _fake(_session, value):
        return {
            "covered": covered,
            "covered_by": [{"entry": value, "origin": ["источник"]}] if covered else [],
            "list_size": size,
        }

    return _fake


# ── Адрес сайта ──────────────────────────────────────────────────────────────


class TestClean:
    def test_link_with_path_and_www(self):
        assert site_help.clean("https://www.YouTube.com/watch?v=1") == ("proxy_domain", "youtube.com")

    def test_short_www_is_kept(self):
        """`www.ru` — сам домен, а не приставка к нему."""
        assert site_help.clean("www.ru")[1] == "www.ru"

    def test_cyrillic_goes_to_punycode_and_back(self):
        _kind, value = site_help.clean("Кинопоиск.РФ")
        assert value.startswith("xn--") and value.endswith(".xn--p1ai")
        assert site_help.shown(value) == "кинопоиск.рф"

    def test_ip(self):
        assert site_help.clean("1.2.3.4") == ("proxy_ip", "1.2.3.4")

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "просто текст",
            "localhost",
            "a..b.com",
            "10.0.0.0/8",
            "-bad-.com",
            # Внутренние адреса: «Добавить» увело бы локальную сеть всего парка в туннель.
            "192.168.1.1",
            "10.0.0.5",
            "127.0.0.1",
            "169.254.1.1",
        ],
    )
    def test_not_a_site(self, bad):
        with pytest.raises(site_help.BadSiteError):
            site_help.clean(bad)

    def test_local_zones(self):
        assert site_help.is_local("gosuslugi.ru")
        assert site_help.is_local(site_help.clean("кинопоиск.рф")[1])
        assert not site_help.is_local("youtube.com")


# ── Проверка ─────────────────────────────────────────────────────────────────


class TestCheck:
    async def test_silent_router_comes_first(self, session, monkeypatch):
        _, device = await _client(session, subscription_days=-5)
        device.frp_online = False
        monkeypatch.setattr(site_help.list_lookup, "lookup", _lookup(False))
        result = await site_help.check(session, device, "youtube.com", now=NOW, read_service=False)
        assert result["verdict"] == site_help.OFFLINE

    async def test_expired_subscription(self, session, monkeypatch):
        _, device = await _client(session, subscription_days=-5)
        monkeypatch.setattr(site_help.list_lookup, "lookup", _lookup(False))
        result = await site_help.check(session, device, "youtube.com", now=NOW, read_service=False)
        assert result["verdict"] == site_help.NO_SUBSCRIPTION

    async def test_panel_date_counts_when_there_is_no_subscription(self, session, monkeypatch):
        """Роутеры, включённые руками за наличные: подписки у нас нет,
        срок есть только в панели — и он живой."""
        _, device = await _client(session, subscription_days=None)
        device.panel_expires_at = NOW + dt.timedelta(days=20)
        monkeypatch.setattr(site_help.list_lookup, "lookup", _lookup(True))
        result = await site_help.check(session, device, "youtube.com", now=NOW, read_service=False)
        assert result["verdict"] == site_help.COVERED

    async def test_switched_off_service(self, session, monkeypatch):
        _, device = await _client(session)

        async def _off(_device):
            return False

        monkeypatch.setattr(site_help, "_service_enabled", _off)
        monkeypatch.setattr(site_help.list_lookup, "lookup", _lookup(True))
        result = await site_help.check(session, device, "youtube.com", now=NOW)
        assert result["verdict"] == site_help.SERVICE_OFF

    async def test_router_that_did_not_answer_about_the_switch_is_not_blamed(self, session, monkeypatch):
        _, device = await _client(session)

        async def _unknown(_device):
            return None

        monkeypatch.setattr(site_help, "_service_enabled", _unknown)
        monkeypatch.setattr(site_help.list_lookup, "lookup", _lookup(False))
        result = await site_help.check(session, device, "example.org", now=NOW)
        assert result["verdict"] == site_help.DIRECT

    async def test_russian_site_is_local(self, session, monkeypatch):
        _, device = await _client(session)
        monkeypatch.setattr(site_help.list_lookup, "lookup", _lookup(False))
        result = await site_help.check(session, device, "gosuslugi.ru", now=NOW, read_service=False)
        assert result["verdict"] == site_help.LOCAL

    async def test_empty_lists_are_unknown_not_direct(self, session, monkeypatch):
        _, device = await _client(session)
        monkeypatch.setattr(site_help.list_lookup, "lookup", _lookup(False, size=0))
        result = await site_help.check(session, device, "example.org", now=NOW, read_service=False)
        assert result["verdict"] == site_help.UNKNOWN


# ── Просьба и решение оператора ──────────────────────────────────────────────


class TestRequest:
    async def test_one_request_per_site(self, session, monkeypatch, topic):
        user, device = await _client(session)
        monkeypatch.setattr(site_help.list_lookup, "lookup", _lookup(False))
        first, created = await site_help.create(session, user, device, "https://example.org/page")
        again, created_again = await site_help.create(session, user, device, "example.org")
        assert created and not created_again
        assert again.id == first.id
        assert len(topic) == 1
        datas = [button.get("data") for button in topic[0]["buttons"]]
        assert f"site:{first.id}:add" in datas and f"site:{first.id}:no" in datas
        assert router_topics.callback(device.id, "dm") in datas

    async def test_note_cannot_draw_fake_lines(self, session, monkeypatch, topic):
        user, device = await _client(session)
        monkeypatch.setattr(site_help.list_lookup, "lookup", _lookup(False))
        request, _ = await site_help.create(
            session, user, device, "example.org", note="не грузит\nРоутер: 00:00:00:00:00:00"
        )
        assert "\n" not in request.note
        lines = topic[0]["text"].splitlines()
        assert [line for line in lines if line.startswith("Роутер:")] == [f"Роутер: {device.mac}"]

    async def test_covered_site_offers_a_talk_not_an_add(self, session, monkeypatch, topic):
        user, device = await _client(session)
        monkeypatch.setattr(site_help.list_lookup, "lookup", _lookup(True))
        request, _ = await site_help.create(session, user, device, "youtube.com")
        datas = [button.get("data") for button in topic[0]["buttons"]]
        assert f"site:{request.id}:done" in datas
        assert f"site:{request.id}:add" not in datas
        assert "уже в списках" in topic[0]["text"]

    async def test_add_puts_it_into_own_list_and_tells_the_client(self, session, monkeypatch, topic):
        user, device = await _client(session)
        monkeypatch.setattr(site_help.list_lookup, "lookup", _lookup(False))
        request, _ = await site_help.create(session, user, device, "example.org")

        assert await site_help.resolve(session, request, "add", author="@oper") == ""
        await session.flush()

        own = await session.scalar(select(ManualList).where(ManualList.kind == "proxy_domain"))
        assert "example.org" in own.body.split()
        assert request.status == site_help.ADDED and request.resolved_by == "@oper"
        letters = list(await session.scalars(select(Notification).where(Notification.tg_id == user.tg_id)))
        assert [letter.text for letter in letters] == [texts.SITE_ADDED.format(site="example.org")]
        # После решения в сообщении не остаётся кнопок решения — второй
        # оператор не нажмёт «добавить» ещё раз.
        datas = [button.get("data") for button in site_help.operator_buttons(request)]
        assert not any(data.startswith("site:") for data in datas)

    async def test_second_press_is_quiet(self, session, monkeypatch, topic):
        user, device = await _client(session)
        monkeypatch.setattr(site_help.list_lookup, "lookup", _lookup(False))
        request, _ = await site_help.create(session, user, device, "example.org")
        await site_help.resolve(session, request, "no")
        assert await site_help.resolve(session, request, "add") == ""
        assert request.status == site_help.DECLINED
        letters = list(await session.scalars(select(Notification)))
        assert len(letters) == 1


# ── Отзыв ────────────────────────────────────────────────────────────────────


class TestReviews:
    async def test_asked_once_in_its_window(self, session):
        _, week = await _client(session, tg_id=11, activated_days=8)
        await _client(session, tg_id=12, activated_days=3)
        await _client(session, tg_id=13, activated_days=20)
        _, done = await _client(session, tg_id=14, activated_days=9)
        session.add(RouterReview(user_id=done.user_id, device_id=done.id, asked_at=NOW))
        await session.flush()

        assert [device.id for device in await reviews.due(session, NOW)] == [week.id]

    async def test_the_message_has_five_stars_in_one_row(self, session):
        user, device = await _client(session, activated_days=8)
        assert await reviews.ask(session, device, NOW)
        review = await session.scalar(select(RouterReview))
        letter = await session.scalar(select(Notification))
        assert letter.tg_id == user.tg_id and letter.text == texts.REVIEW_ASK
        assert [button["start"] for button in letter.buttons] == [
            f"rate_{review.id}_{n}" for n in range(1, 6)
        ]
        assert {button["row"] for button in letter.buttons} == {0}
        assert await reviews.due(session, NOW) == []

    async def test_not_at_night(self, session):
        await _client(session, activated_days=8)
        night = dt.datetime(2026, 10, 14, 0, 30, tzinfo=dt.UTC)  # 03:30 по Москве
        assert await reviews.ask_due(session, night) == 0
        assert await session.scalar(select(RouterReview)) is None

    async def test_blocked_client_is_not_written_but_is_remembered(self, session):
        user, device = await _client(session, activated_days=8)
        user.bot_blocked = True
        assert await reviews.ask(session, device, NOW) is False
        assert await session.scalar(select(Notification)) is None
        assert await reviews.due(session, NOW) == []

    async def test_someone_elses_review(self, session):
        _, device = await _client(session, tg_id=21, activated_days=8)
        await reviews.ask(session, device, NOW)
        review = await session.scalar(select(RouterReview))
        assert await reviews.own_review(session, review.id, 21) is not None
        assert await reviews.own_review(session, review.id, 99) is None

    async def test_low_rating_goes_to_the_operator_with_a_button(self, session, topic):
        _, device = await _client(session, activated_days=8)
        await reviews.ask(session, device, NOW)
        review = await session.scalar(select(RouterReview))
        await reviews.rate(session, review, 2, NOW)
        assert topic[-1]["kind"] == "post"
        assert router_topics.callback(device.id, "dm") in [b["data"] for b in topic[-1]["buttons"]]

        # Передумал — оценка меняется, второй карточки не будет.
        await reviews.rate(session, review, 3, NOW)
        assert review.rating == 3 and len(topic) == 1

    async def test_text_reaches_the_operator_once(self, session, topic):
        """Повторная ссылка и новый текст не засыпают топик: второй раз
        отзыв перезаписывается молча."""
        _, device = await _client(session, activated_days=8)
        await reviews.ask(session, device, NOW)
        review = await session.scalar(select(RouterReview))
        await reviews.rate(session, review, 2, NOW)
        await reviews.add_text(session, review, "плохо")
        await reviews.add_text(session, review, "очень плохо")
        await reviews.add_text(session, review, "совсем плохо")
        assert len(topic) == 2
        assert review.text == "совсем плохо"

    async def test_high_rating_is_a_short_note(self, session, topic):
        _, device = await _client(session, activated_days=8)
        await reviews.ask(session, device, NOW)
        review = await session.scalar(select(RouterReview))
        await reviews.rate(session, review, 5, NOW)
        await reviews.add_text(session, review, "Всё летает, спасибо!")
        assert [item["kind"] for item in topic] == ["say", "say"]
        assert "Всё летает" in topic[-1]["text"]


# ── Ручки ────────────────────────────────────────────────────────────────────


class _Allow:
    async def hit(self, *_args, **_kwargs):
        return True, 0


class TestApi:
    async def test_bad_wifi_password_does_not_touch_the_router(self, session, monkeypatch):
        from api.routes import care_api

        user, device = await _client(session)
        touched = []

        async def _change(*_args):
            touched.append(True)

        monkeypatch.setattr(care_api.router_wifi, "change_password", _change)
        result = await care_api.my_router_wifi_change(
            {"tg_id": user.tg_id, "device_id": device.id, "password": "пароль123"}, session
        )
        assert result["ok"] is False and result["error"] == "refused" and result["message"]
        assert touched == []

    async def test_check_only_answers_without_the_router(self, session, monkeypatch):
        from api.routes import care_api

        user, device = await _client(session)
        result = await care_api.my_router_wifi_change(
            {"tg_id": user.tg_id, "device_id": device.id, "password": "Dacha-2024!", "check_only": True},
            session,
        )
        assert result == {"ok": True, "error": ""}

    async def test_change_is_logged_without_the_password(self, session, monkeypatch):
        from api.routes import care_api
        from core.services import router_wifi

        user, device = await _client(session)

        async def _change(_device, password):
            return [router_wifi.Network("default_radio0", "Titan", "2,4 ГГц", password, False)]

        monkeypatch.setattr(care_api, "RateLimiter", _Allow)
        monkeypatch.setattr(care_api.router_wifi, "change_password", _change)
        result = await care_api.my_router_wifi_change(
            {"tg_id": user.tg_id, "device_id": device.id, "password": "Dacha-2024!"}, session
        )
        assert result["ok"] is True
        events = list(await session.scalars(select(DeviceEvent)))
        assert [event.message for event in events] == ["Клиент сменил пароль Wi-Fi"]
        assert "Dacha" not in events[0].message

    async def test_someone_elses_router(self, session):
        from api.routes import care_api

        await _client(session, tg_id=31)
        _, other = await _client(session, tg_id=32)
        with pytest.raises(HTTPException):
            await care_api.my_router_wifi(tg_id=31, device_id=other.id, session=session)

    async def test_site_request_end_to_end(self, session, monkeypatch, topic):
        from api.routes import care_api

        user, device = await _client(session)
        monkeypatch.setattr(care_api, "RateLimiter", _Allow)
        monkeypatch.setattr(site_help.list_lookup, "lookup", _lookup(False))
        answer = await care_api.my_router_site_request(
            {"tg_id": user.tg_id, "device_id": device.id, "site": "https://example.org/x"}, session
        )
        assert answer["ok"] and answer["created"] and answer["site"] == "example.org"
        # Номер просьбы клиенту не отдаём: с ним проще подделать кнопку оператора.
        assert "id" not in answer

        request = await session.scalar(select(SiteRequest))
        resolved = await care_api.site_request_resolve(
            request.id, {"action": "add", "author": "@op"}, session
        )
        assert resolved["ok"] and resolved["status"] == "added"
        assert "Добавлен в свой список" in resolved["text"]
        assert not any(button.get("data", "").startswith("site:") for button in resolved["buttons"])

    async def test_bad_rating(self, session):
        from api.routes import care_api

        user, device = await _client(session, activated_days=8)
        await reviews.ask(session, device, NOW)
        review = await session.scalar(select(RouterReview))
        assert (await care_api.review_rate(review.id, {"tg_id": user.tg_id, "rating": 9}, session))[
            "ok"
        ] is False
        with pytest.raises(HTTPException):
            await care_api.review_rate(review.id, {"tg_id": 999, "rating": 5}, session)


class TestFixesAfterReview:
    async def test_changing_five_to_two_reaches_the_operator(self, session, topic):
        _, device = await _client(session, activated_days=8)
        await reviews.ask(session, device, NOW)
        review = await session.scalar(select(RouterReview))
        await reviews.rate(session, review, 5, NOW)
        await reviews.rate(session, review, 2, NOW)
        assert [item["kind"] for item in topic] == ["say", "post"]

    async def test_kicked_from_the_work_chat_is_not_a_blocked_client(self, session):
        """Карточка роутера несёт `tg_id` клиента. Если бота выгнали из рабочего
        чата, клиент не должен стать «закрывшимся от бота»."""
        from api.routes import catalog_api

        user, _ = await _client(session)
        card = Notification(
            tg_id=user.tg_id, text="карточка", buttons=[], kind="router_topic", chat_id=-100500
        )
        letter = Notification(tg_id=user.tg_id, text="письмо", buttons=[], kind="reminder")
        session.add_all([card, letter])
        await session.flush()

        await catalog_api.outbox_ack(card.id, {"ok": False, "blocked": True}, session)
        await session.refresh(user)
        assert user.bot_blocked is False

        await catalog_api.outbox_ack(letter.id, {"ok": False, "blocked": True}, session)
        await session.refresh(user)
        assert user.bot_blocked is True

    async def test_repeat_request_does_not_repeat_the_event(self, session, monkeypatch, topic):
        from api.routes import care_api

        user, device = await _client(session)
        monkeypatch.setattr(care_api, "RateLimiter", _Allow)
        monkeypatch.setattr(site_help.list_lookup, "lookup", _lookup(False))
        for _ in range(3):
            await care_api.my_router_site_request(
                {"tg_id": user.tg_id, "device_id": device.id, "site": "example.org"}, session
            )
        events = list(await session.scalars(select(DeviceEvent)))
        assert len(events) == 1


class TestBotButton:
    def test_start_payload_and_row(self):
        from core.notifications import bot_button

        assert bot_button("Продлить", "renew") == {"text": "Продлить", "start": "renew"}
        assert bot_button("5 ★", "rate_3_5", row=0)["row"] == 0

    @pytest.mark.parametrize("bad", ["", "a b", "x" * 65, "renew?x=1", "привет"])
    def test_bad_payload(self, bad):
        from core.notifications import bot_button

        with pytest.raises(ValueError):
            bot_button("x", bad)

    async def test_buttons_are_stored_as_given(self, session):
        from core.notifications import bot_button, send_message

        await send_message(
            7, "Подписка кончается", buttons=[bot_button("Продлить", "renew")], session=session
        )
        await session.flush()
        letter = await session.scalar(select(Notification))
        assert letter.buttons == [{"text": "Продлить", "start": "renew"}]

    def test_reminders_carry_the_renew_link(self):
        """Кнопка под напоминанием — ссылка `/start renew`, её ждёт бот."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "worker" / "tasks" / "subscriptions.py").read_text(
            encoding="utf-8"
        )
        body = source[source.index("async def send_reminders") :]
        body = body[: body.index("\nasync def ")]
        assert "bot_button(ru.RENEW_BUTTON, RENEW_PAYLOAD)" in body
        assert 'RENEW_PAYLOAD = "renew"' in source
