"""Раздача обновлений прошивки.

Формат манифеста разбирает прошивка роутера, и поменять его согласованно
нельзя: обновить прошивку можно только этим же манифестом. Поэтому тесты
здесь дословные — ключи, типы и вид значений, а не «примерно такой JSON».

Второе, что проверяется, — что sha256 и размер считает сервер, потоком.
Ошибка в одном знаке тихо отменяет обновление у всего парка: роутер бросает
закачку и молчит до следующих суток.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core.config import settings
from core.models import FirmwareImage, FirmwareRelease
from core.models.base import Base
from core.services import firmware

SUFFIX = firmware.IMAGE_SUFFIX
MODEL = "cudy,wr3000e-v1"
OTHER = "zbtlink,zbt-z8103ax-c"


class _Bytes:
    """Файл кусками — как его отдаёт `UploadFile`."""

    def __init__(self, data: bytes, chunk: int = 7) -> None:
        self._data = data
        self._chunk = chunk
        self._at = 0

    async def read(self, size: int) -> bytes:
        step = min(size, self._chunk)
        piece = self._data[self._at : self._at + step]
        self._at += len(piece)
        return piece


class _Redis:
    """Билеты живут в Redis; для теста хватает словаря с `getdel`."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def getdel(self, key):
        return self.store.pop(key, None)


@pytest.fixture(autouse=True)
def media(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.app, "media_dir", str(tmp_path))
    monkeypatch.setattr(settings.api, "public_base_url", "https://shop.example")
    return tmp_path


@compiles(BigInteger, "sqlite")
def _bigint_for_sqlite(_type, _compiler, **_kwargs) -> str:
    """SQLite нумерует сама только `INTEGER PRIMARY KEY`; на `BIGINT` она
    оставляет ключ пустым. В Postgres это по-прежнему `bigint`."""
    return "INTEGER"


async def _session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[FirmwareRelease.__table__, FirmwareImage.__table__],
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class TestSaveUpload:
    """Приём образа: считаем сами, пишем атомарно, чужое не берём."""

    @pytest.mark.asyncio
    async def test_hash_and_size_are_computed_by_us(self):
        data = b"firmware-image-bytes" * 100
        saved = await firmware.save_upload(
            version=140, model_key=MODEL, file_name=f"cudy-wr3000e{SUFFIX}", source=_Bytes(data)
        )
        assert saved.sha256 == hashlib.sha256(data).hexdigest()
        assert saved.size_bytes == len(data)
        assert len(saved.sha256) == 64
        assert saved.sha256 == saved.sha256.lower()

    @pytest.mark.asyncio
    async def test_file_lands_where_the_url_says(self, media):
        data = b"payload"
        saved = await firmware.save_upload(
            version=140, model_key=MODEL, file_name=f"cudy{SUFFIX}", source=_Bytes(data)
        )
        assert saved.url_path == f"/firmware/images/v140/cudy{SUFFIX}"
        on_disk = media / "firmware" / "images" / "v140" / f"cudy{SUFFIX}"
        assert on_disk.read_bytes() == data

    @pytest.mark.asyncio
    async def test_nothing_half_written_is_left_behind(self, media):
        """Роутеры тянут файл в произвольный момент: недописанного рядом быть не должно."""
        await firmware.save_upload(
            version=140, model_key=MODEL, file_name=f"cudy{SUFFIX}", source=_Bytes(b"payload")
        )
        names = [item.name for item in (media / "firmware" / "images" / "v140").iterdir()]
        assert names == [f"cudy{SUFFIX}"]

    @pytest.mark.asyncio
    async def test_factory_image_is_refused(self):
        """`factory.bin` весит столько же и выглядит так же, а sysupgrade им
        превращает роутер в кирпич."""
        with pytest.raises(firmware.FirmwareError):
            await firmware.save_upload(
                version=140, model_key=MODEL, file_name="cudy-factory.bin", source=_Bytes(b"x")
            )

    @pytest.mark.asyncio
    async def test_empty_file_is_refused(self):
        with pytest.raises(firmware.FirmwareError):
            await firmware.save_upload(
                version=140, model_key=MODEL, file_name=f"cudy{SUFFIX}", source=_Bytes(b"")
            )

    @pytest.mark.asyncio
    async def test_oversized_file_is_refused_without_being_kept(self, media, monkeypatch):
        monkeypatch.setattr(settings.app, "firmware_max_bytes", 16)
        with pytest.raises(firmware.FirmwareError):
            await firmware.save_upload(
                version=140, model_key=MODEL, file_name=f"cudy{SUFFIX}", source=_Bytes(b"x" * 100)
            )
        directory = media / "firmware" / "images" / "v140"
        assert list(directory.iterdir()) == []

    @pytest.mark.asyncio
    async def test_unknown_model_is_refused(self):
        with pytest.raises(firmware.FirmwareError):
            await firmware.save_upload(
                version=140, model_key="cudy,wr3000e", file_name=f"a{SUFFIX}", source=_Bytes(b"x")
            )

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("../../etc/passwd-sysupgrade.bin", "passwd-sysupgrade.bin"),
            ("C:\\tmp\\a-sysupgrade.bin", "a-sysupgrade.bin"),
            ("образ-sysupgrade.bin", "_-sysupgrade.bin"),
        ],
    )
    def test_name_from_the_form_is_not_trusted(self, given, expected):
        assert firmware.safe_file_name(given) == expected


