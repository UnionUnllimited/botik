"""Живой терминал роутера: вход по разовому билету и его границы.

Терминал открывает root-сессию на устройстве клиента, и запрет опасных команд,
который стоит у разовых, здесь не работает — собрать «строку команды» из
отдельных нажатий нельзя. Значит вся защита в том, как сюда попадают:
подписанный билет, короткая сессия, никакого входа без неё.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.services import terminal_ticket


@pytest.fixture(scope="module")
def client() -> TestClient:
    from api.main import app

    with TestClient(app) as test_client:
        yield test_client


class TestTicketSignature:
    """Подпись проверяется до похода в Redis: подделанный билет не должен
    стоить запроса к базе, а тем более открывать root на роутере."""

    @pytest.mark.anyio
    async def test_forged_ticket_refused(self):
        assert await terminal_ticket.redeem("не-наш-билет") is None

    @pytest.mark.anyio
    async def test_empty_ticket_refused(self):
        assert await terminal_ticket.redeem("") is None

    @pytest.mark.anyio
    async def test_forged_cookie_gives_no_target(self):
        assert await terminal_ticket.load("подделка") is None

    @pytest.mark.anyio
    async def test_no_cookie_gives_no_target(self):
        assert await terminal_ticket.load(None) is None


class TestTerminalWithoutSession:
    def test_open_without_ticket_explains_itself(self, client):
        response = client.get("/terminal/open")
        assert response.status_code == 409
        assert "Терминал закрыт" in response.text

    def test_forged_ticket_does_not_open(self, client):
        response = client.get("/terminal/open?ticket=forged")
        assert response.status_code == 409

    def test_panel_cookie_is_not_a_terminal_cookie(self, client):
        """Кука панели не должна открывать терминал: разные двери, разные права.

        Панель — это веб-интерфейс роутера, терминал — root-сессия. Общая кука
        превратила бы «посмотреть настройки» в «сделать что угодно»."""
        response = client.get("/terminal/open", cookies={"rs_panel": "any-panel-session"})
        assert response.status_code == 409


class TestSessionsAreSeparate:
    """Билеты панели и терминала подписаны разной солью и лежат в разных ключах.

    Иначе билет, выданный на панель, открывал бы терминал — а выдают их
    разные кнопки и, вообще говоря, по разным поводам.
    """

    def test_cookies_differ(self):
        from core.services import panel_ticket

        assert terminal_ticket.COOKIE != panel_ticket.COOKIE

    def test_terminal_session_is_shorter(self):
        """Забытая вкладка терминала — открытый root, забытая вкладка панели —
        всего лишь веб-интерфейс. Времени у первой должно быть меньше."""
        from core.services import panel_ticket

        assert terminal_ticket.SESSION_TTL_SEC < panel_ticket.SESSION_TTL_SEC


class TestPageIsSelfContained:
    """Страница терминала не тянет ничего с чужих адресов.

    Админку открывают из сетей, где половина CDN не отвечает, и терминал
    превратился бы в чёрный экран без единого объяснения.
    """

    SOURCE = ""

    @classmethod
    def setup_class(cls):
        from pathlib import Path

        cls.SOURCE = (
            Path(__file__).resolve().parents[1] / "api" / "routes" / "terminal.py"
        ).read_text(encoding="utf-8")

    def test_no_external_scripts(self):
        for host in ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com", "//"):
            if host == "//":
                # Протокол-относительные адреса — тот же CDN, только записанный иначе.
                assert 'src="//' not in self.SOURCE
                continue
            assert host not in self.SOURCE

    def test_assets_come_from_our_static(self):
        assert '/static/vendor/xterm.js' in self.SOURCE
        assert '/static/vendor/xterm.css' in self.SOURCE


class TestTicketEndpointIsGuarded:
    def test_requires_token(self, client):
        """Ручка выдачи билета закрыта тем же токеном, что и остальной парк:
        без него она отвечает 404, а не выдаёт ссылку на root."""
        response = client.post("/api/v1/fleet/routers/1/terminal-ticket", json={})
        assert response.status_code in (401, 403, 404)
