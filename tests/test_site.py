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
