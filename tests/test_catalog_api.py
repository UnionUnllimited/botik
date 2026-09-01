"""Каталог наружу: доступ по общему секрету, разбор карточки и черновик заказа."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes import fleet_api
from api.routes.catalog_api import _draft, _product_payload, _specs, manage_orders
from core.config import settings
from core.enums import DeliveryMethod, DeliverySpeed, DeliveryStatus, OrderStatus
from core.models import Delivery, Order, Product, User
from core.models.base import Base
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


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    """SQLite in this focused ORM test stores PostgreSQL JSONB as ordinary JSON."""
    return "JSON"


@pytest.mark.asyncio
async def test_manage_orders_eager_loads_customer_and_delivery() -> None:
    """Serialization must not start an async lazy load after the query has returned."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[User.__table__, Order.__table__, Delivery.__table__],
                )
            )

        session_factory = async_sessionmaker(engine, expire_on_commit=True)
        async with session_factory() as session:
            user = User(tg_id=123, username="buyer")
            order = Order(
                public_number="R-260824-0001",
                user=user,
                status=OrderStatus.PAID,
                subtotal=Decimal("6900.00"),
                discount_total=Decimal("0.00"),
                delivery_price=Decimal("0.00"),
                total=Decimal("6900.00"),
                customer_name="",
                customer_phone="",
                customer_city="",
            )
            order.delivery = Delivery(
                method=DeliveryMethod.CDEK,
                speed=DeliverySpeed.FAST,
                status=DeliveryStatus.NEW,
                city="Самара",
                recipient_name="Иванов Иван",
                recipient_phone="+79001234567",
                price=Decimal("0.00"),
            )
            session.add(order)
            await session.commit()

            result = await manage_orders(status_filter="", q="", page=1, session=session)

        assert result["total"] == 1
        assert result["orders"][0]["customer"] == "@buyer"
        assert result["orders"][0]["customer_telegram"] == "@buyer"
        assert result["orders"][0]["customer_tg_id"] == 123
        assert result["orders"][0]["awaiting_quote"] is True
    finally:
        await engine.dispose()


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

    def test_my_router_availability_is_behind_the_same_gate(self, client, token):
        """Ручку зовёт главное меню на каждой отрисовке — она не должна быть открытой."""
        response = client.get("/api/v1/catalog/my-router/available", params={"tg_id": 1})
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
            "delivery_speed": "fast",
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

    def test_chosen_speed_is_parsed(self):
        """Клиент выбирает скорость, а не перевозчика: его ставит оператор."""
        assert _draft(self._payload()).delivery_speed is DeliverySpeed.FAST
        assert _draft(self._payload(delivery_speed="weekly")).delivery_speed is DeliverySpeed.WEEKLY

    def test_unknown_speed_drops_delivery(self):
        """Заказ оформится без доставки, а не упадёт на опечатке в чужом запросе."""
        assert _draft(self._payload(delivery_speed="teleport")).delivery_speed is None
        assert _draft(self._payload(delivery_speed="")).delivery_speed is None

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
            uid="u", username=username, subscription_url="",
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


class TestSshPasswordAccess:
    """Пароль root отдаётся ручкой, а значит закрыт тем же секретом, что и парк.

    Пароль выводится из MAC, а MAC написан на корпусе: единственное, чем схема
    держится, — что соль знает только основное приложение. Открытая ручка
    отдала бы её следствие любому, кто угадал `device_id`.
    """

    def test_disabled_without_token(self, client, monkeypatch):
        monkeypatch.setattr(settings.api, "fleet_token", SecretStr(""))
        response = client.post("/api/v1/fleet/routers/1/ssh-password")
        assert response.status_code == 404

    def test_wrong_token_rejected(self, client, token):
        response = client.post(
            "/api/v1/fleet/routers/1/ssh-password",
            headers={"Authorization": "Bearer not-the-token"},
        )
        assert response.status_code == 401

    def test_no_header_rejected(self, client, token):
        assert client.post("/api/v1/fleet/routers/1/ssh-password").status_code == 401

    def test_not_exposed_over_get(self, client, token):
        """GET осел бы в истории браузера и в журнале прокси вместе с паролем."""
        response = client.get("/api/v1/fleet/routers/1/ssh-password", headers=auth(token))
        assert response.status_code == 405


class TestRoutersFilters:
    """Фильтры парка проверяются ручкой: сюда приходит что угодно из адреса."""

    def test_unknown_link_value_rejected(self, client, token):
        """Опечатка в адресе не должна молча отдавать весь парк."""
        response = client.get(
            "/api/v1/fleet/routers", params={"link": "onlin"}, headers=auth(token)
        )
        assert response.status_code == 422

    def test_unknown_client_value_rejected(self, client, token):
        response = client.get(
            "/api/v1/fleet/routers", params={"client": "yes"}, headers=auth(token)
        )
        assert response.status_code == 422

    def test_page_below_one_rejected(self, client, token):
        """Нулевая страница дала бы отрицательный offset."""
        response = client.get("/api/v1/fleet/routers", params={"page": 0}, headers=auth(token))
        assert response.status_code == 422

    # Проверки «пустой фильтр разрешён» тут нет намеренно: она доходит
    # до обработчика, а базы в этой сборке тестов не поднимается. Пустое
    # значение разрешено самим шаблоном `^(online|offline)?$`.


class TestPromoManagement:
    """Промокоды каталога заводятся ручкой, а раньше — только запросом в базу."""

    def test_listing_needs_token(self, client, token):
        assert client.get("/api/v1/catalog/manage/promos").status_code == 401

    def test_creating_needs_token(self, client, token):
        response = client.post("/api/v1/catalog/manage/promos", json={"code": "X"})
        assert response.status_code == 401

    def test_disabled_without_token(self, client, monkeypatch):
        monkeypatch.setattr(settings.api, "fleet_token", SecretStr(""))
        assert client.get("/api/v1/catalog/manage/promos").status_code == 404
