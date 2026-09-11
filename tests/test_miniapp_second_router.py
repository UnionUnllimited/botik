"""Четыре мелочи, каждая из которых видна клиенту.

Главная — про второго роутера. Экран «Мой роутер» умеет переключаться между
устройствами, и всё на нём спрашивается про открытое. Всё, кроме блока
«Сервис доступа»: он спрашивался вообще без номера, а без номера берётся
последний по счёту роутер. Клиент открывал старый и читал, включён ли
доступ и какой сервер выбран, — у нового. Нажатие при этом уходило на
правильный роутер: то есть человек видел одно, а менял другое.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes import catalog_api
from core.models import Device, User
from core.models.base import Base

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "api" / "static" / "miniapp" / "app.js").read_text(encoding="utf-8")
MINIAPP = (ROOT / "api" / "routes" / "miniapp.py").read_text(encoding="utf-8")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


async def _two_routers():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[User.__table__, Device.__table__]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        owner = User(tg_id=77, username="two")
        stranger = User(tg_id=88, username="other")
        session.add_all([owner, stranger])
        await session.flush()
        old = Device(mac="AA11BB22CC33", user_id=owner.id, frp_online=True)
        new = Device(mac="DD44EE55FF66", user_id=owner.id, frp_online=True)
        theirs = Device(mac="112233445566", user_id=stranger.id, frp_online=True)
        session.add_all([old, new, theirs])
        await session.commit()
        return engine, factory, old.id, new.id, theirs.id


@pytest.mark.asyncio
async def test_the_router_on_screen_is_the_one_asked_about():
    engine, factory, old_id, new_id, _ = await _two_routers()
    try:
        async with factory() as session:
            asked = await catalog_api._own_router(session, 77, old_id)
            assert asked.id == old_id, "открыт старый роутер — про него и вопрос"

            # А без номера по-прежнему последний: у клиента с одним роутером
            # кнопка его не передаёт, и ломать этот случай нельзя.
            fallback = await catalog_api._own_router(session, 77, 0)
            assert fallback.id == new_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_someone_elses_router_number_opens_nothing():
    """Номер приходит из приложения, а приложение можно позвать с любым."""
    engine, factory, _, _, theirs = await _two_routers()
    try:
        async with factory() as session:
            with pytest.raises(HTTPException) as exc:
                await catalog_api._own_router(session, 77, theirs)
            assert exc.value.status_code == 404
    finally:
        await engine.dispose()


def test_the_route_no_longer_hardcodes_the_newest_router():
    head = MINIAPP.index("async def router_nodes(")
    body = MINIAPP[head : MINIAPP.index("\n@router.", head + 1)]
    assert "device_id: int = 0" in body
    assert "device_id=0" not in body


def test_the_app_sends_the_router_it_is_showing():
    assert "'/router/nodes?device_id=' + r.id" in APP_JS


class TestWhatTheClientReadsWhenSomethingGoesWrong:
    def test_a_dropped_connection_speaks_russian(self):
        """Сорванный fetch отдаёт «Failed to fetch» — и это уезжало в alert."""
        head = APP_JS.index("function api(path, options)")
        body = APP_JS[head : APP_JS.index("\n  //", head)]
        assert ".catch(function () {" in body
        assert "Нет связи" in body
        # Ловим именно обрыв, а не ответ сервера: разбор кода состояния ниже
        # должен остаться, иначе «Сервер ответил 500» подменится связью.
        assert body.index(".catch(") < body.index("r.text()")
        # Код состояния остаётся в тексте: клиенту он ничего не говорит, но
        # назвать его поддержке — единственное, чем он может помочь.
        assert "r.status" in body

    def test_a_technical_code_never_reaches_the_client(self):
        """`detail` — код для разбора, а не текст.

        Клиент читал на экране «Не получилось — not_found» и шёл в поддержку
        выяснять, что это значит.
        """
        head = APP_JS.index("function api(path, options)")
        body = APP_JS[head : APP_JS.index("\n  //", head)]
        assert "CODES[body && body.detail]" in body
        assert "body.detail)" not in body, "код показывается только через словарь"


class TestALateAnswerBreaksNothing:
    def test_the_renewal_screen_may_already_be_gone(self):
        head = APP_JS.index("      function refresh() {")
        body = APP_JS[head : APP_JS.index("      }", head)]
        assert "!ends || !pay" in body, "иначе обращение к исчезнувшей строке роняет обработчик"


class TestOutsideTelegram:
    def test_the_tabs_do_not_pretend_to_work(self):
        """Обработчики вкладкам навешиваются в самом конце — до него не дойдём."""
        head = APP_JS.index("if (!tg || !tg.initData) {")
        body = APP_JS[head : APP_JS.index("  tg.ready();", head)]
        assert "nav.remove()" in body


def test_the_asset_version_moved():
    """Иначе клиент открывает приложение и получает старый файл из кэша.

    Номер поднимает тот, кто правил файл, — проверить это на месте нельзя.
    Здесь ловим то, что ловится: разъехавшиеся номера у стилей и скрипта
    (подняли один, забыли второй — и половина приложения старая) и откат
    назад ниже того номера, на котором это правило завели.
    """
    import re

    index = (ROOT / "api" / "static" / "miniapp" / "index.html").read_text(encoding="utf-8")
    versions = {
        name: int(version)
        for name, version in re.findall(r"app\.(js|css)\?v=(\d+)", index)
    }

    assert set(versions) == {"js", "css"}, "оба файла должны быть с номером"
    assert versions["js"] == versions["css"], f"номера разъехались: {versions}"
    assert versions["js"] >= 20, "номер не откатывают назад"


def test_a_device_that_never_reported_is_still_a_device():
    """`is_online` зовётся на роутере без единого показания — и не должен падать."""
    device = Device(mac="AA11BB22CC33", last_heartbeat_at=None)
    assert device.is_online(threshold_min=15) is False
    fresh = Device(mac="AA11BB22CC33", last_heartbeat_at=dt.datetime.now(dt.UTC))
    assert fresh.is_online(threshold_min=15) is True
