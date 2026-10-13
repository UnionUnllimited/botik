"""Поиск по спискам доменов: пойдёт ли сайт через туннель и откуда он в списке.

Страница показывала числа — «2 037 строк», — а что внутри, видно не было.
Здесь стерегутся ответы, в которых легко ошибиться:

* домен покрывается своим родителем: `youtube.com` в списке ведёт и `www.`;
* IP — сетью, в которую он попадает, а не совпадением строк;
* вставленную ссылку чистим до домена, сеть с маской не ломаем;
* откуда запись — по снимкам источников той же сборки;
* «добавить в свой список» не плодит дубли и ложится в историю правок.
"""

from __future__ import annotations

import pytest

from core.models import ListKind
from core.services import domain_lists, list_lookup


class TestNormalize:
    @pytest.mark.parametrize(
        ("raw", "kind", "value"),
        [
            ("YouTube.com", ListKind.PROXY_DOMAIN, "youtube.com"),
            ("https://www.youtube.com/watch?v=1", ListKind.PROXY_DOMAIN, "www.youtube.com"),
            ("t.me:443", ListKind.PROXY_DOMAIN, "t.me"),
            ("149.154.167.50", ListKind.PROXY_IP, "149.154.167.50"),
            ("http://149.154.167.50:80/x", ListKind.PROXY_IP, "149.154.167.50"),
            ("10.0.0.0/8", ListKind.PROXY_IP, "10.0.0.0/8"),
        ],
    )
    def test_pasted_anything(self, raw, kind, value):
        assert list_lookup.normalize(raw) == (kind, value)


def test_parent_domain_covers_subdomains():
    entries = {"youtube.com", "googlevideo.com"}
    assert list_lookup._domain_covered_by("r3.sn-abc.googlevideo.com", entries) == ["googlevideo.com"]
    assert list_lookup._domain_covered_by("youtube.com", entries) == ["youtube.com"]
    assert list_lookup._domain_covered_by("notyoutube.com", entries) == []


def test_ip_is_found_in_its_network_not_by_text():
    entries = ["149.154.160.0/20", "91.108.4.0/22", "1.1.1.1"]
    assert list_lookup._ip_covered_by("149.154.167.50", entries) == ["149.154.160.0/20"]
    assert list_lookup._ip_covered_by("1.1.1.1", entries) == ["1.1.1.1"]
    assert list_lookup._ip_covered_by("149.154.180.1", entries) == []


class _Row:
    def __init__(self, body: str):
        self.body = body


class _Session:
    """Своя «база» из одной строки: свой список домена."""

    def __init__(self, body: str = ""):
        self.row = _Row(body)

    async def scalar(self, _statement):
        return self.row


@pytest.fixture
def lists(tmp_path, monkeypatch):
    monkeypatch.setattr(domain_lists, "lists_dir", lambda: tmp_path)
    domain_lists.write_list(ListKind.PROXY_DOMAIN, ["googlevideo.com", "youtube-nocookie.com", "youtube.com"])
    domain_lists.write_list(ListKind.PROXY_IP, ["149.154.160.0/20"])

    class _Source:
        def __init__(self, id_, title, kind):
            self.id, self.title, self.url, self.kind = id_, title, "https://x", kind

    domain_lists.write_snapshots(
        [_Source(1, "Россия целиком", ListKind.PROXY_DOMAIN), _Source(2, "Telegram", ListKind.PROXY_IP)],
        {1: ["youtube.com", "googlevideo.com"], 2: ["149.154.160.0/20"]},
    )
    return tmp_path


@pytest.mark.asyncio
async def test_lookup_answers_where_from(lists):
    found = await list_lookup.lookup(_Session("youtube-nocookie.com\n"), "https://www.youtube.com/")
    assert found["covered"] is True
    assert found["covered_by"] == [{"entry": "youtube.com", "origin": ["Россия целиком"]}]
    assert found["matches"] == [{"entry": "youtube-nocookie.com", "origin": ["свой список"]}]
    assert found["snapshots_ready"] is True


@pytest.mark.asyncio
async def test_lookup_ip(lists):
    found = await list_lookup.lookup(_Session(), "149.154.167.50")
    assert found["kind"] == ListKind.PROXY_IP
    assert found["covered_by"] == [{"entry": "149.154.160.0/20", "origin": ["Telegram"]}]


@pytest.mark.asyncio
async def test_not_in_lists(lists):
    found = await list_lookup.lookup(_Session(), "example.org")
    assert found["covered"] is False
    assert found["matches"] == []


def test_snapshots_of_removed_sources_are_dropped(lists):
    class _Source:
        id, title, url, kind = 1, "Россия целиком", "https://x", ListKind.PROXY_DOMAIN

    domain_lists.write_snapshots([_Source()], {1: ["youtube.com"]})
    sources, contents = domain_lists.read_snapshots()
    assert [item["id"] for item in sources] == [1]
    assert set(contents) == {1}
    assert not (lists / domain_lists.SNAPSHOT_DIR / "2.lst").exists()


class TestOwnList:
    @pytest.mark.asyncio
    async def test_add_appends_once(self, monkeypatch):
        saved = []

        async def _save(_session, kind, body, *, author=""):
            saved.append(body)
            return (1, 0)

        monkeypatch.setattr(domain_lists, "save_manual", _save)
        session = _Session("# свои\nexample.com\n")
        assert await list_lookup.add_to_own(session, ListKind.PROXY_DOMAIN, "https://New.Site/path") == ""
        assert saved == ["# свои\nexample.com\nnew.site\n"]

        saved.clear()
        assert await list_lookup.add_to_own(session, ListKind.PROXY_DOMAIN, "example.com") == ""
        assert saved == [], "уже есть — дубль не нужен"

    @pytest.mark.asyncio
    async def test_add_refuses_garbage(self):
        assert await list_lookup.add_to_own(_Session(), ListKind.PROXY_IP, "не адрес")

    @pytest.mark.asyncio
    async def test_remove_keeps_comments(self, monkeypatch):
        saved = []

        async def _save(_session, kind, body, *, author=""):
            saved.append(body)
            return (0, 1)

        monkeypatch.setattr(domain_lists, "save_manual", _save)
        session = _Session("# свои\nexample.com\nother.com\n")
        assert await list_lookup.remove_from_own(session, ListKind.PROXY_DOMAIN, "example.com") == ""
        assert saved == ["# свои\nother.com\n"]

    @pytest.mark.asyncio
    async def test_source_line_cannot_be_removed_here(self):
        session = _Session("other.com\n")
        error = await list_lookup.remove_from_own(session, ListKind.PROXY_DOMAIN, "youtube.com")
        assert "из источника" in error
