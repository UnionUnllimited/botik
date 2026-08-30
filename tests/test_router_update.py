"""Кнопка «Обновить прошивку»: у клиента в боте и у оператора в карточке.

Роутер ходит за манифестом раз в сутки сам. Кнопка нужна там, где сутки ждать
нельзя: поддержка выпустила исправление и просит клиента нажать.

Проверяется то, что ломается молча: чужой роутер по номеру из кнопки,
перепрошивка молчащего устройства, круг нажатий, пока роутер перезагружается,
и синхронный запуск, который всегда обрывался бы по таймауту SSH.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes import catalog_api
from core.models import Device, DeviceEvent, User
from core.models.base import Base
from core.services import router_shell

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_for_sqlite(_type, _compiler, **_kwargs) -> str:
    """У журнала событий ключ BIGINT, а SQLite сам заполняет только INTEGER.

    Без этого вставка события падала на NOT NULL по `id` — и падала уже
    после похода к роутеру, то есть прошивка ставилась, а запрос отвечал
    ошибкой."""
    return "INTEGER"


class FakeShell:
    """Подменяет поход к роутеру: SSH в тестах нет."""

    def __init__(self, *, ok: bool = True, fail: bool = False) -> None:
        self.calls: list[str] = []
        self._ok = ok
        self._fail = fail

    async def run_quick(self, _device, name):
        self.calls.append(name)
        if self._fail:
            raise router_shell.ShellError("роутер молчит")
        return router_shell.CommandResult(
            command=router_shell.QUICK_COMMANDS[name][1],
            exit_status=0 if self._ok else 1,
            stdout="запущено",
            stderr="",
        )


def fake_limiter(*, allowed: bool):
    class _Limiter:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def hit(self, _bucket, *, limit, window_sec):
            return allowed, limit if allowed else 0

    return _Limiter


async def _session_with(*, owner_tg: int = 42, online: bool = True):
    """Клиент со своим роутером в базе на память."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[User.__table__, Device.__table__, DeviceEvent.__table__],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session = factory()
    user = User(tg_id=owner_tg, username="buyer")
    session.add(user)
    await session.flush()
    device = Device(mac="AA:BB:CC:DD:EE:FF", user_id=user.id, frp_online=online)
    session.add(device)
    await session.commit()
    return engine, session, device


class TestOwnership:
    """Номер роутера приходит из кнопки, а кнопку можно позвать с любым."""

    @pytest.mark.asyncio
    async def test_someone_elses_router_is_not_found(self, monkeypatch) -> None:
        engine, session, device = await _session_with(owner_tg=42)
        shell = FakeShell()
        monkeypatch.setattr(catalog_api, "RateLimiter", fake_limiter(allowed=True))
        monkeypatch.setattr(catalog_api.router_shell, "run_quick", shell.run_quick)
        try:
            with pytest.raises(HTTPException) as exc:
                await catalog_api.my_router_update(
                    {"tg_id": 999, "device_id": device.id}, session=session
                )
            assert exc.value.status_code == 404
            assert shell.calls == []
        finally:
            await session.close()
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_unknown_client_is_not_found(self, monkeypatch) -> None:
        engine, session, _device = await _session_with(owner_tg=42)
        shell = FakeShell()
        monkeypatch.setattr(catalog_api, "RateLimiter", fake_limiter(allowed=True))
        monkeypatch.setattr(catalog_api.router_shell, "run_quick", shell.run_quick)
        try:
            with pytest.raises(HTTPException):
                await catalog_api.my_router_update({"tg_id": 7}, session=session)
            assert shell.calls == []
        finally:
            await session.close()
            await engine.dispose()


class TestWhenItRefuses:
    @pytest.mark.asyncio
    async def test_offline_router_is_not_touched(self, monkeypatch) -> None:
        """На молчащий роутер команде некуда прийти, а клиенту нужен ответ."""
        engine, session, device = await _session_with(online=False)
        shell = FakeShell()
        monkeypatch.setattr(catalog_api, "RateLimiter", fake_limiter(allowed=True))
        monkeypatch.setattr(catalog_api.router_shell, "run_quick", shell.run_quick)
        try:
            result = await catalog_api.my_router_update(
                {"tg_id": 42, "device_id": device.id}, session=session
            )
            assert result == {"ok": False, "error": "offline"}
            assert shell.calls == []
        finally:
            await session.close()
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_second_press_is_held_back(self, monkeypatch) -> None:
        """Роутер после установки перезагружается и минуты три молчит.
        Клиент видит «не отвечает» и жмёт снова — это круг перепрошивок."""
        engine, session, device = await _session_with()
        shell = FakeShell()
        monkeypatch.setattr(catalog_api, "RateLimiter", fake_limiter(allowed=False))
        monkeypatch.setattr(catalog_api.router_shell, "run_quick", shell.run_quick)
        try:
            result = await catalog_api.my_router_update(
                {"tg_id": 42, "device_id": device.id}, session=session
            )
            assert result == {"ok": False, "error": "too_often"}
            assert shell.calls == []
        finally:
            await session.close()
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_unreachable_router_is_reported(self, monkeypatch) -> None:
        engine, session, device = await _session_with()
        shell = FakeShell(fail=True)
        monkeypatch.setattr(catalog_api, "RateLimiter", fake_limiter(allowed=True))
        monkeypatch.setattr(catalog_api.router_shell, "run_quick", shell.run_quick)
        try:
            result = await catalog_api.my_router_update(
                {"tg_id": 42, "device_id": device.id}, session=session
            )
            assert result == {"ok": False, "error": "unreachable"}
        finally:
            await session.close()
            await engine.dispose()


