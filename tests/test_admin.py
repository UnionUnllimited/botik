"""Админка: доступ по ролям, редиректы без сессии, аудит и форматирование."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from api.admin.audit import _jsonable, diff
from api.admin.auth import ROLE_SECTIONS, AdminSession
from api.admin.templating import days_until, money, status_label, status_tone
from core.enums import AdminRole, OrderStatus, SubscriptionStatus


@pytest.fixture(scope="module")
def client() -> TestClient:
    from api.main import app

    with TestClient(app) as test_client:
        yield test_client


class TestAccessWithoutSession:
    """Любой раздел без входа отправляет на форму, а не отдаёт данные."""

    @pytest.mark.parametrize(
        "path",
        [
            "/admin/",
            "/admin/orders",
            "/admin/clients",
            "/admin/subscriptions",
            "/admin/devices",
            "/admin/nodes",
            "/admin/catalog",
            "/admin/promo",
            "/admin/settings",
            "/admin/audit",
            "/admin/admins",
        ],
    )
    def test_redirects_to_login(self, client, path):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"

    def test_login_page_renders(self, client):
        response = client.get("/admin/login")
        assert response.status_code == 200
        assert "Вход в админку" in response.text

    def test_login_page_is_not_indexed(self, client):
        response = client.get("/admin/login")
        assert "noindex" in response.text


class TestRoleMatrix:
    def test_owner_sees_everything(self):
        assert "admins" in ROLE_SECTIONS[AdminRole.OWNER]
        assert ROLE_SECTIONS[AdminRole.ADMIN] < ROLE_SECTIONS[AdminRole.OWNER]

    def test_admin_cannot_manage_staff(self):
        assert "admins" not in ROLE_SECTIONS[AdminRole.ADMIN]

    def test_support_has_no_money_settings(self):
        support = ROLE_SECTIONS[AdminRole.SUPPORT]
        assert "clients" in support
        assert "settings" not in support
        assert "catalog" not in support
        assert "promo" not in support

    def test_logist_sees_only_fulfilment(self):
        logist = ROLE_SECTIONS[AdminRole.LOGIST]
        assert logist == {"dashboard", "orders", "devices"}

    @pytest.mark.parametrize("role", list(AdminRole))
    def test_every_role_has_dashboard(self, role):
        assert "dashboard" in ROLE_SECTIONS[role]

    def test_session_permission_check(self):
        session = AdminSession(
            admin_id=1,
            role=AdminRole.SUPPORT.value,
            login="support",
            mfa_passed=True,
            csrf="x",
            ip="1.2.3.4",
            created_at="2026-08-03T12:00:00+00:00",
        )
        assert session.can("clients") is True
        assert session.can("settings") is False


class TestAudit:
    def test_diff_keeps_only_changes(self):
        before = {"price": Decimal("100"), "title": "Роутер", "stock": 5}
        after = {"price": Decimal("120"), "title": "Роутер", "stock": 5}
        old, new = diff(before, after)
        assert old == {"price": Decimal("100")}
        assert new == {"price": Decimal("120")}

    def test_diff_empty_when_nothing_changed(self):
        payload = {"a": 1, "b": "x"}
        assert diff(payload, dict(payload)) == ({}, {})

    def test_decimal_is_stored_as_string(self):
        """В jsonb Decimal ушёл бы float'ом и потерял копейки."""
        assert _jsonable({"amount": Decimal("1990.55")}) == {"amount": "1990.55"}

    def test_datetime_is_isoformat(self):
        value = dt.datetime(2026, 8, 3, 12, tzinfo=dt.UTC)
        assert _jsonable({"at": value})["at"].startswith("2026-08-03T12:00")

    def test_nested_structures(self):
        payload = {"items": [{"price": Decimal("10.00")}, {"price": Decimal("20.50")}]}
        assert _jsonable(payload) == {"items": [{"price": "10.00"}, {"price": "20.50"}]}


class TestFormatting:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(Decimal("6900"), "6 900 ₽"), (Decimal("399.50"), "399,50 ₽"), (None, "—")],
    )
    def test_money(self, value, expected):
        assert money(value) == expected

    def test_status_labels_are_russian(self):
        assert status_label(OrderStatus.AWAITING_PAYMENT) == "ждёт оплаты"
        assert status_label(SubscriptionStatus.GRACE) == "льготный период"

    def test_status_tone_defaults_to_muted(self):
        assert status_tone("что-то новое") == "muted"
        assert status_tone(OrderStatus.PAID) == "ok"

    def test_days_until_marks_overdue(self):
        past = dt.datetime.now(dt.UTC) - dt.timedelta(days=5)
        assert "просрочено" in days_until(past)
        assert days_until(None) == "—"
