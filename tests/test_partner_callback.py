"""Чужой колбэк, который не дошёл до бота, обязан позвать оператора.

Уведомления об оплате провайдер шлёт на один адрес на мерчанта — наш. Железо
продаём мы, подписку продаёт бот, поэтому его платежи мы передаём ему сами.
Запасного пути у него для этого провайдера нет: в коде бота прямым текстом
«Platega теперь использует только webhook, polling не нужен». Значит не
дошедшее уведомление — это клиент, который заплатил и не получил ничего.

Ответ провайдеру при этом всё равно 200: иначе он будет слать повторы, а
принять их нам нечем — чужой платёж мы всё так же не узнаем. Единственный
верный ход — ответить и позвать человека.

Раньше о такой пропаже оставалась строка `webhook.forward_failed` в журнале.
Журнал никто не читает, а подписка не включалась молча.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import orjson
import pytest
from fastapi.testclient import TestClient

from api.deps import get_session
from api.routes import webhooks
from core import texts
from core.config import settings

CALLBACK = {
    "id": "tx-77",
    "status": "CONFIRMED",
    "amount": 499,
    "currency": "RUB",
    "payload": "bot-sub-42",
}


class _Answer:
    """Ответ бота на пересылку."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _Client:
    """Подставной httpx: отдаёт заготовленный ответ или роняет заготовленную ошибку."""

    def __init__(self, answer: _Answer | Exception) -> None:
        self.answer = answer
        self.sent: list[tuple[str, bytes, dict[str, str]]] = []

    def __call__(self, *_args, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def post(self, url, content=None, headers=None):
        self.sent.append((url, content, headers or {}))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def bot_at(monkeypatch):
    """Ставит адрес бота и подменяет ходока к нему."""

    def _setup(answer: _Answer | Exception, url: str = "http://127.0.0.1:8081/platega/callback"):
        monkeypatch.setattr(settings.platega, "partner_callback_url", url)
        client = _Client(answer)
        monkeypatch.setattr(webhooks.httpx, "AsyncClient", client)
        return client

    return _setup


class TestForwarding:
    @pytest.mark.asyncio
    async def test_a_delivered_callback_is_silent(self, bot_at):
        bot_at(_Answer(200))
        assert await webhooks._forward_to_partner(b"{}", {}) == ""

    @pytest.mark.asyncio
    async def test_an_unset_address_is_a_reason(self, bot_at, monkeypatch):
        monkeypatch.setattr(settings.platega, "partner_callback_url", "  ")
        assert await webhooks._forward_to_partner(b"{}", {}) != ""

    @pytest.mark.asyncio
    async def test_a_silent_bot_is_a_reason(self, bot_at):
        """Бот в контейнер не виден, перезапускается, лежит — снаружи одинаково."""
        bot_at(httpx.ConnectError("connection refused"))
        assert "ConnectError" in await webhooks._forward_to_partner(b"{}", {})

    @pytest.mark.asyncio
    async def test_a_refusing_bot_is_a_reason(self, bot_at):
        """Ответ бота — единственное подтверждение, что он уведомление принял."""
        bot_at(_Answer(500))
        assert "500" in await webhooks._forward_to_partner(b"{}", {})

    @pytest.mark.asyncio
    async def test_the_body_goes_over_untouched(self, bot_at):
        """Подпись бот проверяет сам по тому же телу — менять его нельзя."""
        client = bot_at(_Answer(200))
        body = orjson.dumps(CALLBACK)
        await webhooks._forward_to_partner(body, {"Content-Type": "application/json"})
        assert client.sent[0][1] == body

    @pytest.mark.asyncio
    async def test_only_the_headers_that_prove_authenticity_travel(self, bot_at):
        client = bot_at(_Answer(200))
        await webhooks._forward_to_partner(
            b"{}",
            {
                "Content-Type": "application/json",
                "X-MerchantId": "m-1",
                "X-Secret": "s-1",
                "Host": "titanvps.pro",
                "Content-Length": "2",
            },
        )
        assert set(client.sent[0][2]) == {"Content-Type", "X-MerchantId", "X-Secret"}


@pytest.fixture
def alien_callback(monkeypatch):
    """Колбэк, которого нет у нас в базе, и запись позванных операторов."""
    called: list[str] = []

    async def _not_ours(*_args, **_kwargs):
        return None, False

    async def _remember(text, **_kwargs):
        called.append(text)

    class _Session:
        def __init__(self) -> None:
            self.commits = 0

        async def commit(self) -> None:
            self.commits += 1

    session = _Session()

    monkeypatch.setattr(webhooks.payment_service, "handle_webhook", _not_ours)
    monkeypatch.setattr(webhooks, "notify_admins", _remember)
    monkeypatch.setattr(
        webhooks,
        "get_provider",
        lambda _name: SimpleNamespace(verify_webhook=lambda _h, _b: True),
    )
    monkeypatch.setattr(settings.platega, "allowed_ips", [])

    from api.main import app

    app.dependency_overrides[get_session] = lambda: session
    try:
        yield SimpleNamespace(alerts=called, session=session)
    finally:
        app.dependency_overrides.pop(get_session, None)


def _post(payload: dict) -> httpx.Response:
    from api.main import app

    with TestClient(app) as client:
        return client.post("/webhooks/platega", content=orjson.dumps(payload))


class TestWhenItDoesNotReachTheBot:
    def test_the_operator_is_called(self, alien_callback, bot_at):
        bot_at(httpx.ConnectError("connection refused"))
        _post(CALLBACK)
        assert alien_callback.alerts, "оплата пропала, и никто об этом не узнал"

    def test_the_alert_names_the_transaction(self, alien_callback, bot_at):
        """По ней оператор найдёт платёж у провайдера и включит подписку руками."""
        bot_at(httpx.ConnectError("connection refused"))
        _post(CALLBACK)
        assert "tx-77" in alien_callback.alerts[0]

    def test_the_alert_is_saved(self, alien_callback, bot_at):
        """Сообщение оператору — строка в очереди: без коммита оно исчезнет."""
        bot_at(httpx.ConnectError("connection refused"))
        _post(CALLBACK)
        assert alien_callback.session.commits == 1

    def test_the_provider_still_gets_its_200(self, alien_callback, bot_at):
        """Повторы нам не помогут: чужой платёж мы и во второй раз не узнаем."""
        bot_at(httpx.ConnectError("connection refused"))
        assert _post(CALLBACK).status_code == 200

    def test_a_delivered_callback_bothers_nobody(self, alien_callback, bot_at):
        bot_at(_Answer(200))
        assert _post(CALLBACK).status_code == 200
        assert alien_callback.alerts == []
        assert alien_callback.session.commits == 0


class TestTheAlertItself:
    def test_it_renders_without_the_amount(self):
        """В колбэке сумма необязательна — текст не должен из-за этого упасть."""
        assert texts.ADMIN_PARTNER_CALLBACK_LOST.format(
            transaction="tx-1", amount="—", status="CONFIRMED", reason="бот не ответил"
        )

    def test_it_says_what_to_do(self):
        """Оператору нужно действие, а не констатация."""
        assert "вручную" in texts.ADMIN_PARTNER_CALLBACK_LOST

    def test_it_names_the_setting_to_check(self):
        assert "PLATEGA_PARTNER_CALLBACK_URL" in texts.ADMIN_PARTNER_CALLBACK_LOST
