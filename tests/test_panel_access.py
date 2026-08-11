"""Вход в веб-панель роутера по разовому билету из админки бота."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.services import panel_ticket


@pytest.fixture(scope="module")
def client() -> TestClient:
    from api.main import app

    with TestClient(app) as test_client:
        yield test_client


class TestTicketSignature:
    """Подпись проверяется до похода в Redis: подделанный билет не должен
    стоить запроса к базе, а тем более открывать панель."""

    @pytest.mark.anyio
    async def test_forged_ticket_refused(self):
        assert await panel_ticket.redeem("не-наш-билет") is None

    @pytest.mark.anyio
    async def test_empty_ticket_refused(self):
        assert await panel_ticket.redeem("") is None

    @pytest.mark.anyio
    async def test_forged_cookie_gives_no_target(self):
        assert await panel_ticket.load("подделка") is None

    @pytest.mark.anyio
    async def test_no_cookie_gives_no_target(self):
        assert await panel_ticket.load(None) is None


class TestPanelWithoutSession:
    def test_proxy_without_anything_explains_itself(self, client):
        """Оператор пришёл из чужой админки — слать его на нашу форму входа
        незачем, там ему делать нечего."""
        response = client.get("/cgi-bin/luci/", follow_redirects=False)
        assert response.status_code == 409
        assert "Панель закрыта" in response.text

    def test_forged_panel_cookie_does_not_open_anything(self, client):
        response = client.get(
            "/cgi-bin/luci/",
            cookies={panel_ticket.COOKIE: "forged-value"},
            follow_redirects=False,
        )
        assert response.status_code == 409

    def test_our_admin_cookie_still_goes_to_login(self, client):
        """Прежнее поведение для нашей админки не изменилось."""
        response = client.get(
            "/cgi-bin/luci/", cookies={"rs_admin": "expired-value"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"

    def test_open_with_bad_ticket_refuses(self, client):
        response = client.get("/panel/open?ticket=подделка", follow_redirects=False)
        assert response.status_code == 409
        assert panel_ticket.COOKIE not in response.cookies