class TestVersions:
    @pytest.mark.asyncio
    async def test_version_must_grow(self):
        engine, factory = await _session()
        try:
            async with factory() as session:
                await firmware.create_release(session, version=140, notes="")
                await session.commit()

                for refused in (140, 139, 0, -1):
                    with pytest.raises(firmware.FirmwareError):
                        await firmware.create_release(session, version=refused, notes="")
                    await session.rollback()

                assert await firmware.next_version(session) == 141
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_draft_counts_towards_the_next_number(self):
        """Иначе черновик 141 и опубликованный 140 позволили бы завести
        второй 141, а номер в таблице уникален."""
        engine, factory = await _session()
        try:
            async with factory() as session:
                await firmware.create_release(session, version=141, notes="")
                await session.commit()
                assert await firmware.next_version(session) == 142
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_second_draft_is_refused(self):
        """Черновик на странице один: образы второго стали бы невидимы,
        а место на диске занимали бы."""
        engine, factory = await _session()
        try:
            async with factory() as session:
                await firmware.create_release(session, version=141, notes="")
                await session.commit()
                with pytest.raises(firmware.FirmwareError, match="141"):
                    await firmware.create_release(session, version=142, notes="")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_words_instead_of_a_number_are_refused(self):
        engine, factory = await _session()
        try:
            async with factory() as session:
                with pytest.raises(firmware.FirmwareError):
                    await firmware.create_release(session, version="скоро", notes="")
        finally:
            await engine.dispose()


class TestRollout:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [(0, 0), (5, 5), (25, 25), (100, 100), (3, 5), (60, 50), (999, 100), (-5, 0), ("", 0)],
    )
    def test_value_snaps_to_a_step(self, given, expected):
        assert firmware.normalize_rollout(given) == expected

    @pytest.mark.asyncio
    async def test_stopping_keeps_the_reached_share_in_history(self):
        """Ноль в `rollout` — это остановка, а не «никто не обновился»."""
        engine, factory = await _session()
        try:
            async with factory() as session:
                release = await firmware.create_release(session, version=140, notes="")
                await firmware.attach_image(
                    session,
                    release,
                    model_key=MODEL,
                    saved=firmware.SavedImage(f"a{SUFFIX}", f"/firmware/images/v140/a{SUFFIX}", "a" * 64, 10),
                )
                await firmware.publish(session, release, rollout=50)
                await firmware.set_rollout(session, release, 0)
                await session.commit()

                assert release.rollout == 0
                assert release.rollout_max == 50
        finally:
            await engine.dispose()


