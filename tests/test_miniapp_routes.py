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


class TestWhoIsAllowedAsked:
    """Бот спрашивает у нас, кому открыто приложение, — и не держит копии списка.

    Две копии одного списка однажды разъезжаются, и тогда кнопка приходит
    тому, кого приложение потом не пускает. Список один, живёт здесь.
    """

    @pytest.mark.asyncio
    async def test_allowed_when_in_the_list(self, monkeypatch):
        from api.routes.fleet_api import miniapp_allowed

        monkeypatch.setattr(settings.miniapp, "bot_token", SecretStr(TOKEN))
        monkeypatch.setattr(settings.miniapp, "allowed_tg_ids", [MINE])
        assert await miniapp_allowed(tg_id=MINE) == {"allowed": True, "configured": True}

    @pytest.mark.asyncio
    async def test_stranger_is_refused(self, monkeypatch):
        from api.routes.fleet_api import miniapp_allowed

        monkeypatch.setattr(settings.miniapp, "bot_token", SecretStr(TOKEN))
        monkeypatch.setattr(settings.miniapp, "allowed_tg_ids", [MINE])
        assert (await miniapp_allowed(tg_id=999))["allowed"] is False

    @pytest.mark.asyncio
    async def test_nobody_while_the_app_is_off(self, monkeypatch):
        """Список пуст — приложения нет, и кнопку слать некому."""
        from api.routes.fleet_api import miniapp_allowed

        monkeypatch.setattr(settings.miniapp, "bot_token", SecretStr(TOKEN))
        monkeypatch.setattr(settings.miniapp, "allowed_tg_ids", [])
        answer = await miniapp_allowed(tg_id=MINE)
        assert answer == {"allowed": False, "configured": False}


class TestHandlersAskTheUserForFieldsItHas:
    """Обработчик, спросивший у подписи несуществующее поле, падает на клиенте.

    Ровно это и случилось: ручка узлов брала `user.id`, которого у
    `TelegramUser` нет, — приложение получало 500 и молча прятало блок
    настроек, а выглядело это как «прошивка не отвечает». Проверка
    статическая, потому что динамическая требует поднять каждую ручку
    с живой базой: здесь важно не поведение, а то, что имя вообще
    существует.
    """

    def test_no_handler_reads_a_field_that_does_not_exist(self):
        import ast
        import inspect
        from pathlib import Path

        from core.services.miniapp_auth import TelegramUser

        source = Path(inspect.getfile(miniapp)).read_text(encoding="utf-8")
        asked = {
            node.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "user"
        }

        known = set(dir(TelegramUser))
        assert asked, "Ни одна ручка не читает подпись — проверка потеряла смысл"
        assert asked <= known, f"Нет таких полей у подписи: {sorted(asked - known)}"


class TestRouterSettingsCarryTheSignedIdentity:
    """Узлы и переключатель сервиса — те же права, что у перезагрузки."""

    @pytest.mark.asyncio
    async def test_node_list_asks_for_the_signed_user(self, configured, monkeypatch):
        seen: dict = {}

        async def fake(*, tg_id, device_id, session):
            seen.update(tg_id=tg_id, device_id=device_id)
            return {"ok": True, "nodes": [], "current": "", "enabled": True}

        monkeypatch.setattr(miniapp.catalog_api, "my_router_nodes", fake)

        user = await miniapp.current_user(init_data=make_init_data())
        answer = await miniapp.router_nodes(user=user, session=None)

        assert seen["tg_id"] == MINE
        assert answer["ok"] is True

    @pytest.mark.asyncio
    async def test_chosen_node_goes_with_our_own_id(self, configured, monkeypatch):
        """Номер из тела запроса не должен переключать чужой роутер."""
        seen: dict = {}

        async def fake(*, payload, session):
            seen.update(payload)
            return {"ok": True}

        monkeypatch.setattr(miniapp.catalog_api, "my_router_select_node", fake)

        user = await miniapp.current_user(init_data=make_init_data())
        await miniapp.router_select_node(
            payload={"node_id": "cfg01", "tg_id": 999999999}, user=user, session=None
        )

        assert seen["tg_id"] == MINE
        assert seen["node_id"] == "cfg01"

    @pytest.mark.asyncio
    async def test_service_switch_goes_with_our_own_id(self, configured, monkeypatch):
        seen: dict = {}

        async def fake(*, payload, session):
            seen.update(payload)
            return {"ok": True}

        monkeypatch.setattr(miniapp.catalog_api, "my_router_service", fake)

        user = await miniapp.current_user(init_data=make_init_data())
        await miniapp.router_service(
            payload={"enabled": False, "tg_id": 999999999}, user=user, session=None
        )

        assert seen["tg_id"] == MINE
        assert seen["enabled"] is False
