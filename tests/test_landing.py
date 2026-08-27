"""Витрина: что попадает на страницу, куда ведут кнопки и чего там не должно быть.

Витрина ничем не торгует сама — она уводит в бота по ссылке `?start=buy_<id>`.
Проверяется поэтому не «страница открылась», а три вещи: данные берутся те же,
что в боте; ссылка собирается рабочая; наружу не уезжает ни модель железа,
ни запрещённое слово.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from api.routes import landing as landing_route
from core.config import settings
from core.models import Plan, Product, Setting
from core.models.base import Base
from core.services import landing


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


class _NoRedis:
    """Заглушка кэша настроек: без неё каждое чтение ждёт таймаут сети."""

    async def get(self, _key):
        return None

    async def set(self, _key, _value, ex=None):
        return None

    async def delete(self, _key):
        return None


@pytest.fixture(scope="module")
def client():
    """Один клиент на модуль: старт приложения ждёт базу и Redis по таймауту."""
    from fastapi.testclient import TestClient

    from api.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    from core.services import settings_service

    def _fake_redis() -> _NoRedis:
        return _NoRedis()

    monkeypatch.setattr(settings_service, "get_redis", _fake_redis)


async def _session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[Product.__table__, Plan.__table__, Setting.__table__],
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _product(**kwargs) -> Product:
    values = {
        "slug": "tr3000",
        "title": "Роутер для квартиры",
        "subtitle": "Держит всю домашнюю сеть",
        "description": "Приезжает настроенным.",
        "model_code": "CUDY-TR3000",
        "price": Decimal("6900.00"),
        "stock": 5,
        "is_active": True,
        "sort_order": 10,
        "specs": {"Порты": "3 LAN", "Wi-Fi": "AX3000"},
    }
    values.update(kwargs)
    return Product(**values)


class TestContent:
    @pytest.mark.asyncio
    async def test_only_active_products_and_in_order(self, monkeypatch):
        monkeypatch.setattr(settings.app, "bot_username", "router_shop_bot")
        engine, factory = await _session()
        try:
            async with factory() as session:
                session.add_all(
                    [
                        _product(slug="second", title="Второй", sort_order=20),
                        _product(slug="hidden", title="Снят с продажи", is_active=False),
                        _product(slug="first", title="Первый", sort_order=10),
                    ]
                )
                await session.commit()

                content = await landing.page_content(session)

            titles = [card["title"] for card in content["products"]]
            assert titles == ["Первый", "Второй"]
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_buy_button_leads_to_the_card_in_the_bot(self, monkeypatch):
        """Ссылка открывает бота сразу на модели — иначе клиент ищет её заново."""
        monkeypatch.setattr(settings.app, "bot_username", "@router_shop_bot")
        engine, factory = await _session()
        try:
            async with factory() as session:
                session.add(_product())
                await session.commit()
                content = await landing.page_content(session)

            card = content["products"][0]
            assert card["buy_url"] == f"https://t.me/router_shop_bot?start=buy_{card['id']}"
            assert content["bot_url"] == "https://t.me/router_shop_bot"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_without_bot_username_there_is_no_dead_link(self, monkeypatch):
        """Пустой `APP_BOT_USERNAME` даёт кнопку в никуда — лучше подсказка."""
        monkeypatch.setattr(settings.app, "bot_username", "")
        engine, factory = await _session()
        try:
            async with factory() as session:
                session.add(_product())
                await session.commit()
                content = await landing.page_content(session)

            assert content["products"][0]["buy_url"] == ""
            assert content["bot_url"] == ""
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_hardware_model_does_not_leak(self, monkeypatch):
        """Клиенту показывается название товара, а не модель железа —
        то же решение, что на экране «Мой роутер»."""
        monkeypatch.setattr(settings.app, "bot_username", "router_shop_bot")
        engine, factory = await _session()
        try:
            async with factory() as session:
                session.add(_product(model_code="CUDY-TR3000"))
                await session.commit()
                content = await landing.page_content(session)

            assert "CUDY" not in str(content["products"][0])
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_plans_are_shown_with_monthly_price(self, monkeypatch):
        monkeypatch.setattr(settings.app, "bot_username", "router_shop_bot")
        engine, factory = await _session()
        try:
            async with factory() as session:
                session.add_all(
                    [
                        Plan(slug="m1", title="Месяц", months=1, price=Decimal("500.00")),
                        Plan(
                            slug="m12",
                            title="Год",
                            months=12,
                            extra_days=30,
                            price=Decimal("4800.00"),
                        ),
                    ]
                )
                await session.commit()
                content = await landing.page_content(session)

            year = content["plans"][1]
            assert year["period"] == "12 мес. +30 дн."
            assert year["price"] == "4 800 ₽"
            assert year["per_month"] == "400 ₽"
            # У месячного тарифа «в месяц» не пишем — это та же цифра дважды.
            assert content["plans"][0]["per_month"] == ""
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_texts_come_from_settings_when_operator_changed_them(self, monkeypatch):
        monkeypatch.setattr(settings.app, "bot_username", "router_shop_bot")
        engine, factory = await _session()
        try:
            async with factory() as session:
                session.add(Setting(key="landing.hero_title", value={"value": "Свой заголовок"}))
                await session.commit()
                content = await landing.page_content(session)

            assert content["hero_title"] == "Свой заголовок"
        finally:
            await engine.dispose()


class TestMoney:
    def test_thousands_are_separated(self):
        assert landing.money(Decimal("6900.00")) == "6 900 ₽"

    def test_kopecks_survive_when_they_are_not_zero(self):
        assert landing.money(Decimal("6900.50")) == "6 900,50 ₽"

    def test_zero_is_a_price_too(self):
        """Ноль — законная цена, а не «не считали»: доставку можно подарить."""
        assert landing.money(Decimal("0.00")) == "0 ₽"


class TestErrorsLookLikePages:
    def test_service_paths_keep_json(self):
        """Их читают провайдер оплаты, мониторинг и бот — HTML сломает разбор."""
        for path in ("/api/v1/catalog/products", "/webhooks/platega", "/healthz", "/metrics"):
            assert landing_route.is_page_request(path) is False

    def test_router_panel_paths_are_not_pages(self):
        """LuCI строит абсолютные ссылки и живёт в корне рядом с витриной."""
        for path in ("/cgi-bin/luci", "/luci-static/resources/cbi.js", "/ubus", "/panel/open"):
            assert landing_route.is_page_request(path) is False

    def test_everything_else_is_a_page(self):
        assert landing_route.is_page_request("/") is True
        assert landing_route.is_page_request("/kupit-router") is True


class TestRendering:
    def _render(self, content: dict) -> str:
        template = landing_route.templates.get_template("landing.html")
        return template.render(request=None, **content)

    @pytest.mark.asyncio
    async def test_page_shows_price_and_buy_button(self, monkeypatch):
        monkeypatch.setattr(settings.app, "bot_username", "router_shop_bot")
        engine, factory = await _session()
        try:
            async with factory() as session:
                session.add(_product())
                await session.commit()
                content = await landing.page_content(session)
        finally:
            await engine.dispose()

        html = self._render(content)
        assert "6 900 ₽" in html
        assert "?start=buy_" in html
        # «чат», а не «бот»: для покупателя это переписка в Telegram,
        # а слово «бот» он читает как «с живым человеком говорить не дадут».
        assert "Купить в чате" in html
        assert "3 LAN" in html

    @pytest.mark.asyncio
    async def test_operator_text_is_escaped(self, monkeypatch):
        """Название товара правит оператор в админке — оно идёт в HTML."""
        monkeypatch.setattr(settings.app, "bot_username", "router_shop_bot")
        engine, factory = await _session()
        try:
            async with factory() as session:
                session.add(_product(title="<script>alert(1)</script>"))
                await session.commit()
                content = await landing.page_content(session)
        finally:
            await engine.dispose()

        html = self._render(content)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    @pytest.mark.asyncio
    async def test_empty_catalog_does_not_break_the_page(self, monkeypatch):
        """Пустой каталог — обычное состояние до первой партии."""
        monkeypatch.setattr(settings.app, "bot_username", "router_shop_bot")
        engine, factory = await _session()
        try:
            async with factory() as session:
                content = await landing.page_content(session)
        finally:
            await engine.dispose()

        html = self._render(content)
        assert "Модели скоро появятся" in html


class TestRoute:
    """Маршрут целиком: страницу отдаёт он, а не только шаблон сам по себе."""

    @pytest.mark.asyncio
    async def test_root_returns_the_page(self, client, monkeypatch):
        from api.deps import get_session
        from api.main import app

        monkeypatch.setattr(settings.app, "bot_username", "router_shop_bot")
        engine, factory = await _session()
        async with factory() as session:
            session.add(_product())
            await session.commit()

        async def _session_override():
            async with factory() as session:
                yield session

        app.dependency_overrides[get_session] = _session_override
        try:
            response = client.get("/")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/html")
            assert "Роутер для квартиры" in response.text
        finally:
            app.dependency_overrides.pop(get_session, None)
            await engine.dispose()

    def test_unknown_page_answers_html_not_json(self, client):
        response = client.get("/kupit-router")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("text/html")

    def test_service_path_still_answers_json(self, client):
        """Ручку читает бот: страница с извинениями сломала бы разбор ответа."""
        response = client.get("/api/v1/catalog/products")
        assert response.headers["content-type"].startswith("application/json")


class TestForbiddenTerm:
    def test_landing_never_names_the_service_the_forbidden_way(self):
        """Слово из трёх букв не используется нигде — витрину читают все."""
        forbidden = "".join(("v", "p", "n"))  # само слово в репозитории не пишем
        sources = [
            (landing_route.TEMPLATES_DIR / "landing.html").read_text(encoding="utf-8"),
            (landing_route.TEMPLATES_DIR / "landing_off.html").read_text(encoding="utf-8"),
            (landing_route.TEMPLATES_DIR / "landing_error.html").read_text(encoding="utf-8"),
            (landing_route.STATIC_DIR / "landing.css").read_text(encoding="utf-8"),
            (landing_route.TEMPLATES_DIR / "instruction.html").read_text(encoding="utf-8"),
            (landing_route.TEMPLATES_DIR / "guide.html").read_text(encoding="utf-8"),
            str(landing.STEPS),
            str(landing.FEATURES),
            str(landing.FAQ),
            str(landing.MARQUEE),
            str(landing.HOME_POINTS),
            str(landing.COMPARISON),
            str(landing.INSTRUCTION_STEPS),
            str(landing.GUIDE_SECTIONS),
        ]
        for source in sources:
            assert forbidden not in source.lower()

    def test_no_text_block_slips_past_the_check(self):
        """Проверка растёт вместе с текстами сама.

        Присланная заказчиком вёрстка несла это слово трижды, и заметить его
        глазами в трёх экранах разметки — вопрос везения. Поэтому смотрим
        не список, который надо не забыть дополнить, а все текстовые блоки
        модуля: новый блок попадает под проверку сам.
        """
        forbidden = "".join(("v", "p", "n"))
        blocks = {
            name: value
            for name, value in vars(landing).items()
            if name.isupper() and isinstance(value, (str, list, tuple, dict))
        }
        assert len(blocks) >= 6, "тексты витрины перестали быть константами модуля"
        for name, value in blocks.items():
            assert forbidden not in str(value).lower(), f"{name} содержит запрещённое слово"


class TestWordingForBuyers:
    """Для покупателя это переписка в Telegram, а не «бот».

    Слово «бот» человек читает как «с живым человеком поговорить не дадут»,
    хотя отвечает там оператор. На витрине везде «чат».
    """

    def _visible_texts(self) -> str:
        from api.routes import landing as route

        pages = "".join(
            (route.TEMPLATES_DIR / name).read_text(encoding="utf-8")
            for name in ("landing.html", "instruction.html", "guide.html")
        )
        blocks = landing.STEPS + landing.FEATURES + landing.INSTRUCTION_STEPS + landing.GUIDE_SECTIONS
        return pages + " ".join(item["title"] + item["text"] for item in blocks)

    def test_no_bot_in_client_facing_text(self):
        import re

        # Именно слово, а не часть «работает» или «ноутбук».
        assert not re.search(r"\bбот\w*", self._visible_texts(), flags=re.IGNORECASE)

    def test_chat_is_used_instead(self):
        assert "чат" in self._visible_texts().lower()


class TestSetupStepsMatchTheRouter:
    """Шаги подключения списаны с инструкции, которая лежит на роутере.

    Придуманные шаги расходятся с тем, что человек видит на устройстве,
    и первым же несовпадением («какой ещё мастер?») отправляют его
    в поддержку.
    """

    def _text(self) -> str:
        return " ".join(
            item["title"] + item["text"] for item in landing.INSTRUCTION_STEPS
        ).lower()

    def test_cable_goes_to_wan(self):
        assert "wan" in self._text()

    def test_panel_address_is_named(self):
        assert "192.168.14.1" in self._text() or "titan.lan" in self._text()

    def test_wifi_networks_are_named(self):
        assert "titan-2.4" in self._text() and "titan-5" in self._text()

    def test_mac_binding_is_explained(self):
        """Самая частая причина «кабель воткнул, а интернета нет»."""
        assert "mac" in self._text()

    def test_no_forbidden_term(self):
        forbidden = "".join(("v", "p", "n"))
        assert forbidden not in self._text()


class TestLookIsOneAcrossPages:
    """Шапка, логотип и значок вкладки одинаковы на всех страницах витрины.

    Человек попадает на инструкцию по ссылке из чата, а не с главной,
    и оттуда должен уметь дойти до моделей — иначе он ищет адрес витрины
    заново или пишет в поддержку.
    """

    def _pages(self) -> dict[str, str]:
        from api.routes import landing as route

        return {
            name: (route.TEMPLATES_DIR / name).read_text(encoding="utf-8")
            for name in ("landing.html", "instruction.html", "guide.html")
        }

    def test_every_page_has_the_logo(self):
        for name, page in self._pages().items():
            assert "brand__logo" in page, name
            assert '{{ logo_url }}' in page, name

    def test_every_page_has_a_favicon(self):
        for name, page in self._pages().items():
            assert 'rel="icon"' in page, name

    def test_service_pages_have_it_too(self):
        """404 и выключенная витрина — тоже наши страницы."""
        from api.routes import landing as route

        for name in ("landing_error.html", "landing_off.html"):
            page = (route.TEMPLATES_DIR / name).read_text(encoding="utf-8")
            assert 'rel="icon"' in page, name

    def test_instructions_lead_back_to_the_catalog(self):
        for name in ("instruction.html", "guide.html"):
            page = self._pages()[name]
            assert "/#catalog" in page, name

    def test_logo_falls_back_to_our_own(self):
        """Оператор ничего не грузил — знак всё равно есть."""
        from api.routes import landing as route

        assert landing.DEFAULT_LOGO_URL == "/static/logo.svg"
        assert (route.STATIC_DIR / "logo.svg").exists()

    @pytest.mark.asyncio
    async def test_operator_logo_wins(self, monkeypatch):
        engine, factory = await _session()
        try:
            async with factory() as session:
                session.add(
                    Setting(key="landing.logo_url", value={"value": "/media/own-logo.png"})
                )
                await session.commit()
                assert await landing.logo_url(session) == "/media/own-logo.png"
        finally:
            await engine.dispose()


class TestConnectionTypes:
    """Тип подключения провайдера — на странице «Как подключить».

    Неверный тип — вторая по частоте причина «кабель воткнул, а интернета
    нет», сразу после привязки по MAC.
    """

    def test_all_four_are_listed(self):
        titles = " ".join(item["title"] for item in landing.CONNECTION_TYPES).lower()
        for kind in ("dhcp", "pppoe", "статический", "l2tp"):
            assert kind in titles

    def test_page_renders_them(self):
        from api.routes import landing as route

        page = (route.TEMPLATES_DIR / "instruction.html").read_text(encoding="utf-8")
        assert "connection_types" in page

    def test_no_forbidden_term(self):
        forbidden = "".join(("v", "p", "n"))
        blob = " ".join(i["title"] + i["text"] for i in landing.CONNECTION_TYPES).lower()
        assert forbidden not in blob


class TestOperatorUploadsHisOwnMarks:
    """Знак и значок вкладки грузятся файлом из админки.

    Ссылкой просить бесполезно: чужой адрес протухнет, а класть картинку
    рядом с товарами оператору некуда. Адрес после загрузки прописывает
    основное приложение само — копировать его руками во второе поле
    оператор бы не стал, а промахнувшись, получил бы витрину без знака.
    """

    def _api(self) -> str:
        from pathlib import Path

        return (
            Path(__file__).resolve().parents[1] / "api/routes/catalog_api.py"
        ).read_text(encoding="utf-8")

    def test_both_kinds_are_accepted(self):
        from api.routes.catalog_api import LANDING_IMAGE_SETTINGS

        assert LANDING_IMAGE_SETTINGS == {
            "logo": "landing.logo_url",
            "favicon": "landing.favicon_url",
            "hero": "landing.hero_image_url",
        }

    def test_unknown_kind_is_refused(self):
        body = self._api()[self._api().index("async def upload_landing_image") :]
        body = body[: body.index("@router.get")]
        assert "Неизвестная картинка витрины" in body

    def test_setting_is_written_by_us(self):
        body = self._api()[self._api().index("async def upload_landing_image") :]
        body = body[: body.index("@router.get")]
        assert "set_setting" in body

    def test_path_not_absolute_url(self):
        """Витрину открывают и по другому домену: ссылка с прежним именем
        хоста после переезда вела бы в никуда."""
        body = self._api()[self._api().index("async def upload_landing_image") :]
        body = body[: body.index("@router.get")]
        assert "public_base_url" not in body

    def test_admin_page_has_both_fields(self):
        from pathlib import Path

        page = (
            Path(__file__).resolve().parents[1]
            / "bot/web_admin/templates/catalog_settings.html"
        ).read_text(encoding="utf-8")
        assert 'name="landing_logo"' in page
        assert 'name="landing_favicon"' in page

    @pytest.mark.asyncio
    async def test_favicon_falls_back_to_our_own(self):
        engine, factory = await _session()
        try:
            async with factory() as session:
                assert await landing.favicon_url(session) == landing.DEFAULT_FAVICON_URL
        finally:
            await engine.dispose()

    def test_two_marks_are_different_files(self):
        """В шапке нужна одна буква, во вкладке — знак целиком."""
        from api.routes import landing as route

        assert landing.DEFAULT_LOGO_URL != landing.DEFAULT_FAVICON_URL
        assert (route.STATIC_DIR / "favicon.svg").exists()


class TestOwnFileInStatic:
    """Знак можно заменить, просто закоммитив файл в статику.

    Это самый короткий путь для оператора: он кладёт `logo.png` рядом
    с нашим `logo.svg`, и витрина берёт его — без настроек, без правок кода
    и без загрузки через админку.
    """

    def test_png_wins_over_our_svg(self, tmp_path, monkeypatch):
        from api.routes import landing as route

        (tmp_path / "logo.svg").write_text("<svg/>", encoding="utf-8")
        (tmp_path / "logo.png").write_bytes(b"\x89PNG")
        monkeypatch.setattr(route, "STATIC_DIR", tmp_path)
        assert route.logo_fallback() == "/static/logo.png"

    def test_our_own_when_nothing_is_put(self, tmp_path, monkeypatch):
        from api.routes import landing as route

        monkeypatch.setattr(route, "STATIC_DIR", tmp_path)
        assert route.logo_fallback() == landing.DEFAULT_LOGO_URL
        assert route.favicon_fallback() == landing.DEFAULT_FAVICON_URL

    def test_favicon_takes_ico_too(self, tmp_path, monkeypatch):
        """`favicon.ico` — то, что отдаёт большинство рисовалок значков."""
        from api.routes import landing as route

        (tmp_path / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
        monkeypatch.setattr(route, "STATIC_DIR", tmp_path)
        assert route.favicon_fallback() == "/static/favicon.ico"


class TestHeroImage:
    """Картинка первого экрана — та же, что баннер над меню бота.

    Клиент приходит с витрины в чат и видит то же изображение: переход
    не рвётся. Ничего не поставили — берём фото первой модели, пустое место
    на первом экране хуже, чем фото товара.
    """

    @pytest.mark.asyncio
    async def test_setting_wins(self, monkeypatch):
        monkeypatch.setattr(settings.app, "bot_username", "router_shop_bot")
        monkeypatch.setattr(settings.api, "public_base_url", "https://shop.example")
        engine, factory = await _session()
        try:
            async with factory() as session:
                session.add(_product())
                session.add(
                    Setting(
                        key="landing.hero_image_url", value={"value": "/media/banner-a1.png"}
                    )
                )
                await session.commit()
                content = await landing.page_content(session)

            assert content["hero_image"] == "https://shop.example/media/banner-a1.png"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_falls_back_to_the_first_product(self, monkeypatch):
        monkeypatch.setattr(settings.app, "bot_username", "router_shop_bot")
        monkeypatch.setattr(settings.api, "public_base_url", "https://shop.example")
        engine, factory = await _session()
        try:
            async with factory() as session:
                session.add(_product(photo_url="/media/router.jpg"))
                await session.commit()
                content = await landing.page_content(session)

            assert content["hero_image"] == "https://shop.example/media/router.jpg"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_no_products_no_picture(self, monkeypatch):
        """Пустой каталог — просто без картинки, а не с битой ссылкой."""
        monkeypatch.setattr(settings.app, "bot_username", "router_shop_bot")
        engine, factory = await _session()
        try:
            async with factory() as session:
                content = await landing.page_content(session)
            assert content["hero_image"] == ""
        finally:
            await engine.dispose()


class TestPlanDescriptionHasNoTraffic:
    """Гигабайты в описании тарифа — след подписки для телефона.

    Тарифы заводились под неё, и в описании осталось «Будет добавлено: 15 GB».
    К роутеру это не относится: мы не считаем гигабайты и не продаём их —
    за роутером стоит квартира. Показать такую строку значит пообещать то,
    чего нет.
    """

    def _plan(self, description: str) -> Plan:
        return Plan(slug="m1", title="Месяц", months=1, price=Decimal("300.00"), description=description)

    def test_traffic_line_is_dropped(self):
        assert landing.plan_description(self._plan("➕ Будет добавлено: 15 GB")) == ""

    def test_russian_gigabytes_too(self):
        assert landing.plan_description(self._plan("Плюс 15 ГБ трафика")) == ""

    def test_meaningful_text_survives(self):
        plan = self._plan("Скидка при оплате за три месяца\n➕ Будет добавлено: 15 GB")
        assert landing.plan_description(plan) == "Скидка при оплате за три месяца"

    @pytest.mark.asyncio
    async def test_card_shows_the_cleaned_text(self, monkeypatch):
        monkeypatch.setattr(settings.app, "bot_username", "router_shop_bot")
        engine, factory = await _session()
        try:
            async with factory() as session:
                session.add(
                    Plan(
                        slug="m1",
                        title="Месяц",
                        months=1,
                        price=Decimal("300.00"),
                        description="➕ Будет добавлено: 0 GB",
                    )
                )
                await session.commit()
                content = await landing.page_content(session)

            assert content["plans"][0]["description"] == ""
        finally:
            await engine.dispose()


class TestFaviconTypeIsNotForced:
    """Тип значка не объявляем: файл кладёт оператор.

    Присланный `favicon.png` оказался JPEG с расширением `.png` — так бывает
    сплошь и рядом. Браузер разберёт содержимое сам, а объявленный неверно
    тип он отвергнет, и значка не будет вовсе.
    """

    def test_no_hardcoded_mime(self):
        from api.routes import landing as route

        for name in ("landing.html", "instruction.html", "guide.html"):
            page = (route.TEMPLATES_DIR / name).read_text(encoding="utf-8")
            marker = page[page.index('rel="icon"') : page.index('rel="icon"') + 120]
            assert "image/svg+xml" not in marker, name