class TestManifest:
    """Формат разбирает прошивка. Менять его нельзя — только вместе с ней."""

    @staticmethod
    def _saved(name: str, size: int, digest: str) -> firmware.SavedImage:
        return firmware.SavedImage(name, f"/firmware/images/v140/{name}", digest, size)

    @pytest.mark.asyncio
    async def test_published_release_is_the_manifest(self):
        engine, factory = await _session()
        try:
            async with factory() as session:
                release = await firmware.create_release(
                    session, version=140, notes="фикс WAN на WR3000E"
                )
                await firmware.attach_image(
                    session,
                    release,
                    model_key=MODEL,
                    saved=self._saved(f"cudy-wr3000e{SUFFIX}", 26951907, "a" * 64),
                )
                await firmware.publish(session, release, rollout=5)
                await session.commit()

                body = await firmware.manifest(session)

            assert body == {
                "version": 140,
                "notes": "фикс WAN на WR3000E",
                "rollout": 5,
                "images": {
                    "cudy,wr3000e-v1": {
                        "url": f"https://shop.example/firmware/images/v140/cudy-wr3000e{SUFFIX}",
                        "sha256": "a" * 64,
                        "size": 26951907,
                    }
                },
            }
            # Размер — число, а не строка: роутер сверяет его с длиной закачки.
            assert isinstance(body["images"][MODEL]["size"], int)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_empty_notes_are_left_out(self):
        engine, factory = await _session()
        try:
            async with factory() as session:
                release = await firmware.create_release(session, version=140, notes="")
                await firmware.attach_image(
                    session, release, model_key=MODEL, saved=self._saved(f"a{SUFFIX}", 1, "b" * 64)
                )
                await firmware.publish(session, release, rollout=100)
                await session.commit()
                assert "notes" not in await firmware.manifest(session)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_draft_is_not_given_out(self):
        """Иначе роутеры увидели бы номер выше своего раньше, чем догружены образы."""
        engine, factory = await _session()
        try:
            async with factory() as session:
                release = await firmware.create_release(session, version=140, notes="")
                await firmware.attach_image(
                    session, release, model_key=MODEL, saved=self._saved(f"a{SUFFIX}", 1, "b" * 64)
                )
                await session.commit()
                assert await firmware.manifest(session) == {
                    "version": 0,
                    "rollout": 0,
                    "images": {},
                }
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_nothing_published_yet_answers_with_an_empty_list(self):
        """Пустое, а не 404: прошивка вправе понять 404 как «адрес сменился»."""
        engine, factory = await _session()
        try:
            async with factory() as session:
                assert await firmware.manifest(session) == {
                    "version": 0,
                    "rollout": 0,
                    "images": {},
                }
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_model_without_an_image_simply_is_not_there(self):
        """Штатный способ приостановить одну модель: её роутеры ничего не делают."""
        engine, factory = await _session()
        try:
            async with factory() as session:
                release = await firmware.create_release(session, version=140, notes="")
                for key in (MODEL, OTHER):
                    await firmware.attach_image(
                        session, release, model_key=key, saved=self._saved(f"a{SUFFIX}", 1, "c" * 64)
                    )
                await firmware.publish(session, release, rollout=25)
                assert await firmware.detach_image(session, release, OTHER)
                await session.commit()

                body = await firmware.manifest(session)
            assert list(body["images"]) == [MODEL]
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_rollout_change_needs_no_rebuild(self):
        """Манифест собирается из базы: доля применяется той же секундой."""
        engine, factory = await _session()
        try:
            async with factory() as session:
                release = await firmware.create_release(session, version=140, notes="")
                await firmware.attach_image(
                    session, release, model_key=MODEL, saved=self._saved(f"a{SUFFIX}", 1, "d" * 64)
                )
                await firmware.publish(session, release, rollout=50)
                await session.commit()
                assert (await firmware.manifest(session))["rollout"] == 50

                await firmware.set_rollout(session, release, 0)
                await session.commit()
                after = await firmware.manifest(session)
            assert after["rollout"] == 0
            # Образы на месте: обновившиеся роутеры не откатываются, а тем,
            # кто уже качает, ссылка обрываться не должна.
            assert after["images"]
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_newer_published_release_wins(self):
        engine, factory = await _session()
        try:
            async with factory() as session:
                for version in (140, 141):
                    release = await firmware.create_release(session, version=version, notes="")
                    await firmware.attach_image(
                        session,
                        release,
                        model_key=MODEL,
                        saved=self._saved(f"a{SUFFIX}", 1, "e" * 64),
                    )
                    await firmware.publish(session, release, rollout=100)
                await session.commit()
                assert (await firmware.manifest(session))["version"] == 141
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_publishing_without_images_is_refused(self):
        engine, factory = await _session()
        try:
            async with factory() as session:
                release = await firmware.create_release(session, version=140, notes="")
                with pytest.raises(firmware.FirmwareError):
                    await firmware.publish(session, release, rollout=100)
        finally:
            await engine.dispose()


