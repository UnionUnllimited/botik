"""Чужой колбэк доходит до бота через очередь, а не по запросу к нему.

Уведомления об оплате провайдер шлёт на один адрес на мерчанта — наш. Железо
продаём мы, подписку продаёт бот, и его платежи надо передавать ему. Запасного
пути у него для этого провайдера нет: в его коде прямым текстом «Platega
теперь использует только webhook, polling не нужен». Значит не дошедшее
уведомление — это клиент, который заплатил и не получил ничего.

Сначала мы слали колбэк боту HTTP-запросом на `127.0.0.1:8081`. Это
не работало и работать не могло: бот живёт службой на хосте, а мы
в контейнере, где `127.0.0.1` — это мы сами. Публичного адреса у бота нет,
а открывать его вебхуки в интернет нельзя — защита там только заголовками
мерчанта.

Поэтому направление развёрнуто. Мы складываем колбэк в очередь, бот приходит
за ней сам — тем же способом, каким уже забирает очередь сообщений. Это
направление работает всегда и переживает пересоздание контейнера, смену
прокси и переезд адреса.

Ответ провайдеру при этом всё равно 200: повтор нам не поможет — чужой платёж
мы и во второй раз не узнаем, — а вечные повторы он в какой-то момент бросит
совсем. Значит с этой секунды доставка наша забота, и колбэк обязан лежать
в базе раньше, чем провайдер услышит «принято».
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import orjson
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.deps import get_session
from api.routes import webhooks
from core.config import settings
from core.notifications import PARTNER_MAX_ATTEMPTS

ROOT = Path(__file__).resolve().parents[1]
WEBHOOKS = (ROOT / "api" / "routes" / "webhooks.py").read_text(encoding="utf-8")
CATALOG = (ROOT / "api" / "routes" / "catalog_api.py").read_text(encoding="utf-8")
BOT_LOOP = (ROOT / "bot" / "src" / "shop_callbacks.py").read_text(encoding="utf-8")
BOT_API = (ROOT / "bot" / "src" / "shop_api.py").read_text(encoding="utf-8")
BOT_MAIN = (ROOT / "bot" / "main.py").read_text(encoding="utf-8-sig")
WATCH = (ROOT / "worker" / "tasks" / "outbox_watch.py").read_text(encoding="utf-8")

CALLBACK = {
    "id": "tx-77",
    "status": "CONFIRMED",
    "amount": 499,
    "currency": "RUB",
    "payload": "bot-sub-42",
}

BOT_HEADERS = {"X-MerchantId": "bot-merchant", "X-Secret": "bot-secret"}
"""Реквизиты бота у провайдера, если мерчант у него свой."""


@pytest.fixture
def alien_callback(monkeypatch):
    """Колбэк, которого нет у нас в базе, и запись положенного в очередь."""
    queued: list = []

    async def _not_ours(*_args, **_kwargs):
        return None, False

    class _Session:
        def __init__(self) -> None:
            self.commits = 0

        def add(self, item) -> None:
            queued.append(item)

        async def commit(self) -> None:
            self.commits += 1

    session = _Session()

    monkeypatch.setattr(webhooks.payment_service, "handle_webhook", _not_ours)
    monkeypatch.setattr(
        webhooks,
        "get_provider",
        lambda _name: SimpleNamespace(verify_webhook=lambda _h, _b: True),
    )
    monkeypatch.setattr(settings.platega, "allowed_ips", [])

    from api.main import app

    app.dependency_overrides[get_session] = lambda: session
    try:
        yield SimpleNamespace(queued=queued, session=session)
    finally:
        app.dependency_overrides.pop(get_session, None)


def _post(payload: dict, headers: dict[str, str] | None = None) -> httpx.Response:
    from api.main import app

    with TestClient(app) as client:
        return client.post(
            "/webhooks/platega", content=orjson.dumps(payload), headers=headers or {}
        )


class TestTheCallbackGoesIntoTheQueue:
    def test_the_provider_gets_its_200(self, alien_callback):
        """Повторы нам не помогут: чужой платёж мы и во второй раз не узнаем."""
        assert _post(CALLBACK).status_code == 200

    def test_it_is_written_down(self, alien_callback):
        _post(CALLBACK)
        assert alien_callback.queued, "колбэк не попал в очередь"

    def test_the_transaction_is_kept_for_the_operator(self, alien_callback):
        """По ней человек находит платёж у провайдера, если что-то пошло не так."""
        _post(CALLBACK)
        assert alien_callback.queued[0].transaction_id == "tx-77"

    def test_the_body_survives_word_for_word(self, alien_callback):
        """Бот разбирает его сам: всякое наше приведение по дороге станет
        расхождением, заметным на одном платеже из ста."""
        _post(CALLBACK)
        assert orjson.loads(alien_callback.queued[0].body) == CALLBACK

    def test_only_the_headers_that_prove_authenticity_are_kept(self, alien_callback):
        _post(CALLBACK, headers={**BOT_HEADERS, "X-Forwarded-For": "1.2.3.4"})
        kept = {key.lower() for key in alien_callback.queued[0].headers}
        assert "x-merchantid" in kept and "x-secret" in kept
        assert "x-forwarded-for" not in kept

    def test_it_is_saved_before_the_provider_is_answered(self, alien_callback):
        """Провайдер услышал «принято» — повтора не будет. Значит колбэк
        обязан к этому моменту уже лежать в базе."""
        _post(CALLBACK)
        assert alien_callback.session.commits == 1

    def test_nothing_is_sent_anywhere(self):
        """Запрос к боту из контейнера не работает и вернуться не должен."""
        assert "httpx" not in WEBHOOKS
        assert "_forward_to_partner" not in WEBHOOKS


class TestWhenTheBotTradesUnderItsOwnMerchant:
    """Реквизиты в колбэке — бота, а не наши.

    Наша сверка такой колбэк не признаёт, и до очереди дело не доходило:
    401, провайдер несколько раз повторяет и бросает.
    """

    @pytest.fixture
    def partner(self, monkeypatch):
        monkeypatch.setattr(settings.platega, "partner_merchant_id", "bot-merchant")
        monkeypatch.setattr(settings.platega, "partner_secret", SecretStr("bot-secret"))

    @pytest.fixture
    def strangers(self, alien_callback, monkeypatch):
        monkeypatch.setattr(
            webhooks,
            "get_provider",
            lambda _name: SimpleNamespace(verify_webhook=lambda _h, _b: False),
        )
        return alien_callback

    def test_it_is_queued_not_refused(self, strangers, partner):
        assert _post(CALLBACK, headers=BOT_HEADERS).status_code == 200
        assert strangers.queued, "колбэк под мерчантом бота потерян"

    def test_someone_elses_credentials_are_still_refused(self, strangers, partner):
        wrong = {"X-MerchantId": "bot-merchant", "X-Secret": "guessed-secret"}
        assert _post(CALLBACK, headers=wrong).status_code == 401

    def test_unset_partner_changes_nothing(self, strangers, monkeypatch):
        """Мерчант общий — вторая пара не заполнена и ничего не открывает."""
        monkeypatch.setattr(settings.platega, "partner_merchant_id", "")
        monkeypatch.setattr(settings.platega, "partner_secret", SecretStr(""))
        assert _post(CALLBACK, headers=BOT_HEADERS).status_code == 401

    def test_an_empty_header_does_not_match_an_empty_setting(self, strangers, monkeypatch):
        monkeypatch.setattr(settings.platega, "partner_merchant_id", "")
        monkeypatch.setattr(settings.platega, "partner_secret", SecretStr(""))
        assert _post(CALLBACK, headers={"X-MerchantId": "", "X-Secret": ""}).status_code == 401


class TestTheBotCanTakeTheQueue:
    """Ручки, через которые бот забирает колбэки и отчитывается."""

    def test_the_queue_is_offered(self):
        assert '@router.get("/partner-callbacks")' in CATALOG

    def test_delivered_ones_are_not_offered_again(self):
        head = CATALOG.index('@router.get("/partner-callbacks")')
        body = CATALOG[head : CATALOG.index("@router.post", head)]
        assert "delivered_at.is_(None)" in body
        assert "attempts < PARTNER_MAX_ATTEMPTS" in body

    def test_there_is_a_report(self):
        assert '@router.post("/partner-callbacks/{callback_id}/ack")' in CATALOG

    def test_without_a_report_it_comes_back(self):
        """Повтор у бота безопасен — он сверяет статус платежа, — а потерянная
        оплата нет. Поэтому неподтверждённый колбэк обязан вернуться."""
        head = CATALOG.index('@router.post("/partner-callbacks/{callback_id}/ack")')
        body = CATALOG[head : head + 1200]
        assert "item.delivered_at = utcnow()" in body
        assert "item.attempts += 1" in body

    def test_it_gives_up_later_than_messages(self):
        """Не дошло сообщение — клиент не узнал новости. Не дошёл колбэк —
        клиент заплатил и не получил оплаченного."""
        from core.notifications import OUTBOX_MAX_ATTEMPTS

        assert PARTNER_MAX_ATTEMPTS > OUTBOX_MAX_ATTEMPTS


class TestTheBotSideComesAndGets:
    def test_it_asks_our_queue(self):
        assert "/api/v1/catalog/partner-callbacks" in BOT_API

    def test_it_reports_back(self):
        assert "partner_callback_ack" in BOT_API and "partner_callback_ack" in BOT_LOOP

    def test_it_hands_the_callback_to_its_own_handler(self):
        """Разбирать его на стороне бота значило бы завести вторую копию
        логики зачисления — она разъедется с первой и разъедется молча."""
        assert "127.0.0.1:8081/platega/callback" in BOT_LOOP

    def test_a_refusal_is_reported_not_swallowed(self):
        head = BOT_LOOP.index("except Exception")
        assert "partner_callback_ack(callback_id, ok=False" in BOT_LOOP[head : head + 400]

    def test_an_unknown_provider_does_not_loop_forever(self):
        """Молчание вернуло бы его в следующую пачку, и так до конца попыток —
        а причина всё это время осталась бы в нашем коде, а не в связи."""
        assert "неизвестный провайдер" in BOT_LOOP

    def test_the_loop_is_started(self):
        assert "start_partner_callbacks()" in BOT_MAIN

    def test_it_survives_anything(self):
        """Круг не должен умирать от одного отказа: очередь подождёт."""
        head = BOT_LOOP.index("async def callbacks_loop")
        body = BOT_LOOP[head:]
        assert "except asyncio.CancelledError" in body
        assert "await asyncio.sleep(POLL_INTERVAL_SEC)" in body


class TestNobodyForgetsTheQueue:
    """Очередь, за которой никто не приходит, — это неполученная оплата."""

    def test_there_is_a_watchdog(self):
        assert "async def watch_partner_callbacks" in WATCH

    def test_it_is_scheduled(self):
        scheduler = (ROOT / "worker" / "scheduler.py").read_text(encoding="utf-8")
        assert "watch_partner_callbacks" in scheduler

    def test_it_watches_sooner_than_the_message_queue(self):
        """Там встала переписка, здесь — включение оплаченной подписки."""
        stuck = int(re.search(r"PARTNER_STUCK_AFTER_MIN = (\d+)", WATCH).group(1))
        messages = int(re.search(r"STUCK_AFTER_MIN = (\d+)", WATCH).group(1))
        assert stuck < messages

    def test_it_does_not_repeat_itself(self):
        """Сторож ходит по кругу, а очередь стоит: без метки он подкладывал бы
        новую тревогу каждый круг."""
        head = WATCH.index("async def watch_partner_callbacks")
        assert "PARTNER_ALERT_KIND" in WATCH[head:]

    def test_the_alert_says_what_it_costs(self):
        head = WATCH.index("async def watch_partner_callbacks")
        assert "заплатили" in WATCH[head:]


def test_the_dead_setting_is_gone():
    """Адрес бота больше не читается никем: запроса к нему нет."""
    config = (ROOT / "core" / "config.py").read_text(encoding="utf-8")
    assert "partner_callback_url" not in config
    assert "PLATEGA_PARTNER_CALLBACK_URL" not in (ROOT / ".env.example").read_text(encoding="utf-8")
