"""Граница доступа приложения в Telegram.

Внутри — те же данные, что у бота: заказы, роутер, подписка, платёжные
ссылки. Разница в том, что ходит за ними браузер клиента, а не наш процесс.
Поэтому проверяется ровно одно: без подписи и без допуска внутрь не попасть,
а `tg_id` берётся из подписи и ничем из запроса не перебивается.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.routes import miniapp
from core.config import settings
from core.services.miniapp_auth import InitDataError
from tests.test_miniapp_auth import TOKEN, make_init_data

MINE = 614685408


@pytest.fixture(scope="module")
def client() -> TestClient:
    from api.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings.miniapp, "bot_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings.miniapp, "allowed_tg_ids", [MINE])


@pytest.fixture
def switched_off(monkeypatch):
    monkeypatch.setattr(settings.miniapp, "bot_token", SecretStr(""))
    monkeypatch.setattr(settings.miniapp, "allowed_tg_ids", [])


class TestSwitchedOff:
    """Не настроено — ручек как будто нет."""

    def test_page_is_hidden(self, client, switched_off):
        assert client.get("/app").status_code == 404

    def test_data_is_hidden(self, client, switched_off):
        assert client.get("/app/api/catalog").status_code == 404


class TestEntry:
    def test_page_opens_without_signature(self, client, configured):
        """Страница пустая: подпись появляется только в браузере Telegram."""
        response = client.get("/app")
        assert response.status_code == 200
        assert "telegram-web-app.js" in response.text

    def test_data_without_signature_is_rejected(self, client, configured):
        assert client.get("/app/api/catalog").status_code == 401

    def test_data_with_broken_signature_is_rejected(self, client, configured):
        raw = make_init_data().replace(str(MINE), "999999999")
        response = client.get("/app/api/catalog", headers={"X-Telegram-Init-Data": raw})
        assert response.status_code == 401

    def test_stranger_with_valid_signature_is_refused(self, client, configured):
        """Подпись настоящая, но на обкатке пускаем только своих."""
        raw = make_init_data(user={"id": 555000111, "first_name": "Кто-то"})
        response = client.get("/app/api/catalog", headers={"X-Telegram-Init-Data": raw})
        assert response.status_code == 403


class TestIdentityComesFromSignature:
    @pytest.mark.asyncio
    async def test_allowed_user_is_recognised(self, configured):
        user = await miniapp.current_user(init_data=make_init_data())
        assert user.tg_id == MINE

    @pytest.mark.asyncio
    async def test_payload_tg_id_never_wins(self, configured, monkeypatch):
        """Ключевое место: браузер прислал чужой номер — платим всё равно свой.

        Иначе подменой одного числа в теле запроса можно было бы получить
        платёжную ссылку на чужую подписку.
        """
        seen: dict = {}

        async def fake_renew(*, payload, session):
            seen.update(payload)
            return {"ok": True, "pay_url": "https://example.test/pay"}

        monkeypatch.setattr(miniapp.catalog_api, "renew_start", fake_renew)

        user = await miniapp.current_user(init_data=make_init_data())
        await miniapp.renew_start(
            payload={"plan_id": 1, "tg_id": 999999999}, user=user, session=None
        )

        assert seen["tg_id"] == MINE
        assert seen["plan_id"] == 1

    @pytest.mark.asyncio
    async def test_expired_signature_is_rejected(self, configured):
        import time

        # Ловим широко намеренно: важен код ответа, а не класс исключения.
        with pytest.raises(Exception) as info:
            await miniapp.current_user(
                init_data=make_init_data(auth_date=int(time.time()) - 200000)
            )
        assert getattr(info.value, "status_code", None) == 401

    def test_error_text_explains_itself(self):
        """Отказ должен объяснять причину: иначе первый же тест выглядит поломкой."""
        assert str(InitDataError("Подпись не сошлась"))