class TestDeletion:
    @pytest.mark.asyncio
    async def test_the_one_being_served_is_not_deleted(self, media):
        """Роутеры в эту минуту качают по этим ссылкам."""
        engine, factory = await _session()
        try:
            async with factory() as session:
                release = await firmware.create_release(session, version=140, notes="")
                saved = await firmware.save_upload(
                    version=140, model_key=MODEL, file_name=f"a{SUFFIX}", source=_Bytes(b"payload")
                )
                await firmware.attach_image(session, release, model_key=MODEL, saved=saved)
                await firmware.publish(session, release, rollout=100)
                await session.commit()

                with pytest.raises(firmware.FirmwareError):
                    await firmware.delete_release(session, release)
                await session.rollback()
            assert (media / "firmware" / "images" / "v140" / f"a{SUFFIX}").is_file()
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_draft_goes_away_with_its_files(self, media):
        engine, factory = await _session()
        try:
            async with factory() as session:
                release = await firmware.create_release(session, version=140, notes="")
                saved = await firmware.save_upload(
                    version=140, model_key=MODEL, file_name=f"a{SUFFIX}", source=_Bytes(b"payload")
                )
                await firmware.attach_image(session, release, model_key=MODEL, saved=saved)
                await session.commit()

                await firmware.delete_release(session, release)
                await session.commit()

                assert await session.get(FirmwareRelease, release.id) is None
            assert not (media / "firmware" / "images" / "v140").exists()
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_replacing_an_image_removes_the_old_file(self, media):
        engine, factory = await _session()
        try:
            async with factory() as session:
                release = await firmware.create_release(session, version=140, notes="")
                first = await firmware.save_upload(
                    version=140, model_key=MODEL, file_name=f"old{SUFFIX}", source=_Bytes(b"one")
                )
                await firmware.attach_image(session, release, model_key=MODEL, saved=first)
                second = await firmware.save_upload(
                    version=140, model_key=MODEL, file_name=f"new{SUFFIX}", source=_Bytes(b"two")
                )
                await firmware.attach_image(session, release, model_key=MODEL, saved=second)
                await session.commit()

                assert len(release.images) == 1
            names = sorted(item.name for item in (media / "firmware" / "images" / "v140").iterdir())
            assert names == [f"new{SUFFIX}"]
        finally:
            await engine.dispose()

    def test_stray_paths_are_not_followed(self, media, monkeypatch):
        """Путь приходит из базы, но обходить каталог по нему всё равно не даём."""
        outsider = media / "keep.txt"
        outsider.write_text("данные", encoding="utf-8")
        firmware.delete_file("/firmware/images/../../keep.txt")
        firmware.delete_file("/media/keep.txt")
        assert outsider.is_file()


class TestTicket:
    """Билет на загрузку: одноразовый, потому что закрывает приём файлов."""

    @pytest.fixture(autouse=True)
    def redis(self, monkeypatch):
        fake = _Redis()
        monkeypatch.setattr(firmware, "get_redis", lambda: fake)
        return fake

    @pytest.mark.asyncio
    async def test_ticket_is_spent_once(self):
        url = await firmware.issue_ticket(release_id=7, model_key=MODEL)
        raw = url.split("ticket=", 1)[1]

        target = await firmware.redeem_ticket(raw)
        assert target is not None
        assert (target.release_id, target.model_key) == (7, MODEL)
        assert await firmware.redeem_ticket(raw) is None

    @pytest.mark.asyncio
    async def test_forged_ticket_is_refused(self, redis):
        await firmware.issue_ticket(release_id=7, model_key=MODEL)
        stolen_id = next(iter(redis.store)).rsplit(":", 1)[-1]
        # Значение ключа без подписи не годится: билет подписан нашим секретом.
        assert await firmware.redeem_ticket(stolen_id) is None
        assert await firmware.redeem_ticket("") is None

    @pytest.mark.asyncio
    async def test_unknown_model_gets_no_ticket(self):
        with pytest.raises(firmware.FirmwareError):
            await firmware.issue_ticket(release_id=7, model_key="cudy,wr3000e")


