"""Объектное хранилище: адреса, отказы и застава перед публикацией.

Кладём туда две вещи, и цена ошибки у них разная. Список весит пару мегабайт,
отдаётся и с нашего домена, и не доехавшая копия стоит одного круга ожидания.
Образ прошивки весит полсотни, качают его триста устройств разом, а сказать
нам «ссылка битая» роутеру нечем — он молча бросает закачку и ждёт суток.

Отсюда разное поведение при отказе, и проверяется здесь именно оно.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.models import ListKind
from core.services import domain_lists, firmware, object_storage

YANDEX = "https://storage.yandexcloud.net"

FULL = {
    "lists_s3_bucket": "234588",
    "lists_s3_endpoint": YANDEX,
    "lists_s3_region": "ru-central1",
    "lists_s3_prefix": "lists/",
    "lists_s3_access_key": "YCAJE-key",
    "lists_s3_secret_key": "YCP-secret",
}


class _Bucket:
    """Подставное хранилище: помнит, что в него положили."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.objects: dict[str, int] = {}
        self.deleted: list[str] = []
        self.fail = fail

    def __call__(self, _storage):
        return self

    def _maybe_fail(self) -> None:
        if self.fail is not None:
            raise self.fail

    def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803 — имена из boto3
        self._maybe_fail()
        self.objects[Key] = len(Body)

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):  # noqa: N803
        self._maybe_fail()
        self.objects[Key] = Path(Filename).stat().st_size

    def head_object(self, Bucket, Key):  # noqa: N803
        self._maybe_fail()
        return {"ContentLength": self.objects[Key]}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self._maybe_fail()
        self.objects.pop(Key, None)
        self.deleted.append(Key)


@pytest.fixture
def bucket(monkeypatch):
    def _install(fail: Exception | None = None) -> _Bucket:
        fake = _Bucket(fail=fail)
        monkeypatch.setattr(object_storage, "_client", fake)
        return fake

    return _install


class TestWhereTheFileWillBeLookedFor:
    """Адрес собирается один раз и уезжает в манифест — ошибиться в нём дороже
    всего: роутер по битой ссылке молчит до следующих суток."""

    def test_yandex_url_is_built_from_endpoint_and_bucket(self):
        storage = object_storage.from_mapping(FULL)
        assert storage.url_of("firmware/v140/a.bin") == f"{YANDEX}/234588/firmware/v140/a.bin"

    def test_a_cdn_in_front_replaces_it(self):
        """Полсотни мегабайт на триста устройств разумнее отдавать через CDN."""
        storage = object_storage.from_mapping({**FULL, "lists_s3_public_url": "https://cdn.example/"})
        assert storage.url_of("firmware/v140/a.bin") == "https://cdn.example/firmware/v140/a.bin"

    def test_a_leading_slash_does_not_double_up(self):
        storage = object_storage.from_mapping(FULL)
        assert "//firmware" not in storage.url_of("/firmware/a.bin")


class TestWhenItIsNotSetUp:
    @pytest.mark.parametrize("missing", sorted(FULL))
    def test_every_field_is_required(self, missing):
        """Половина настройки — это забытая настройка, а не выключенная выкладка."""
        if missing in ("lists_s3_region", "lists_s3_prefix"):
            pytest.skip("у них есть значение по умолчанию")
        storage = object_storage.from_mapping({**FULL, missing: ""})
        assert storage.configured is False

    @pytest.mark.asyncio
    async def test_nothing_is_sent_anywhere(self, bucket):
        fake = bucket()
        empty = object_storage.from_mapping({})
        assert await object_storage.put_bytes(empty, "k", b"x", "text/plain") != ""
        assert fake.objects == {}


class TestLists:
    """Списку отказ хранилища не страшен: он и так отдаётся с нашего домена."""

    @pytest.mark.asyncio
    async def test_both_kinds_go_under_the_prefix(self, bucket):
        fake = bucket()
        ok = await domain_lists.upload(
            {ListKind.PROXY_DOMAIN: ["a.com"], ListKind.PROXY_IP: ["1.2.3.0/24"]}, FULL
        )
        assert ok is True
        assert set(fake.objects) == {"lists/proxy-domains.lst", "lists/proxy-ip.lst"}

    @pytest.mark.asyncio
    async def test_a_refusal_is_reported_not_raised(self, bucket):
        bucket(RuntimeError("бакет недоступен"))
        assert await domain_lists.upload({ListKind.PROXY_DOMAIN: ["a.com"]}, FULL) is False

    def test_the_file_name_is_ours(self):
        """Оператор задаёт префикс, имя файла — нет: роутер ищет постоянное."""
        assert domain_lists.list_key("copy/", ListKind.PROXY_DOMAIN) == "copy/proxy-domains.lst"
        assert domain_lists.list_key("", ListKind.PROXY_DOMAIN) == "lists/proxy-domains.lst"


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self._data, self._at = data, 0

    async def read(self, size: int) -> bytes:
        piece = self._data[self._at : self._at + size]
        self._at += len(piece)
        return piece


NAME = f"titan-r140-cudy{firmware.IMAGE_SUFFIX}"


@pytest.fixture(autouse=True)
def media(tmp_path, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings.app, "media_dir", str(tmp_path))
    monkeypatch.setattr(settings.api, "public_base_url", "https://shop.example")
    return tmp_path


