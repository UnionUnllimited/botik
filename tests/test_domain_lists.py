"""Чистка списков переехала со скрипта и должна вести себя так же.

Разойтись с прежними правилами значит поменять то, что попадёт в туннель,
у всего парка сразу: строка, которую скрипт выбрасывал, а мы оставим, —
это домен, поехавший в обход, и наоборот.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from core.models import ListKind
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
        merged = merge([["b.com", "a.com"], ["a.com"]], "", ListKind.DIRECT_DOMAIN)
        assert merged == ["a.com", "b.com"]

    def test_manual_list_is_cleaned_too(self):
        """Оператор вставляет что придётся — причёсываем тем же способом."""
        merged = merge([["a.com"]], "https://Z.COM/path\n# коммент\n\nмусор тут", ListKind.DIRECT_DOMAIN)
        assert merged == ["a.com", "z.com"]

    def test_manual_ip_list_uses_ip_rules(self):
        merged = merge([["1.2.3.4"]], "10.0.0.0/8\nexample.com", ListKind.DIRECT_IP)
        assert merged == ["1.2.3.4", "10.0.0.0/8"]

    def test_empty_everything_is_not_an_error(self):
        assert merge([], "", ListKind.DIRECT_DOMAIN) == []


class TestStorage:
    """Списки лежат файлом: за ними ходит весь парк, база тут лишняя."""

    def test_write_then_read(self, tmp_path, monkeypatch):
        from core.config import settings
        from core.services import domain_lists

        monkeypatch.setattr(settings.app, "media_dir", str(tmp_path))
        domain_lists.write_list(ListKind.DIRECT_DOMAIN, ["a.com", "b.com"])
        assert domain_lists.read_list(ListKind.DIRECT_DOMAIN) == "a.com\nb.com\n"

    def test_missing_file_is_empty_not_error(self, tmp_path, monkeypatch):
        """Сборки ещё не было — отдаём пустое, а не падаем."""
        from core.config import settings
        from core.services import domain_lists

        monkeypatch.setattr(settings.app, "media_dir", str(tmp_path))
        assert domain_lists.read_list(ListKind.DIRECT_IP) == ""

    def test_no_temp_file_left_behind(self, tmp_path, monkeypatch):
        """Запись атомарная: роутер не должен получить половину списка."""
        from core.config import settings
        from core.services import domain_lists

        monkeypatch.setattr(settings.app, "media_dir", str(tmp_path))
        domain_lists.write_list(ListKind.DIRECT_IP, ["1.2.3.4"])
        leftovers = list((tmp_path / "lists").glob("*.tmp"))
        assert leftovers == []

    def test_empty_list_does_not_write_a_stray_newline(self, tmp_path, monkeypatch):
        from core.config import settings
        from core.services import domain_lists

        monkeypatch.setattr(settings.app, "media_dir", str(tmp_path))
        domain_lists.write_list(ListKind.DIRECT_DOMAIN, [])
        assert domain_lists.read_list(ListKind.DIRECT_DOMAIN) == ""


class TestServing:
    """Раздача открыта: за списком приходит прошивка, а не наш процесс."""

    def test_served_without_token(self, client):
        assert client.get("/lists/direct-domains.lst").status_code == 200
        assert client.get("/lists/direct-ip.lst").status_code == 200

    def test_served_as_plain_text(self, client):
        response = client.get("/lists/direct-domains.lst")
        assert response.headers["content-type"].startswith("text/plain")

    def test_cacheable(self, client):
        """Парк тянет списки разом — без кеша это лишняя нагрузка на ровном месте."""
        assert "max-age" in client.get("/lists/direct-ip.lst").headers.get("cache-control", "")


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
        assert client.get("/lists/direct-domains.lst").status_code == 200


class TestUpload:
    """Выкладка мягкая: список уже отдаётся с нашего домена."""

    CONF: ClassVar[dict[str, str]] = {
        "lists_s3_bucket": "b",
        "lists_s3_endpoint": "https://storage.example",
        "lists_s3_access_key": "k",
        "lists_s3_secret_key": "s",
        "lists_s3_prefix": "lists/",
    }

    @pytest.mark.asyncio
    async def test_skipped_when_not_configured(self):
        """Пустой bucket выключает выкладку — это не ошибка сборки."""
        from core.services import domain_lists

        assert await domain_lists.upload({ListKind.DIRECT_DOMAIN: ["a.com"]}, {}) is False

    @pytest.mark.asyncio
    async def test_partial_config_is_not_enough(self):
        """Адрес без ключей — это забытая настройка, а не выключенная выкладка."""
        from core.services import domain_lists

        half = {"lists_s3_bucket": "b", "lists_s3_endpoint": "https://storage.example"}
        assert await domain_lists.upload({ListKind.DIRECT_DOMAIN: ["a.com"]}, half) is False

    @pytest.mark.asyncio
    async def test_broken_storage_does_not_raise(self, monkeypatch):
        """Хранилище недоступно — сборка всё равно должна досчитаться."""
        from core.services import domain_lists

        def _boom(_conf):
            raise RuntimeError("хранилище недоступно")

        monkeypatch.setattr(domain_lists, "_s3_client", _boom)
        assert await domain_lists.upload({ListKind.DIRECT_DOMAIN: ["a.com"]}, self.CONF) is False


class TestSecretsNotLeaked:
    def test_keys_are_marked_secret(self):
        """Страница открыта оператору, ключ от хранилища ему смотреть незачем."""
        from core.services import domain_lists

        assert "lists_s3_access_key" in domain_lists.SECRET_KEYS
        assert "lists_s3_secret_key" in domain_lists.SECRET_KEYS


class TestDiffCounts:
    """История отвечает на «что изменилось», а не «сколько символов тронули»."""

    def test_added_and_removed(self):
        from core.services.domain_lists import diff_counts

        assert diff_counts("a.com\nb.com", "b.com\nc.com", ListKind.DIRECT_DOMAIN) == (1, 1)

    def test_reordering_is_not_a_change(self):
        """Иначе перестановка строк давала бы «+40 −40» на правке одной буквы."""
        from core.services.domain_lists import diff_counts

        assert diff_counts("a.com\nb.com", "b.com\na.com", ListKind.DIRECT_DOMAIN) == (0, 0)

    def test_case_and_scheme_are_not_a_change(self):
        from core.services.domain_lists import diff_counts

        assert diff_counts("a.com", "https://A.COM/path", ListKind.DIRECT_DOMAIN) == (0, 0)

    def test_garbage_lines_do_not_count(self):
        """Строку, которую сборка отбросит, история не считает добавленной."""
        from core.services.domain_lists import diff_counts

        assert diff_counts("a.com", "a.com\nне домен\n\n# коммент", ListKind.DIRECT_DOMAIN) == (0, 0)


class TestImportFromUrl:
    @pytest.mark.asyncio
    async def test_rejects_non_http(self):
        from core.services.domain_lists import import_from_url

        body, error = await import_from_url("ftp://example.com/list.lst", ListKind.DIRECT_DOMAIN)
        assert not body
        assert "http" in error

    @pytest.mark.asyncio
    async def test_cleans_what_it_downloaded(self, monkeypatch):
        """Файл приезжает как есть, а в поле должен лечь причёсанным."""
        from core.services import domain_lists

        async def _fetch(_client, _url, etag=""):
            return "# заголовок\nhttps://A.COM/path\nмусор\nb.com\n", "", ""

        monkeypatch.setattr(domain_lists, "fetch", _fetch)
        body, error = await domain_lists.import_from_url("https://e.com/l.lst", ListKind.DIRECT_DOMAIN)
        assert error == ""
        assert body == "a.com\nb.com"

    @pytest.mark.asyncio
    async def test_empty_result_is_reported(self, monkeypatch):
        """Скачали, а в файле ничего подходящего — это ошибка, а не пустой список."""
        from core.services import domain_lists

        async def _fetch(_client, _url, etag=""):
            return "# только комментарии\n", "", ""

        monkeypatch.setattr(domain_lists, "fetch", _fetch)
        body, error = await domain_lists.import_from_url("https://e.com/l.lst", ListKind.DIRECT_DOMAIN)
        assert not body
        assert error


class TestManualFingerprint:
    """Свой список — такой же повод пересобрать, как новая версия источника.

    Круг пропускался, если не изменился ни один источник, и дописанный
    оператором домен не доезжал до роутеров никогда.
    """

    def test_change_moves_the_fingerprint(self):
        from core.services.domain_lists import manual_fingerprint

        before = manual_fingerprint({ListKind.DIRECT_DOMAIN: "a.com", ListKind.DIRECT_IP: ""})
        after = manual_fingerprint({ListKind.DIRECT_DOMAIN: "a.com\nb.com", ListKind.DIRECT_IP: ""})
        assert before != after

    def test_reordering_does_not(self):
        """Иначе каждое сохранение запускало бы перекачку всех источников."""
        from core.services.domain_lists import manual_fingerprint

        one = manual_fingerprint({ListKind.DIRECT_DOMAIN: "a.com\nb.com", ListKind.DIRECT_IP: ""})
        two = manual_fingerprint({ListKind.DIRECT_DOMAIN: "b.com\na.com", ListKind.DIRECT_IP: ""})
        assert one == two

    def test_ip_list_counts_too(self):
        from core.services.domain_lists import manual_fingerprint

        before = manual_fingerprint({ListKind.DIRECT_DOMAIN: "a.com", ListKind.DIRECT_IP: ""})
        after = manual_fingerprint({ListKind.DIRECT_DOMAIN: "a.com", ListKind.DIRECT_IP: "10.0.0.0/8"})
        assert before != after


class TestLocalPublish:
    """Копия на диск — для домена, который отдаёт списки своим веб-сервером."""

    def test_writes_both_files(self, tmp_path):
        from core.services.domain_lists import publish_local

        built = {ListKind.DIRECT_DOMAIN: ["a.com"], ListKind.DIRECT_IP: ["1.2.3.4"]}
        assert publish_local(str(tmp_path), built) is True
        assert (tmp_path / "direct-domains.lst").read_text(encoding="utf-8") == "a.com\n"
        assert (tmp_path / "direct-ip.lst").read_text(encoding="utf-8") == "1.2.3.4\n"

    def test_empty_path_means_do_not_publish(self, tmp_path):
        from core.services.domain_lists import publish_local

        assert publish_local("", {ListKind.DIRECT_DOMAIN: ["a.com"]}) is False

    def test_no_temp_file_left_behind(self, tmp_path):
        """Запись атомарная: роутер не должен получить половину списка."""
        from core.services.domain_lists import publish_local

        publish_local(str(tmp_path), {ListKind.DIRECT_DOMAIN: ["a.com"]})
        assert list(tmp_path.glob("*.tmp")) == []

    def test_unwritable_path_does_not_raise(self, tmp_path):
        """Каталог пропал или прав нет — сборка всё равно должна досчитаться."""
        from core.services.domain_lists import publish_local

        busy = tmp_path / "file"
        busy.write_text("не каталог", encoding="utf-8")
        assert publish_local(str(busy), {ListKind.DIRECT_DOMAIN: ["a.com"]}) is False


class TestListsAreSeparate:
    """Домены и подсети — разные файлы по разным адресам.

    Роутер берёт их порознь: правило для домена и правило для подсети
    попадают в разные части его конфигурации.
    """

    def test_file_names_do_not_collide(self):
        from core.services.domain_lists import FILE_NAMES

        assert set(FILE_NAMES) == set(ListKind.ALL)
        assert len(set(FILE_NAMES.values())) == len(ListKind.ALL), (
            "два вида пишутся в один файл — один затрёт другой"
        )

    def test_written_separately(self, tmp_path, monkeypatch):
        """Запись одного вида не трогает файл другого."""
        from core.config import settings
        from core.services import domain_lists

        monkeypatch.setattr(settings.app, "media_dir", str(tmp_path))
        domain_lists.write_list(ListKind.DIRECT_DOMAIN, ["a.com"])
        domain_lists.write_list(ListKind.DIRECT_IP, ["1.2.3.4"])
        assert domain_lists.read_list(ListKind.DIRECT_DOMAIN) == "a.com\n"
        assert domain_lists.read_list(ListKind.DIRECT_IP) == "1.2.3.4\n"

        domain_lists.write_list(ListKind.DIRECT_DOMAIN, ["a.com", "b.com"])
        assert domain_lists.read_list(ListKind.DIRECT_IP) == "1.2.3.4\n"

    def test_served_at_different_urls(self, client):
        first = client.get("/lists/direct-domains.lst")
        second = client.get("/lists/direct-ip.lst")
        assert first.status_code == 200
        assert second.status_code == 200

    def test_source_is_cleaned_by_its_own_kind(self):
        """Источник, помеченный доменным, чистится доменными правилами.

        Подсеть в списке доменов — правило, которое прошивка не применит.
        Рубеж стоит в сборке: каждый кусок причёсывается чистильщиком своего
        вида, и `merge` получает его уже разобранным. Проверяем сам чистильщик,
        а не `merge`: тот принимает готовые значения и обещаний про вид не даёт.
        """
        from core.services.domain_lists import CLEANERS

        assert CLEANERS[ListKind.DIRECT_DOMAIN]("1.2.3.4") == []
        assert CLEANERS[ListKind.DIRECT_IP]("example.com") == []

    def test_manual_list_is_cleaned_by_kind_in_merge(self):
        """Свой список чистится внутри merge — он приходит сырым от человека."""
        from core.services.domain_lists import merge

        mixed = "1.2.3.4\nexample.com"
        assert merge([], mixed, ListKind.DIRECT_DOMAIN) == ["example.com"]
        assert merge([], mixed, ListKind.DIRECT_IP) == ["1.2.3.4"]


class TestOrderMatchesTheRepository:
    """Порядок строк повторяет `parts/build.sh` из репозитория списков.

    Собранное у нас и собранное скриптом должно совпадать строка в строку:
    иначе не сверить глазами, что роутер получил именно то, что лежит
    в `domensrouter`, — а diff в тысячу строк никто читать не станет.
    """

    def test_networks_sort_numerically(self):
        """`sort -t. -k1,1n -k2,2n -k3,3n -k4,4n`, а не побайтово.

        Побайтовая сортировка ставит `10.0.0.0` между `1.0.0.0` и `2.0.0.0`.
        """
        given = ["2.0.0.0/8", "10.0.0.0/8", "1.0.0.0/8", "9.9.9.9"]
        assert merge([given], "", ListKind.DIRECT_IP) == [
            "1.0.0.0/8",
            "2.0.0.0/8",
            "9.9.9.9",
            "10.0.0.0/8",
        ]

    def test_same_network_sorts_by_mask(self):
        given = ["10.0.0.0/16", "10.0.0.0/8"]
        assert merge([given], "", ListKind.DIRECT_IP) == ["10.0.0.0/8", "10.0.0.0/16"]

    def test_domains_sort_bytewise(self):
        """`LC_ALL=C sort -u`: дефис (0x2D) раньше цифр и букв."""
        given = ["ya.ru", "a-b.ru", "ab.ru", "a.ru"]
        assert merge([given], "", ListKind.DIRECT_DOMAIN) == ["a-b.ru", "a.ru", "ab.ru", "ya.ru"]


class TestThreeListsHaveDifferentMeanings:
    """Три списка, и перепутать их дороже всего остального в этом модуле.

    Первые два уводят мимо туннеля, третий — в туннель. Ошибка в одну сторону
    пустит банки и госуслуги через зарубежный сервер, в другую — оставит
    заблокированное без обхода.
    """

    def test_all_three_kinds_are_built(self):
        from core.services.domain_lists import CLEANERS, FILE_NAMES, PASSWALL_SETTINGS

        assert set(ListKind.ALL) == {"direct_domain", "direct_ip", "proxy_domain"}
        for mapping in (CLEANERS, FILE_NAMES, PASSWALL_SETTINGS):
            assert set(mapping) == set(ListKind.ALL)

    def test_proxy_list_is_cleaned_as_domains(self):
        """Через туннель идут домены, а не сети: чистильщик тот же, что у direct."""
        from core.services.domain_lists import CLEANERS, clean_domains

        assert CLEANERS[ListKind.PROXY_DOMAIN] is clean_domains

    def test_settings_do_not_collide(self):
        """Каждый список прописывается в свою настройку PassWall."""
        from core.services.domain_lists import PASSWALL_SETTINGS

        assert len(set(PASSWALL_SETTINGS.values())) == len(ListKind.ALL)
        assert "chnlist_url" in PASSWALL_SETTINGS[ListKind.DIRECT_DOMAIN]
        assert "chnroute_url" in PASSWALL_SETTINGS[ListKind.DIRECT_IP]
        assert "gfwlist_url" in PASSWALL_SETTINGS[ListKind.PROXY_DOMAIN]

    def test_old_names_are_gone(self):
        """Смысл списков стал обратным: отдавать новое по старому адресу
        значит молча вывернуть маршрутизацию у того, кто на него смотрит."""
        from core.services.domain_lists import FILE_NAMES

        assert "domains.lst" not in FILE_NAMES.values()
        assert "ip.lst" not in FILE_NAMES.values()


class TestSourcesFromOurRepo:
    """Каждый файл из `parts/` должен быть заведён источником.

    Незаведённый источник не ошибка и не предупреждение: его строки просто
    не попадают в сборку, и на странице списков это видно как «0 из N» —
    то есть только если туда заглянуть.
    """

    @staticmethod
    def _module(name: str):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "migrations/versions" / name
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_migration_continues_the_chain(self):
        later = self._module("0019_more_direct_domain_parts.py")
        assert later.revision == "0019"
        assert later.down_revision == "0018"

    def test_new_parts_are_registered(self):
        """Шесть категорий из `parts/domains/`, которых не было в посеве."""
        later = self._module("0019_more_direct_domain_parts.py")
        stems = {stem for stem, _title, _order in later._NEW}
        assert stems == {
            "05-own-infra",
            "81-device-ota",
            "82-dev-registries",
            "83-asia",
            "84-hardware-vendors",
            "85-desktop-software",
        }

    def test_they_go_past_the_tunnel(self):
        """Все шесть лежат в `parts/domains/`, а этой папке отвечает
        `direct_domain` — мимо туннеля. Ошибиться видом значит завернуть
        обновления и репозитории в зарубежный канал."""
        later = self._module("0019_more_direct_domain_parts.py")
        for stem, _title, _order in later._NEW:
            assert "/domains/" in later._url(stem)

    def test_addresses_do_not_repeat_the_seed(self):
        """Адрес уникален в таблице: повтор посева упёрся бы в конфликт,
        а не добавил бы категорию."""
        seed = self._module("0018_lists_from_our_repo.py")
        seeded = {
            f"{seed._RAW}/{seed._FOLDERS[kind]}/{stem}.lst" for kind, stem, _title in seed._SEED
        }
        later = self._module("0019_more_direct_domain_parts.py")
        assert not seeded & {later._url(stem) for stem, _t, _o in later._NEW}

    def test_order_puts_them_among_neighbours(self):
        """Сортировка — по соседям с тем же номером, а не в хвост за сетями:
        иначе `81` окажется ниже списков IP, и найти его будет негде."""
        later = self._module("0019_more_direct_domain_parts.py")
        order = {stem: value for stem, _title, value in later._NEW}
        assert order["05-own-infra"] < order["81-device-ota"]
        # 80-oss-updates в посеве — двадцать первый по счёту, то есть 210.
        assert all(210 < order[stem] < 220 for stem in order if stem.startswith("8"))