class TestPublicPaths:
    def test_manifest_is_a_service_path_not_a_page(self):
        """Иначе 404 отдал бы прошивке HTML-страницу витрины."""
        from api.routes import landing

        assert not landing.is_page_request("/firmware/manifest.json")
        assert not landing.is_page_request("/firmware/images/v140/a-sysupgrade.bin")

    def test_manifest_address_is_absolute_and_fixed(self):
        assert firmware.manifest_url() == "https://shop.example/firmware/manifest.json"

    def test_manifest_answers_over_http(self, monkeypatch):
        """Ручку читает прошивка: без токена, JSON и с коротким кешем."""
        from fastapi.testclient import TestClient

        from api.deps import get_session
        from api.main import app

        async def _no_session():
            yield None

        async def _manifest(_session):
            return {"version": 140, "rollout": 5, "images": {}}

        monkeypatch.setattr(firmware, "manifest", _manifest)
        app.dependency_overrides[get_session] = _no_session
        try:
            with TestClient(app) as client:
                response = client.get("/firmware/manifest.json")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("application/json")
            assert "max-age" in response.headers["cache-control"]
            assert json.loads(response.text)["version"] == 140
        finally:
            app.dependency_overrides.pop(get_session, None)


class TestUploadOverHttp:
    """Приём образа целиком: билет, поток, запись, ответ.

    Ради этого пути всё и городилось: файл идёт из браузера оператора прямо
    сюда, минуя админку бота, и проверять его по частям бессмысленно —
    ломается стык.
    """

    @pytest.fixture(autouse=True)
    def redis(self, monkeypatch):
        fake = _Redis()
        monkeypatch.setattr(firmware, "get_redis", lambda: fake)
        return fake

    @pytest.mark.asyncio
    async def test_file_arrives_and_is_measured_by_us(self, media):
        from fastapi.testclient import TestClient

        from api.deps import get_transaction
        from api.main import app

        engine, factory = await _session()
        data = b"sysupgrade-image" * 500

        async with factory() as session:
            release = await firmware.create_release(session, version=140, notes="")
            await session.commit()
            release_id = release.id

        url = await firmware.issue_ticket(release_id=release_id, model_key=MODEL)
        ticket = url.split("ticket=", 1)[1]

        async def _transaction():
            async with factory() as session:
                yield session
                await session.commit()

        app.dependency_overrides[get_transaction] = _transaction
        try:
            with TestClient(app) as client:
                answer = client.post(
                    "/firmware/upload",
                    params={"ticket": ticket},
                    files={"image": (f"cudy-wr3000e{SUFFIX}", data, "application/octet-stream")},
                )
                assert answer.status_code == 200
                body = answer.json()
                assert body["ok"] is True
                assert body["sha256"] == hashlib.sha256(data).hexdigest()
                assert body["size"] == len(data)

                # Билет одноразовый: повторная отправка той же формы ничего
                # не перезапишет.
                again = client.post(
                    "/firmware/upload",
                    params={"ticket": ticket},
                    files={"image": (f"cudy-wr3000e{SUFFIX}", data, "application/octet-stream")},
                )
                assert again.status_code == 403

            async with factory() as session:
                stored = await firmware.get_release(session, release_id)
                assert list(stored.images) and stored.images[0].model_key == MODEL
                # Образ отдаётся тем же адресом, что уйдёт в манифест.
                served = media / "firmware" / "images" / "v140" / f"cudy-wr3000e{SUFFIX}"
                assert served.read_bytes() == data
        finally:
            app.dependency_overrides.pop(get_transaction, None)
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_upload_without_a_ticket_is_refused(self):
        from fastapi.testclient import TestClient

        from api.main import app

        with TestClient(app) as client:
            answer = client.post(
                "/firmware/upload",
                files={"image": (f"a{SUFFIX}", b"x", "application/octet-stream")},
            )
        assert answer.status_code == 403