class TestFirmware:
    @pytest.mark.asyncio
    async def test_the_image_goes_to_the_bucket(self, bucket):
        fake = bucket()
        saved = await firmware.save_upload(
            version=140,
            model_key="cudy,wr3000e-v1",
            file_name=NAME,
            source=_Bytes(b"image" * 100),
            storage=object_storage.from_mapping(FULL),
        )
        assert f"firmware/v140/{NAME}" in fake.objects
        assert saved.remote_url.endswith(f"firmware/v140/{NAME}")

    @pytest.mark.asyncio
    async def test_it_stays_on_our_disk_too(self, bucket, media):
        """Запасной адрес и единственный способ удалить файл за собой."""
        bucket()
        saved = await firmware.save_upload(
            version=140,
            model_key="cudy,wr3000e-v1",
            file_name=NAME,
            source=_Bytes(b"image"),
            storage=object_storage.from_mapping(FULL),
        )
        assert (media / "firmware" / "images" / "v140" / NAME).exists()
        assert saved.url_path == f"/firmware/images/v140/{NAME}"

    @pytest.mark.asyncio
    async def test_a_refused_bucket_does_not_lose_the_upload(self, bucket, media):
        """Образ уже принят и посчитан — отказ хранилища делает выпуск дороже
        по трафику, а не сломанным."""
        bucket(RuntimeError("нет доступа"))
        saved = await firmware.save_upload(
            version=140,
            model_key="cudy,wr3000e-v1",
            file_name=NAME,
            source=_Bytes(b"image"),
            storage=object_storage.from_mapping(FULL),
        )
        assert saved.remote_url == ""
        assert (media / "firmware" / "images" / "v140" / NAME).exists()

    @pytest.mark.asyncio
    async def test_without_a_bucket_nothing_changes(self, bucket):
        bucket()
        saved = await firmware.save_upload(
            version=140,
            model_key="cudy,wr3000e-v1",
            file_name=NAME,
            source=_Bytes(b"image"),
            storage=None,
        )
        assert saved.remote_url == ""


class TestManifestPointsAtTheBucket:
    def _release(self, remote: str):
        from core.models import FirmwareImage, FirmwareRelease

        release = FirmwareRelease(version=140, notes="", rollout=50, rollout_max=50)
        release.images = [
            FirmwareImage(
                model_key="cudy,wr3000e-v1",
                file_name=NAME,
                url_path=f"/firmware/images/v140/{NAME}",
                remote_url=remote,
                sha256="a" * 64,
                size_bytes=500,
            )
        ]
        return release

    def test_the_bucket_url_wins(self):
        body = firmware.manifest_of(self._release(f"{YANDEX}/234588/firmware/v140/{NAME}"))
        assert body["images"]["cudy,wr3000e-v1"]["url"].startswith(YANDEX)

    def test_our_domain_is_the_fallback(self):
        body = firmware.manifest_of(self._release(""))
        assert body["images"]["cudy,wr3000e-v1"]["url"] == (
            f"https://shop.example/firmware/images/v140/{NAME}"
        )

    def test_the_shape_of_the_manifest_is_untouched(self):
        """Формат разбирает прошивка, обновить которую можно только им же."""
        body = firmware.manifest_of(self._release("https://cdn.example/x.bin"))
        assert set(body["images"]["cudy,wr3000e-v1"]) == {"url", "sha256", "size"}


class TestTheGuardBeforePublishing:
    """Единственная проверка, которую можно сделать, не выкачивая образ обратно."""

    def _release_with(self, *, size: int, remote: str = "https://cdn.example/a.bin"):
        from core.models import FirmwareImage, FirmwareRelease

        release = FirmwareRelease(version=140, notes="", rollout=0, rollout_max=0)
        release.images = [
            FirmwareImage(
                model_key="cudy,wr3000e-v1",
                file_name=NAME,
                url_path=f"/firmware/images/v140/{NAME}",
                remote_url=remote,
                sha256="a" * 64,
                size_bytes=size,
            )
        ]
        return release

    @pytest.mark.asyncio
    async def test_a_matching_object_passes(self, bucket):
        fake = bucket()
        fake.objects[f"firmware/v140/{NAME}"] = 500
        storage = object_storage.from_mapping(FULL)
        assert await firmware.check_remote(self._release_with(size=500), storage) == []

    @pytest.mark.asyncio
    async def test_a_missing_object_is_caught(self, bucket):
        bucket()
        storage = object_storage.from_mapping(FULL)
        assert await firmware.check_remote(self._release_with(size=500), storage) == [NAME]

    @pytest.mark.asyncio
    async def test_a_half_uploaded_object_is_caught(self, bucket):
        """Недолитый объект существует и отдаётся — по sha256 он не сойдётся,
        а выглядит как рабочая ссылка ровно до прихода парка."""
        fake = bucket()
        fake.objects[f"firmware/v140/{NAME}"] = 120
        storage = object_storage.from_mapping(FULL)
        assert await firmware.check_remote(self._release_with(size=500), storage) == [NAME]

    @pytest.mark.asyncio
    async def test_images_served_by_us_are_not_asked_about(self, bucket):
        bucket()
        storage = object_storage.from_mapping(FULL)
        assert await firmware.check_remote(self._release_with(size=500, remote=""), storage) == []

    @pytest.mark.asyncio
    async def test_without_a_bucket_there_is_nothing_to_check(self):
        assert await firmware.check_remote(self._release_with(size=500), None) == []
