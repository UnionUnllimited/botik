"""Меню админки бота: кому что показывается и чего в нём больше нет.

Меню собрано заново 11 октября 2026 под работу магазина роутеров. Здесь
стерегутся три вещи.

**Меню совпадает со стражем путей.** Права модератора проверяет
`_path_to_section` в `run.py`, а меню рисуется своими `moderator_can_see`.
Разойдись они — модератор видит пункт и получает 403, либо, хуже, страница
открыта, а пункт спрятан и о ней никто не помнит. Роутеры, склад, каталог,
заказы и прошивка модератору не выдаются вовсе: за ними консоль, root-пароли
клиентских роутеров и выкат на весь парк.

**Архив не распаковывается поверх кода.** Родная страница «Обновления»
принимала любой zip и раскрывала его в корень бота, который работает от root:
вход в панель был равен входу на сервер. Штатное использование было не лучше —
архив поставщика затёр бы все наши разделы разом.

**Следов чужого продукта в меню нет:** донат разработчику, справка
поставщика, его имя на странице входа.

Базовый шаблон целиком тестами не отрисовывается — он тянет весь каркас их
админки. Поэтому меню вырезается из него и рисуется отдельно, с настоящим
Jinja и заглушками `url_for` и прав.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import jinja2
import pytest

ADMIN = Path(__file__).resolve().parents[1] / "bot" / "web_admin"
TEMPLATES = ADMIN / "templates"
BASE = (TEMPLATES / "base.html").read_text(encoding="utf-8")
RUN = (ADMIN / "run.py").read_text(encoding="utf-8")
SETTINGS = (ADMIN / "routes" / "settings.py").read_text(encoding="utf-8")
SETTINGS_PAGE = (TEMPLATES / "settings_general.html").read_text(encoding="utf-8")

PREFIX = "/admin123/"


def _between(source: str, start: str, end: str) -> str:
    head = source.index(start)
    return source[head : source.index(end, head) + len(end)]


SIDEBAR = _between(BASE, '<nav class="tw-scroll', "</nav>")
BOTTOM = _between(BASE, "{% set _bn_dash", "</nav>")

# Разделы, которых нет в правах модератора, и страницы за ними.
ADMIN_ONLY = {
    "routers_fleet": "devices",
    "firmware_page": "devices",
    "domain_lists_page": "devices",
    "devices_stock": "devices",
    "orders_shop": "shop",
    "catalog_shop": "shop",
}
GUARD_PATHS = {
    "/routers": "devices",
    "/firmware": "devices",
    "/lists": "devices",
    "/stock": "devices",
    "/orders": "shop",
    "/catalog": "shop",
}


def _grantable() -> set[str]:
    match = re.search(r"MODERATOR_GRANTABLE = frozenset\(\s*\{([^}]*)\}", SETTINGS)
    assert match, "в settings.py нет MODERATOR_GRANTABLE"
    return set(re.findall(r"'(\w+)'", match.group(1)))


def _render(snippet: str, *, admin: bool, sections: tuple[str, ...] = (), path: str = "dashboard") -> str:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, autoescape=True)

    def url_for(endpoint: str, **_kw: object) -> str:
        return PREFIX + endpoint.split(".", 1)[1]

    def can_see(section: str) -> bool:
        return admin or section in sections

    return env.from_string(snippet).render(
        url_for=url_for,
        moderator_can_see=can_see,
        current_user=SimpleNamespace(is_admin=admin, is_authenticated=True),
        request=SimpleNamespace(path=PREFIX + path),
    )


def _links(html: str) -> set[str]:
    return {href[len(PREFIX) :] for href in re.findall(r'href="([^"]+)"', html) if href.startswith(PREFIX)}


class TestTheMenuMatchesTheGuard:
    def test_admin_sees_the_whole_shop_and_fleet(self):
        links = _links(_render(SIDEBAR, admin=True))
        assert set(ADMIN_ONLY) <= links

    def test_a_moderator_with_every_section_still_sees_none_of_it(self):
        links = _links(_render(SIDEBAR, admin=False, sections=tuple(_grantable())))
        leaked = sorted(set(ADMIN_ONLY) & links)
        assert not leaked, f"модератору видны страницы админа: {leaked}"

    def test_a_moderator_does_not_see_settings_or_bulk_actions(self):
        links = _links(_render(SIDEBAR, admin=False, sections=tuple(_grantable())))
        for endpoint in ("settings_general", "remnawave_dashboard", "bulk_actions_page"):
            assert endpoint not in links

    @pytest.mark.parametrize(("path", "section"), sorted(GUARD_PATHS.items()))
    def test_the_guard_keeps_these_pages_for_the_admin(self, path, section):
        mapping = dict(
            re.findall(
                r"\(\s*['\"](/[\w\-]+)['\"]\s*,\s*['\"](\w+)['\"]\s*\)",
                _between(RUN, "_path_to_section = [", "req_section = None"),
            )
        )
        assert mapping.get(path) == section
        assert section not in _grantable()

    def test_no_empty_section_headers(self):
        """Модератор с одним разделом не должен видеть заголовки пустых групп:
        «Магазин» без пунктов под ним выглядит как сломанное меню."""
        html = _render(SIDEBAR, admin=False, sections=("users",))
        headers = re.findall(r'tw-nav-section tw-sidebar-label">([^<]+)<', html)
        assert headers == ["Клиенты"]


class TestGrantableSections:
    def test_the_form_offers_exactly_what_the_server_accepts(self):
        """Форма и сервер берут разделы из разных мест — пусть совпадают.

        Раньше сервер принимал любой ключ `moderator_section_*`, и защитой
        была только форма: лишнее поле в запросе выдавало модератору консоль.
        """
        form = re.search(r"\{% for sid, label in \[(.*?)\] %\}", SETTINGS_PAGE, re.S)
        assert form
        offered = set(re.findall(r"\('(\w+)',", form.group(1)))
        assert offered == _grantable()

    def test_the_server_filters_by_the_list(self):
        head = SETTINGS.index("mod_sections = [")
        assert "in MODERATOR_GRANTABLE" in SETTINGS[head : head + 300]


class TestMobileBar:
    def test_admin_gets_orders_and_routers_under_the_thumb(self):
        links = _links(_render(BOTTOM, admin=True))
        assert {"orders_shop", "routers_fleet"} <= links

    def test_payments_never_lead_to_the_phone_subscription_page(self):
        """Родная `/payments` — подписка для телефона: оплат роутеров там нет."""
        for admin, sections in ((True, ()), (False, ("dashboard", "users", "payments"))):
            links = _links(_render(BOTTOM, admin=admin, sections=sections))
            assert "payments" not in links

    def test_a_moderator_gets_shop_payments_instead(self):
        links = _links(_render(BOTTOM, admin=False, sections=("dashboard", "users", "payments")))
        assert "payments_shop" in links
        assert not links & {"orders_shop", "routers_fleet"}


SOURCES = (*(ADMIN / "routes").glob("*.py"), ADMIN / "run.py")


class TestNothingUnpacksArchives:
    def test_no_archive_is_extracted_anywhere(self):
        offenders = [p.name for p in SOURCES if "extractall(" in p.read_text(encoding="utf-8")]
        assert not offenders, f"распаковка архива поверх кода: {offenders}"

    def test_the_updates_page_is_gone(self):
        for path in SOURCES:
            assert "route('/updates'" not in path.read_text(encoding="utf-8"), path.name
        assert not (TEMPLATES / "updates.html").exists()
        assert "'updates'" not in SETTINGS_PAGE


class TestNoVendorTraces:
    @pytest.mark.parametrize("trace", ["donate3xuistore", "docs.3xstore.ru", "3XUIStore", "xtls.github.io"])
    def test_menu_and_login(self, trace):
        login = (TEMPLATES / "login.html").read_text(encoding="utf-8")
        assert trace not in BASE
        assert trace not in login

    def test_the_version_does_not_name_the_vendor_product(self):
        version = (ADMIN.parent / "version.txt").read_text(encoding="utf-8")
        assert "RM-Only" not in version
