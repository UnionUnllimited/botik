"""Путь до frps через прокси — одной настройкой и для всех, кто туда ходит.

frps обязан стоять в России: иначе до него не достучатся роутеры. Наше
приложение живёт за границей, и дорога между ними может быть перекрыта
целиком — 4 октября 2026 перестали ходить и TCP, и ICMP, в обе стороны,
при рабочем интернете с обеих сторон.

Первым решением был мост из `socat` на третьей машине. Он продержался две
недели и упал, потому что его собственная дорога до frps пропала тем же
способом. Выглядело это как «все роутеры отвалились», и час ушёл на то,
чтобы понять, где именно рвётся.

Теперь путь задаётся настройкой, а не посредником: у frpc для этого есть
штатный `transport.proxyURL`, а httpx умеет `proxy=`. Лишнего звена,
которое падает молча, в схеме больше нет.

Главное, что стережёт этот файл, — что путь **один на обоих**. К frps
ходят двое: визитёр за туннелями и воркер за статусом. Разойдись они —
получим рабочие туннели при молчащем статусе, то есть панель открывается,
а в админке все «не на связи». Ровно это и наблюдали, пока мост стоял
только под визитёром.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RENDER = (ROOT / "worker" / "tasks" / "frpc_config.py").read_text(encoding="utf-8")
DASHBOARD = (ROOT / "core" / "services" / "frp.py").read_text(encoding="utf-8")


class TestBothTakeTheSameRoad:
    def test_the_visitor_is_told_about_the_proxy(self):
        assert "transport.proxyURL" in RENDER

    def test_the_dashboard_is_told_too(self):
        head = DASHBOARD.index("def _get_client")
        body = DASHBOARD[head : DASHBOARD.index("return self._client", head)]
        assert "proxy=self._config.proxy_url" in body

    def test_they_read_one_setting(self):
        """Две настройки разъехались бы на первой же правке, и разъехались бы
        молча: туннели работают, статус врёт."""
        assert "frp.proxy_url" in RENDER
        assert "self._config.proxy_url" in DASHBOARD


class TestWithoutItNothingChanges:
    def test_an_empty_setting_adds_no_line(self):
        """Прямой путь — обычное дело, и строка про прокси в конфиге
        визитёра при нём лишняя."""
        head = RENDER.index("transport.proxyURL")
        window = RENDER[head - 400 : head + 200]
        assert "if frp.proxy_url" in window
        assert "else []" in window

    def test_an_empty_setting_means_no_proxy_for_httpx(self):
        """`proxy=""` httpx понимает не как «напрямую», а как сломанный
        адрес, и падает на первом же запросе."""
        head = DASHBOARD.index("proxy=self._config.proxy_url")
        assert "or None" in DASHBOARD[head : head + 60]


def test_socks_is_actually_installed():
    """`socks5://` без пакета падает с «the 'socksio' package is not
    installed» — в контейнере, где проверить это некому."""
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "httpx[socks]" in requirements


def test_the_setting_is_described_for_the_operator():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "FRP_PROXY_URL=" in example
    assert "socks5://" in example


@pytest.mark.parametrize("address", ["http://proxy:3128", "socks5://user:pass@proxy:1080"])
def test_the_visitor_line_is_quoted(address, monkeypatch):
    """TOML требует кавычек, а адрес приходит из настройки: без них конфиг
    не разберётся, и frpc не стартует вовсе."""
    from core.config import settings
    from worker.tasks.frpc_config import render_config

    monkeypatch.setattr(settings.frp, "proxy_url", address)
    assert f'transport.proxyURL = "{address}"' in render_config([])
