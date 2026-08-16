"""Чистка списков переехала со скрипта и должна вести себя так же.

Разойтись с прежними правилами значит поменять то, что попадёт в туннель,
у всего парка сразу: строка, которую скрипт выбрасывал, а мы оставим, —
это домен, поехавший в обход, и наоборот.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.services.domain_lists import clean_domains, clean_ips, merge


@pytest.fixture(scope="module")
def client() -> TestClient:
    from api.main import app

    with TestClient(app) as test_client:
        yield test_client


class TestCleanDomains:
    def test_plain_lines_survive(self):
        assert clean_domains("example.com\nsub.example.org") == ["example.com", "sub.example.org"]

    def test_scheme_and_path_are_cut(self):
        assert clean_domains("https://example.com/some/path?a=1") == ["example.com"]

    def test_comments_and_blanks_dropped(self):
        raw = "# заголовок\nexample.com  # почему\n\n   \n"
        assert clean_domains(raw) == ["example.com"]

    def test_case_is_normalized(self):
        assert clean_domains("EXAMPLE.COM") == ["example.com"]

    def test_carriage_returns_do_not_leak(self):
        """Файлы у поставщика приходят с CRLF — скрипт снимал их первым делом."""
        assert clean_domains("example.com\r\nexample.org\r\n") == ["example.com", "example.org"]

    @pytest.mark.parametrize(
        "line",
        ["не домен", "example", "1.2.3.4", "два слова.com", "*.example.com", "-", "example.c"],
    )
    def test_garbage_dropped(self, line):
        assert clean_domains(line) == []


class TestCleanIps:
    def test_addresses_and_subnets(self):
        assert clean_ips("1.2.3.4\n10.0.0.0/8") == ["1.2.3.4", "10.0.0.0/8"]

    def test_comment_stripped(self):
        assert clean_ips("1.2.3.4 # telegram") == ["1.2.3.4"]

    @pytest.mark.parametrize("line", ["example.com", "::1", "1.2.3", "1.2.3.4 5.6.7.8", ""])
    def test_garbage_dropped(self, line):
        assert clean_ips(line) == []


class TestMerge:
    def test_duplicates_collapse_and_sorted(self):
        merged = merge([["b.com", "a.com"], ["a.com"]], "", "domain")
        assert merged == ["a.com", "b.com"]

    def test_manual_list_is_cleaned_too(self):
        """Оператор вставляет что придётся — причёсываем тем же способом."""
        merged = merge([["a.com"]], "https://Z.COM/path\n# коммент\n\nмусор тут", "domain")
        assert merged == ["a.com", "z.com"]

    def test_manual_ip_list_uses_ip_rules(self):
        merged = merge([["1.2.3.4"]], "10.0.0.0/8\nexample.com", "ip")
        assert merged == ["1.2.3.4", "10.0.0.0/8"]

    def test_empty_everything_is_not_an_error(self):
        assert merge([], "", "domain") == []


class TestStorage:
    """Списки лежат файлом: за ними ходит весь парк, база тут лишняя."""

    def test_write_then_read(self, tmp_path, monkeypatch):
        from core.config import settings
        from core.services import domain_lists

        monkeypatch.setattr(settings.app, "media_dir", str(tmp_path))
        domain_lists.write_list("domain", ["a.com", "b.com"])
        assert domain_lists.read_list("domain") == "a.com\nb.com\n"

    def test_missing_file_is_empty_not_error(self, tmp_path, monkeypatch):
        """Сборки ещё не было — отдаём пустое, а не падаем."""
        from core.config import settings
        from core.services import domain_lists

        monkeypatch.setattr(settings.app, "media_dir", str(tmp_path))
        assert domain_lists.read_list("ip") == ""

    def test_no_temp_file_left_behind(self, tmp_path, monkeypatch):
        """Запись атомарная: роутер не должен получить половину списка."""
        from core.config import settings
        from core.services import domain_lists

        monkeypatch.setattr(settings.app, "media_dir", str(tmp_path))
        domain_lists.write_list("ip", ["1.2.3.4"])
        leftovers = list((tmp_path / "lists").glob("*.tmp"))
        assert leftovers == []

    def test_empty_list_does_not_write_a_stray_newline(self, tmp_path, monkeypatch):
        from core.config import settings
        from core.services import domain_lists

        monkeypatch.setattr(settings.app, "media_dir", str(tmp_path))
        domain_lists.write_list("domain", [])
        assert domain_lists.read_list("domain") == ""


class TestServing:
    """Раздача открыта: за списком приходит прошивка, а не наш процесс."""

    def test_served_without_token(self, client):
        assert client.get("/lists/domains.lst").status_code == 200
        assert client.get("/lists/ip.lst").status_code == 200

    def test_served_as_plain_text(self, client):
        response = client.get("/lists/domains.lst")
        assert response.headers["content-type"].startswith("text/plain")

    def test_cacheable(self, client):
        """Парк тянет списки разом — без кеша это лишняя нагрузка на ровном месте."""
        assert "max-age" in client.get("/lists/ip.lst").headers.get("cache-control", "")


class TestAdminEndpointsAccess:
    """Правка списков — под тем же токеном, что весь парк.

    Раздача открыта намеренно, а правка нет: домен, дописанный в свой список,
    открывает доступ всему парку разом.
    """

    PATHS = (
        ("get", "/api/v1/fleet/lists"),
        ("post", "/api/v1/fleet/lists/sources"),
        ("post", "/api/v1/fleet/lists/manual/domain"),
        ("post", "/api/v1/fleet/lists/build"),
    )

    @pytest.mark.parametrize(("method", "path"), PATHS)
    def test_disabled_without_token(self, client, monkeypatch, method, path):
        """Пустой токен — ручки как будто нет, как и у остального парка."""
        from pydantic import SecretStr

        from core.config import settings

        monkeypatch.setattr(settings.api, "fleet_token", SecretStr(""))
        kwargs = {"json": {}} if method == "post" else {}
        assert getattr(client, method)(path, **kwargs).status_code == 404

    @pytest.mark.parametrize(("method", "path"), PATHS)
    def test_wrong_token_rejected(self, client, monkeypatch, method, path):
        from pydantic import SecretStr

        from core.config import settings

        monkeypatch.setattr(settings.api, "fleet_token", SecretStr("lists-token"))
        kwargs = {"json": {}} if method == "post" else {}
        response = getattr(client, method)(
            path, headers={"Authorization": "Bearer not-the-token"}, **kwargs
        )
        assert response.status_code == 401

    def test_serving_stays_open_when_editing_is_locked(self, client, monkeypatch):
        """Токен закрывает правку, но не раздачу: за списком приходит прошивка."""
        from pydantic import SecretStr

        from core.config import settings

        monkeypatch.setattr(settings.api, "fleet_token", SecretStr("lists-token"))
        assert client.get("/api/v1/fleet/lists").status_code == 401
        assert client.get("/lists/domains.lst").status_code == 200


class TestUpload:
    """Выкладка мягкая: список уже отдаётся с нашего домена."""

    @pytest.mark.asyncio
    async def test_skipped_when_not_configured(self):
        """Пустой bucket выключает выкладку — это не ошибка сборки."""
        from core.services import domain_lists

        assert await domain_lists.upload({"domain": ["a.com"]}) is False

    @pytest.mark.asyncio
    async def test_broken_storage_does_not_raise(self, monkeypatch):
        """Хранилище недоступно — сборка всё равно должна досчитаться."""
        from pydantic import SecretStr

        from core.config import settings
        from core.services import domain_lists

        monkeypatch.setattr(settings.lists, "s3_bucket", "b")
        monkeypatch.setattr(settings.lists, "s3_endpoint", "https://storage.example")
        monkeypatch.setattr(settings.lists, "s3_access_key", SecretStr("k"))
        monkeypatch.setattr(settings.lists, "s3_secret_key", SecretStr("s"))

        def _boom():
            raise RuntimeError("хранилище недоступно")

        monkeypatch.setattr(domain_lists, "_s3_client", _boom)
        assert await domain_lists.upload({"domain": ["a.com"]}) is False