class TestWhenItRuns:
    @pytest.mark.asyncio
    async def test_ota_command_goes_to_the_router(self, monkeypatch) -> None:
        engine, session, device = await _session_with()
        shell = FakeShell()
        monkeypatch.setattr(catalog_api, "RateLimiter", fake_limiter(allowed=True))
        monkeypatch.setattr(catalog_api.router_shell, "run_quick", shell.run_quick)
        try:
            result = await catalog_api.my_router_update(
                {"tg_id": 42, "device_id": device.id}, session=session
            )
            assert result["ok"] is True
            assert shell.calls == ["ota_now"]
        finally:
            await session.close()
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_event_says_who_started_it(self, monkeypatch) -> None:
        """Оператор должен отличать нажатие клиента от своего и от суточного круга."""
        engine, session, device = await _session_with()
        shell = FakeShell()
        monkeypatch.setattr(catalog_api, "RateLimiter", fake_limiter(allowed=True))
        monkeypatch.setattr(catalog_api.router_shell, "run_quick", shell.run_quick)
        try:
            await catalog_api.my_router_update(
                {"tg_id": 42, "device_id": device.id}, session=session
            )
            events = list(await session.scalars(select(DeviceEvent)))
            assert len(events) == 1
            assert "клиентом" in events[0].message
        finally:
            await session.close()
            await engine.dispose()


class TestOperatorCommand:
    """Готовая команда в карточке роутера."""

    def test_ota_is_in_the_closed_set(self) -> None:
        assert "ota_now" in router_shell.QUICK_COMMANDS
        assert "titan_ota.sh now" in router_shell.QUICK_COMMANDS["ota_now"][1]

    def test_ota_runs_detached(self) -> None:
        """Образ весит 27–54 МБ, а сессии отведено `ssh_timeout_sec` — пятнадцать
        секунд. Синхронный запуск обрывался бы на середине закачки, и оператор
        видел бы отказ у обновления, которое на самом деле идёт."""
        command = router_shell.QUICK_COMMANDS["ota_now"][1]
        assert command.startswith("nohup ")
        assert "&" in command

    def test_log_is_a_separate_button(self) -> None:
        """Ответ приходит минутами позже, и смотреть его надо отдельно."""
        assert "ota_log" in router_shell.QUICK_COMMANDS


class TestBotSide:
    SOURCE = (BOT_DIR / "src" / "router_catalog.py").read_text(encoding="utf-8")

    def test_button_only_for_a_router_on_air(self) -> None:
        """У выключенного роутера кнопка обещала бы то, чего не случится."""
        body = self.SOURCE[self.SOURCE.index("def my_router_keyboard") :]
        body = body[: body.index("btn_router_instruction")]
        gate = body[: body.index("btn_router_update")]
        assert 'router.get("online")' in gate

    def test_answers_cover_every_refusal(self) -> None:
        """Ручка отвечает кодом, и на каждый код у бота должен быть текст."""
        for code in ("offline", "too_often", "unreachable"):
            assert f'"{code}": "text_router_update_' in self.SOURCE

    def test_alert_is_trimmed(self) -> None:
        """Тексты правятся в админке, а Telegram длиннее двухсот знаков
        во всплывающем окне не принимает и роняет весь колбэк."""
        body = self.SOURCE[self.SOURCE.index("async def cq_router_update") :][:1600]
        assert "[:200]" in body


@pytest.fixture(scope="module")
def shop_api():
    """`bot/` — не пакет, импортом не взять."""
    spec = importlib.util.spec_from_file_location(
        "shop_api_router_update", BOT_DIR / "src" / "shop_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestShopApi:
    @pytest.mark.asyncio
    async def test_device_id_reaches_the_api(self, shop_api, monkeypatch) -> None:
        seen: dict = {}

        async def _post(path, payload):
            seen["path"] = path
            seen["payload"] = payload
            return {"ok": True}, ""

        monkeypatch.setattr(shop_api, "post", _post)
        await shop_api.update_router(42, 7)
        assert seen["path"] == "/api/v1/catalog/my-router/update"
        assert seen["payload"] == {"tg_id": 42, "device_id": 7}

    @pytest.mark.asyncio
    async def test_without_device_id_the_key_is_absent(self, shop_api, monkeypatch) -> None:
        """Пустой номер значит «первый по списку», и ручка его не должна видеть."""
        seen: dict = {}

        async def _post(path, payload):
            seen["payload"] = payload
            return {"ok": True}, ""

        monkeypatch.setattr(shop_api, "post", _post)
        await shop_api.update_router(42)
        assert seen["payload"] == {"tg_id": 42}
