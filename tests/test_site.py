"""Публичный сайт: формы входа, доступ в кабинет и проверка полей.

Страницы, которым нужна база (витрина, кабинет вошедшего), здесь не поднимаются:
в тестовом окружении нет Postgres. Проверяется то, что до базы не доходит —
маршруты, редиректы, разбор форм и валидаторы.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.site.auth import HONEYPOT_FIELD, SESSION_COOKIE, looks_like_bot
from core.validators import PASSWORD_MIN_LENGTH, clean_email, password_problem


@pytest.fixture(scope="module")
def client() -> TestClient:
    from api.main import app

    with TestClient(app) as test_client:
        yield test_client


class TestPages:
    def test_login_page_renders(self, client):
        response = client.get("/login")
        assert response.status_code == 200
        assert "Вход" in response.text

    def test_register_page_renders(self, client):
        response = client.get("/register")
        assert response.status_code == 200
        assert "Регистрация" in response.text

    def test_register_page_has_honeypot(self, client):
        """Приманка должна быть в разметке и быть невидимой для человека."""
        response = client.get("/register")
        assert f'name="{HONEYPOT_FIELD}"' in response.text
        assert 'class="trap"' in response.text

    def test_register_warns_that_recovery_is_missing(self, client):
        """Восстановления пароля нет — человек должен узнать об этом до регистрации."""
        assert "Восстановление пароля пока не работает" in client.get("/register").text

    def test_unknown_page_answers_html_not_json(self, client):
        response = client.get("/nosuchpage", headers={"accept": "text/html"})
        assert response.status_code == 404
        assert "text/html" in response.headers["content-type"]

    def test_webhooks_still_answer_json(self, client):
        """Служебные пути отвечают машинам, даже когда заголовок просит HTML."""
        response = client.get("/webhooks/nosuch", headers={"accept": "text/html"})
        assert "application/json" in response.headers["content-type"]


class TestCabinetAccess:
    def test_cabinet_redirects_without_session(self, client):
        response = client.get("/cabinet", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_cabinet_redirects_with_forged_cookie(self, client):
        """Кука с чужой подписью не пускает: сверяется подпись, а не наличие."""
        response = client.get(
            "/cabinet", cookies={SESSION_COOKIE: "forged.session.value"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_logout_without_session_is_not_an_error(self, client):
        response = client.post("/logout", data={"csrf_token": "x"}, follow_redirects=False)
        assert response.status_code == 303

    def test_activation_needs_a_session(self, client):
        """Активация меняет чужой роутер — без входа её не должно быть даже видно."""
        response = client.post(
            "/cabinet/activate",
            data={"csrf_token": "x", "mac": "A0:B1:C2:D3:E4:F5"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestHoneypot:
    def test_filled_trap_is_a_bot(self):
        assert looks_like_bot({HONEYPOT_FIELD: "ООО Ромашка"}) is True

    def test_empty_trap_is_a_human(self):
        assert looks_like_bot({HONEYPOT_FIELD: ""}) is False
        assert looks_like_bot({}) is False


class TestEmail:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("  Ivan@Mail.RU ", "ivan@mail.ru"),
            ("client@example.co.uk", "client@example.co.uk"),
            ("тест@почта.рф", "тест@почта.рф"),
        ],
    )
    def test_valid_addresses_are_normalized(self, raw, expected):
        assert clean_email(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["", "ivan", "ivan@", "@mail.ru", "ivan@mail", "ivan mail@mail.ru", "a@b.c d"],
    )
    def test_broken_addresses_are_rejected(self, raw):
        assert clean_email(raw) == ""

    def test_too_long_address_is_rejected(self):
        assert clean_email("a" * 250 + "@mail.ru") == ""


class TestPassword:
    def test_short_password_is_rejected(self):
        assert password_problem("a" * (PASSWORD_MIN_LENGTH - 1))

    def test_normal_password_passes(self):
        assert password_problem("правильный-пароль") == ""

    def test_huge_password_is_rejected(self):
        """Длина ограничена не ради стойкости, а чтобы не занимать процессор хешированием."""
        assert password_problem("x" * 5000)

    def test_spaces_only_is_rejected(self):
        assert password_problem(" " * 20)


class TestCabinetDeviceStates:
    """«Роутера нет» и «доступ к роутеру закрыт» — для клиента разные вещи."""

    @staticmethod
    def _page(**context) -> str:
        from api.site.templating import templates

        payload = {
            "request": None,
            "client": object(),
            "csrf_token": "t",
            "current_path": "/cabinet",
            "user": type("U", (), {"email": "client@example.com", "display_name": "client"})(),
            "subscription": None,
            "plan": None,
            "device": None,
            "orders": [],
            "can_activate": False,
            "activated": False,
            "closed": False,
            "online": False,
            "last_seen": None,
            "ok": "",
            "error": "",
        }
        payload.update(context)
        return templates.env.get_template("cabinet.html").render(**payload)

    @staticmethod
    def _device(status):
        from core.models import Device

        return Device(mac="A0:B1:C2:D3:E4:F5", status=status, model="ZBT")

    def test_no_device_says_not_bound(self):
        assert "не привязан" in self._page()

    def test_revoked_device_does_not_claim_it_is_missing(self):
        """Раньше отвязанный роутер показывался как «не привязан» — человек шёл искать
        несуществующую проблему, хотя устройство за ним числится."""
        from core.enums import DeviceStatus

        page = self._page(device=self._device(DeviceStatus.REVOKED), closed=True)
        assert "Доступ к этому роутеру закрыт" in page
        assert "не привязан" not in page
        assert "A0:B1:C2:D3:E4:F5" in page

    def test_blocked_device_shows_the_same_closed_notice(self):
        from core.enums import DeviceStatus

        page = self._page(device=self._device(DeviceStatus.BLOCKED), closed=True)
        assert "Доступ к этому роутеру закрыт" in page

    def test_bound_but_not_activated(self):
        from core.enums import DeviceStatus

        page = self._page(device=self._device(DeviceStatus.ASSIGNED))
        assert "ещё не активирован" in page

    def test_activated_device_shows_readings(self):
        from core.enums import DeviceStatus

        device = self._device(DeviceStatus.ACTIVE)
        device.clients_wifi, device.clients_dhcp = 3, 2
        page = self._page(device=device, activated=True, online=True)
        assert "Устройств в сети" in page
        assert "на связи" in page

    def test_subscription_names_the_router_it_works_on(self):
        """«Подписка активна» без роутера не отвечает на вопрос клиента: у него
        их может быть несколько, и работает она на одном."""
        from core.enums import DeviceStatus, SubscriptionStatus
        from core.models import Subscription

        device = self._device(DeviceStatus.ACTIVE)
        device.id = 4
        page = self._page(
            device=device,
            activated=True,
            subscription=Subscription(user_id=1, device_id=4, status=SubscriptionStatus.ACTIVE),
        )
        assert "A0:B1:C2:D3:E4:F5" in page

    def test_unactivated_subscription_says_so(self):
        from core.enums import SubscriptionStatus
        from core.models import Subscription

        page = self._page(
            subscription=Subscription(user_id=1, device_id=None, status=SubscriptionStatus.PENDING)
        )
        assert "ещё не привязана" in page

    def test_subscription_on_another_router_is_not_passed_off_as_this_one(self):
        """Иначе клиент будет чинить работающий роутер, а платит он за другой."""
        from core.enums import DeviceStatus, SubscriptionStatus
        from core.models import Subscription

        device = self._device(DeviceStatus.ACTIVE)
        device.id = 4
        page = self._page(
            device=device,
            activated=True,
            subscription=Subscription(user_id=1, device_id=9, status=SubscriptionStatus.ACTIVE),
        )
        assert "не привязана к этому роутеру" in page

    def test_manual_access_is_shown_from_the_panel(self):
        """Доступ, выданный вручную, живёт только в панели. Молчать про работающий
        доступ нельзя: клиент решит, что он не оплачен, и пойдёт платить второй раз."""
        import datetime as dt

        from core.enums import DeviceStatus

        device = self._device(DeviceStatus.ACTIVE)
        device.id = 4
        page = self._page(
            device=device,
            activated=True,
            subscription=None,
            panel_expires_at=dt.datetime(2026, 9, 26, tzinfo=dt.UTC),
            panel_active=True,
        )
        assert "Подписки пока нет" not in page
        assert "активна" in page
        assert "26 сентября 2026" in page

    def test_expired_manual_access_says_it_expired(self):
        import datetime as dt

        from core.enums import DeviceStatus

        device = self._device(DeviceStatus.ACTIVE)
        device.id = 4
        page = self._page(
            device=device,
            activated=True,
            subscription=None,
            panel_expires_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
            panel_active=False,
        )
        assert "истекла" in page

    def test_no_access_anywhere_still_says_so(self):
        assert "Подписки пока нет" in self._page(panel_expires_at=None)
