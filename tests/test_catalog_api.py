"""Каталог наружу: доступ по общему секрету, разбор карточки и черновик заказа."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.routes import fleet_api
from api.routes.catalog_api import _draft, _product_payload, _specs
from core.config import settings
from core.enums import DeliveryMethod
from core.models import Product
from core.services.remnawave import RemnaUser

TOKEN = "catalog-token"


@pytest.fixture(scope="module")
def client() -> TestClient:
    from api.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def token(monkeypatch) -> str:
    monkeypatch.setattr(settings.api, "fleet_token", SecretStr(TOKEN))
    return TOKEN


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestAccess:
    """Тот же рубеж, что у парка: без токена ручки как будто нет."""

    def test_disabled_without_token(self, client, monkeypatch):
        monkeypatch.setattr(settings.api, "fleet_token", SecretStr(""))
        assert client.get("/api/v1/catalog/products").status_code == 404

    def test_wrong_token_rejected(self, client, token):
        response = client.post(
            "/api/v1/catalog/validate",
            json={"field": "phone", "value": "89001234567"},
            headers={"Authorization": "Bearer not-the-token"},
        )
        assert response.status_code == 401

    def test_no_header_rejected(self, client, token):
        response = client.post("/api/v1/catalog/validate", json={"field": "city", "value": "Москва"})
        assert response.status_code == 401


class TestFieldValidation:
    """Правила ввода живут у нас: разъехавшись, бот пропустил бы телефон,
    на который потом не дозвонится перевозчик."""

    def test_phone_comes_back_normalized(self, client, token):
        response = client.post(
            "/api/v1/catalog/validate",
            json={"field": "phone", "value": "8 (900) 123-45-67"},
            headers=auth(token),
        )
        assert response.json() == {"ok": True, "value": "+79001234567"}

    def test_name_needs_two_words(self, client, token):
        response = client.post(
            "/api/v1/catalog/validate",
            json={"field": "name", "value": "Иван"},
            headers=auth(token),
        )
        assert response.json()["ok"] is False
        assert response.json()["error"]

    def test_unknown_field_is_a_bug_not_a_refusal(self, client, token):
        """Поле приходит из нашего же кода бота — неизвестное значит опечатку."""
        response = client.post(
            "/api/v1/catalog/validate",
            json={"field": "surname", "value": "Иванов"},
            headers=auth(token),
        )
        assert response.status_code == 422


class TestProductPayload:
    def _product(self) -> Product:
        return Product(
            id=7,
            slug="ax3000",
            title="Роутер AX3000",
            subtitle="",
            description="Описание",
            model_code="AX3000",
            price=Decimal("6900.00"),
            old_price=Decimal("7900.00"),
            stock=3,
            allow_preorder=False,
            is_active=True,
            sort_order=10,
            specs={"Порты": "3 LAN"},
            photo_url="/media/product-7-ab12cd34.jpg",
        )

    def test_money_goes_as_strings(self):
        payload = _product_payload(self._product())
        assert payload["price"] == "6900.00"
        assert payload["old_price"] == "7900.00"

    def test_photo_url_is_absolute(self, monkeypatch):
        """Картинку тянет Telegram, а он ходит снаружи и относительный путь не откроет."""
        monkeypatch.setattr(settings.api, "public_base_url", "https://shop.example/")
        payload = _product_payload(self._product())
        assert payload["photo_url"] == "https://shop.example/media/product-7-ab12cd34.jpg"
        assert payload["photo_path"] == "/media/product-7-ab12cd34.jpg"

    def test_foreign_photo_link_kept_as_is(self, monkeypatch):
        product = self._product()
        product.photo_url = "https://cdn.example/router.jpg"
        assert _product_payload(product)["photo_url"] == "https://cdn.example/router.jpg"

    def test_empty_photo_stays_empty(self):
        product = self._product()
        product.photo_url = None
        assert _product_payload(product)["photo_url"] == ""

    def test_out_of_stock_without_preorder(self):
        product = self._product()
        product.stock = 0
        assert _product_payload(product)["in_stock"] is False


class TestSpecs:
    def test_json_string_from_form(self):
        assert _specs('{"Порты": "3 LAN"}') == {"Порты": "3 LAN"}

    def test_ready_object(self):
        assert _specs({"Wi-Fi": 6}) == {"Wi-Fi": "6"}

    def test_empty_means_no_specs(self):
        assert _specs("") == {}

    @pytest.mark.parametrize("raw", ["не json", "[1, 2]", '"строка"'])
    def test_garbage_is_refused_not_swallowed(self, raw):
        """None — это отказ с объяснением. Пустой словарь молча стёр бы таблицу."""
        assert _specs(raw) is None


class TestDraft:
    def _payload(self, **extra) -> dict:
        base = {
            "product_id": "7",
            "name": "  Иванов   Иван ",
            "phone": "89001234567",
            "city": "Москва",
            "delivery_method": "cdek",
            "address": "Ленина 1, кв 2",
        }
        base.update(extra)
        return base

    def test_contacts_are_cleaned(self):
        draft = _draft(self._payload())
        assert draft.customer_name == "Иванов Иван"
        assert draft.customer_phone == "+79001234567"
        assert draft.product_id == 7

    def test_pvz_address_does_not_leak_into_courier_field(self):
        draft = _draft(self._payload(delivery_to_pvz=True))
        assert draft.pvz_address == "Ленина 1, кв 2"
        assert draft.delivery_address == ""

    def test_courier_address_does_not_leak_into_pvz_field(self):
        draft = _draft(self._payload(delivery_to_pvz=False))
        assert draft.delivery_address == "Ленина 1, кв 2"
        assert draft.pvz_address == ""

    def test_known_delivery_method_parsed(self):
        assert _draft(self._payload()).delivery_method is DeliveryMethod.CDEK

    def test_unknown_delivery_method_drops_delivery(self):
        """Заказ без доставки посчитается, заказ с выдуманным способом — нет."""
        assert _draft(self._payload(delivery_method="teleport")).delivery_method is None

    def test_source_marked_as_bot(self):
        assert _draft(self._payload()).utm_source == "bot"


class TestPanelTrafficMatching:
    """Учётка в панели заводится двумя путями и называется по-разному:
    клиентская активация даёт `tg{id}_{mac}`, ручная из карточки — имя из MAC.
    Искать только по первому значит терять всё, что активировано руками."""

    @pytest.fixture(autouse=True)
    def panel_configured(self, monkeypatch):
        """Без настроенной панели функция выходит раньше — тест бы ничего
        не проверял, а зелёным был."""
        monkeypatch.setattr(
            type(settings.remnawave), "is_configured", property(lambda self: True)
        )

    def _account(self, username, *, telegram_id=0, used=0):
        return RemnaUser(
            uuid="u", username=username, subscription_url="",
            used_traffic_bytes=used, telegram_id=telegram_id,
        )

    @pytest.mark.anyio
    async def test_client_activation_matched_by_telegram_id(self, monkeypatch):
        monkeypatch.setattr(
            fleet_api.remnawave, "client",
            lambda: SimpleNamespace(users=self._users([
                self._account("tg614685408_d40dab283b80", telegram_id=614685408, used=1024)
            ])),
        )
        found = await fleet_api._panel_traffic({"614685408": ["D4:0D:AB:28:3B:80"]})
        assert found["614685408"]["used_bytes"] == 1024

    @pytest.mark.anyio
    async def test_manual_activation_matched_by_mac(self, monkeypatch):
        """Ровно этот случай и не находился: имя из MAC, telegram_id пустой."""
        monkeypatch.setattr(
            fleet_api.remnawave, "client",
            lambda: SimpleNamespace(users=self._users([
                self._account("d4-0d-ab-28-3b-80", used=2048)
            ])),
        )
        found = await fleet_api._panel_traffic({"614685408": ["D4:0D:AB:28:3B:80"]})
        assert found["614685408"]["used_bytes"] == 2048

    @pytest.mark.anyio
    async def test_foreign_account_ignored(self, monkeypatch):
        """В панели живут и чужие учётки — приписать их клиенту нельзя."""
        monkeypatch.setattr(
            fleet_api.remnawave, "client",
            lambda: SimpleNamespace(users=self._users([
                self._account("tg999_aaaa", telegram_id=999, used=4096)
            ])),
        )
        assert await fleet_api._panel_traffic({"614685408": ["D4:0D:AB:28:3B:80"]}) == {}

    def _users(self, accounts):
        async def users():
            return accounts
        return users


class TestStockAccess:
    """Склад закрыт тем же секретом, что и остальной парк."""

    def test_disabled_without_token(self, client, monkeypatch):
        monkeypatch.setattr(settings.api, "fleet_token", SecretStr(""))
        assert client.get("/api/v1/fleet/devices").status_code == 404

    def test_wrong_token_rejected(self, client, token):
        response = client.get(
            "/api/v1/fleet/devices", headers={"Authorization": "Bearer not-the-token"}
        )
        assert response.status_code == 401
