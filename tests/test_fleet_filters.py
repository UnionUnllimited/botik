"""Фильтры и массовые действия на странице роутеров.

Фильтр, показывающий не то, дороже отсутствующего: оператор отбирает
«подписки нет», видит десять строк и идёт по ним звонить — а там половина
чужих. Поэтому границы каждого значения проверяются поимённо.

«Нет подписки» и «на другом роутере» разведены намеренно: в первом случае
клиент не платил, во втором заплатил, а роутер срока не получил — это и есть
второй роутер, который молча не активировался.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.routes.fleet_api import (
    BULK_LIMITS,
    EXPIRING_SOON_DAYS,
    ROUTERS_PAGE_SIZES,
    _matches_link,
    _matches_sub,
)

NOW = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.UTC)


def device(device_id: int = 1, *, frp_online: bool = False, heartbeat: bool = False):
    return SimpleNamespace(
        id=device_id,
        frp_online=frp_online,
        is_online=lambda threshold_min, now: heartbeat,
    )


def subscription(device_id: int | None, *, expires_in_days: int | None = 30):
    expires = NOW + dt.timedelta(days=expires_in_days) if expires_in_days is not None else None
    return SimpleNamespace(device_id=device_id, expires_at=expires)


class TestLinkFilter:
    def test_empty_filter_keeps_everything(self):
        assert _matches_link(device(), "", now=NOW)

    def test_tunnel_counts_as_online(self):
        """Роутер за туннелем на связи, даже если heartbeat давно не приходил."""
        assert _matches_link(device(frp_online=True), "online", now=NOW)
        assert not _matches_link(device(frp_online=True), "offline", now=NOW)

    def test_fresh_heartbeat_counts_as_online(self):
        assert _matches_link(device(heartbeat=True), "online", now=NOW)

    def test_silent_router(self):
        assert _matches_link(device(), "offline", now=NOW)
        assert not _matches_link(device(), "online", now=NOW)


class TestSubscriptionFilter:
    """Одна подписка — один роутер.

    `subs` — своя подписка роутера и только она; подписка владельца, лежащая
    на другом его роутере, сюда не попадает вовсе. Она приходит отдельной
    картой `client_subs` и объясняет «нет»: клиент платил, а этот роутер срока
    не получил — то есть молча не активировался.
    """

    def test_empty_filter_keeps_everything(self):
        assert _matches_sub(device(), "", {}, {}, now=NOW)

    def test_none_means_no_subscription_of_its_own(self):
        assert _matches_sub(device(1), "none", {1: None}, {}, now=NOW)
        assert not _matches_sub(device(1), "none", {1: subscription(1)}, {}, now=NOW)

    def test_none_covers_a_router_whose_client_pays_for_another(self):
        """Своей подписки нет — значит «нет», сколько бы их ни было у клиента."""
        assert _matches_sub(device(1), "none", {1: None}, {1: subscription(2)}, now=NOW)

    def test_active_means_active_on_this_router(self):
        assert _matches_sub(device(1), "active", {1: subscription(1)}, {}, now=NOW)
        assert not _matches_sub(device(1), "active", {1: None}, {1: subscription(2)}, now=NOW)

    def test_elsewhere_finds_the_second_router(self):
        """Клиент заплатил, но срок ушёл первому роутеру — второй не активирован."""
        assert _matches_sub(device(1), "elsewhere", {1: None}, {1: subscription(2)}, now=NOW)
        assert not _matches_sub(device(1), "elsewhere", {1: subscription(1)}, {}, now=NOW)

    def test_elsewhere_needs_a_subscription(self):
        assert not _matches_sub(device(1), "elsewhere", {1: None}, {1: None}, now=NOW)

    def test_awaiting_activation_is_not_elsewhere(self):
        """Оплаченная и ещё не активированная не лежит ни на одном роутере:
        звать её «на другом» — врать оператору, он пойдёт искать второй роутер."""
        assert not _matches_sub(device(1), "elsewhere", {1: None}, {1: subscription(None)}, now=NOW)

    @pytest.mark.parametrize("days", [0, 1, EXPIRING_SOON_DAYS])
    def test_expiring_covers_the_next_week(self, days):
        assert _matches_sub(
            device(1), "expiring", {1: subscription(1, expires_in_days=days)}, {}, now=NOW
        )

    @pytest.mark.parametrize("days", [EXPIRING_SOON_DAYS + 1, 90])
    def test_far_away_is_not_expiring(self, days):
        assert not _matches_sub(
            device(1), "expiring", {1: subscription(1, expires_in_days=days)}, {}, now=NOW
        )

    def test_already_expired_is_not_expiring(self):
        """Истёкшая не «истекает»: звонить по ней поздно, это другой список."""
        assert not _matches_sub(
            device(1), "expiring", {1: subscription(1, expires_in_days=-1)}, {}, now=NOW
        )

    def test_endless_subscription_is_not_expiring(self):
        assert not _matches_sub(
            device(1), "expiring", {1: subscription(1, expires_in_days=None)}, {}, now=NOW
        )

    def test_expiring_on_another_router_is_not_ours(self):
        assert not _matches_sub(
            device(1), "expiring", {1: None}, {1: subscription(2, expires_in_days=1)}, now=NOW
        )


class TestPageSizes:
    def test_sizes_are_a_closed_list(self):
        """Иначе `per_page=100000` в адресе вернёт нас к странице во весь парк."""
        assert tuple(sorted(ROUTERS_PAGE_SIZES)) == ROUTERS_PAGE_SIZES
        assert all(size > 0 for size in ROUTERS_PAGE_SIZES)


class TestBulkAndFilterWiring:
    """Проверки по исходнику: фильтр, потерянный в ссылке, — молчаливая ошибка."""

    def _source(self, relative: str) -> str:
        return (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")

    def test_pagination_carries_every_filter(self):
        page = self._source("bot/web_admin/templates/routers_fleet.html")
        assert "**nav" in page, (
            "ссылки страниц должны нести все фильтры разом: перечисление по одному "
            "уже теряло их при добавлении нового"
        )

    def test_filter_keys_are_listed_once(self):
        route = self._source("bot/web_admin/routes/routers_fleet.py")
        assert "FLEET_FILTER_KEYS" in route
        for key in ("sub", "state", "model", "per_page"):
            assert f'"{key}"' in route

    def test_tunnel_actions_fit_into_the_page_timeout(self):
        """Предел свой у каждого действия.

        Общий в двести упирался в таймаут админки: перезагрузка десяти
        молчащих роутеров занимает больше, чем она ждёт. Оператор видел
        «приложение не ответило», роутеры при этом перезагружались, и второй
        щелчок перезагружал их снова.
        """
        # Пятнадцать секунд на молчащий роутер, восемь одновременно —
        # столько уложится в минуту с небольшим.
        for action in ("poll", "reboot", "command"):
            assert BULK_LIMITS[action] <= 40, f"{action}: слишком много за раз"
        assert BULK_LIMITS["activate"] <= 10, "активация идёт до минуты на роутер"
        assert BULK_LIMITS["status"] > BULK_LIMITS["poll"], (
            "записи в базе мгновенные — держать их на пределе похода к роутеру незачем"
        )

    def test_one_failure_does_not_cancel_the_rest(self):
        """Половина парка молчит всегда — опрос тридцати не должен падать целиком.

        Отказ роутера ловится внутри обхода и возвращается строкой, а не
        исключением наружу: иначе один молчащий отменил бы работу по всем.
        """
        api = self._source("api/routes/fleet_api.py")
        walk = api[api.index("async def _over_the_tunnel") : api.index("@router.post(\"/routers/bulk\"")]
        assert "except Exception" in walk, "отказ одного роутера должен оставаться внутри обхода"
        assert "return device," in walk, "и возвращаться причиной, а не падением"

    def test_tunnel_walk_touches_the_database_alone(self):
        """Сессия базы одна на запрос, и трогать её из нескольких задач сразу
        нельзя: туннели поднимаются до обхода, записи делаются после."""
        api = self._source("api/routes/fleet_api.py")
        walk = api[api.index("async def _over_the_tunnel") : api.index("@router.post(\"/routers/bulk\"")]
        assert walk.index("ensure_frp_binding") < walk.index("asyncio.gather"), (
            "туннели надо поднять до одновременного обхода, а не в нём"
        )

    def test_reboot_goes_through_the_tunnel(self):
        """Другого пути к роутеру нет: флаг в базе его не перезагрузит."""
        api = self._source("api/routes/fleet_api.py")
        body = api[api.index("async def bulk_routers") :]
        assert "router_shell.run(device, REBOOT_COMMAND)" in body

    def test_own_command_reuses_the_console_guard(self):
        """Массовая команда — тот же путь, что у консоли в карточке.
        Перепрошивка на сорока роутерах разом — сорок кирпичей у клиентов."""
        api = self._source("api/routes/fleet_api.py")
        body = api[api.index("async def bulk_routers") :]
        assert "FORBIDDEN_COMMANDS" in body

    def test_reboot_lets_ssh_close_first(self):
        """Без задержки роутер уходит в перезагрузку прямо в разговоре,
        и сработавшая команда выглядит как оборванная."""
        api = self._source("api/routes/fleet_api.py")
        assert "sleep" in api[api.index("REBOOT_COMMAND") : api.index("REBOOT_COMMAND") + 200]

    def test_quick_script_runs_on_many(self):
        """Готовый скрипт — из закрытого набора, тот же, что в карточке.

        Ради него и затевалось: «перезапустить туннель» спрашивают у парка
        целиком, а не у одного роутера, и вводить это руками сорок раз —
        сорок поводов опечататься в том, что уходит на устройство клиента.
        """
        api = self._source("api/routes/fleet_api.py")
        body = api[api.index("async def bulk_routers") :]
        assert "router_shell.QUICK_COMMANDS.get(quick_name)" in body
        assert "Неизвестный скрипт" in body
        assert BULK_LIMITS["quick"] <= 40, "тот же поход к роутеру, что и у команды"

    def test_quick_script_is_named_in_the_device_log(self):
        """Через полгода «Туннель: перезапустить» скажет больше, чем строка
        с путями к init-скриптам."""
        api = self._source("api/routes/fleet_api.py")
        body = api[api.index("async def bulk_routers") :]
        assert "Скрипт из админки" in body

    def test_scripts_come_from_one_place(self):
        """Список на странице парка и кнопки в карточке — один набор.
        Разойдись они, оператор запускал бы на многих не то, что на одном."""
        api = self._source("api/routes/fleet_api.py")
        assert api.count("router_shell.QUICK_COMMANDS") >= 2

    def test_actions_live_under_the_table(self):
        """Панель, а не окно по кнопке: отметил строки — видно, что с ними
        можно сделать, без лишнего щелчка."""
        page = self._source("bot/web_admin/templates/routers_fleet.html")
        assert "<dialog" not in page
        assert "data-fleet-run" in page

    def test_whole_row_opens_the_card(self):
        """Попасть в MAC мышью не проще, чем в строку, а открывают её каждый раз."""
        page = self._source("bot/web_admin/templates/routers_fleet.html")
        assert "data-fleet-href" in page
        assert "event.target.closest('a, input, button, select, label')" in page, (
            "иначе щелчок по отметке уводил бы со страницы"
        )

    def test_client_column_links_to_the_client(self):
        page = self._source("bot/web_admin/templates/routers_fleet.html")
        assert "admin.user_details" in page
        assert "client_tg_id" in page, (
            "карточка клиента открывается по telegram_id: внутренний номер "
            "админке бота ничего не говорит"
        )


class TestSelectPopupIsReadable:
    """Выпадающий список рисует браузер, и цвета ему надо задать явно.

    Правило ссылалось на `--admin-card`, которой в теме нет вовсе: фон молча
    откатывался к белому, а цвет текста подставлялся честный — светлый, по
    тёмной теме. Белым по белому список и выглядел.
    """

    ADMIN = Path(__file__).resolve().parents[1] / "bot" / "web_admin"

    def _admin(self, relative: str) -> str:
        return (self.ADMIN / relative).read_text(encoding="utf-8")

    def _theme(self) -> str:
        """Переменные темы разложены по нескольким файлам — читаем все."""
        return "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (self.ADMIN / "static" / "css").glob("*.css")
        )

    def test_option_colors_use_variables_that_exist(self):
        base = self._admin("templates/base.html")
        rule = base[base.index("select option") : base.index("select option") + 300]
        theme = self._theme()
        used = {name for name in ("--admin-bg-elevated", "--admin-text-base") if name in rule}
        assert used == {"--admin-bg-elevated", "--admin-text-base"}
        for name in used:
            assert f"{name}:" in theme, f"{name} в правиле есть, а в теме не задана"

    def test_the_missing_variable_is_gone(self):
        base = self._admin("templates/base.html")
        rule = base[base.index("select option") : base.index("select option") + 300]
        assert "--admin-card" not in rule, "этой переменной в теме нет — фон снова станет белым"


class TestSorting:
    """Порядок строк по колонке.

    Опасных мест два. Первое: пустые показания. Роутер, который ни разу
    не ответил, не должен занимать верх списка, отсортированного по загрузке, —
    ни при каком направлении. Второе: одинаковые значения. Без второго ключа
    база вправе переставлять такие строки между запросами, и на второй странице
    оператор увидит те же роутеры, что на первой.
    """

    DEFAULTS = {  # noqa: RUF012 — параметры ручки, а не изменяемое состояние
        "q": "", "link": "", "client": "", "sub": "", "state": "", "model": "",
        "page": 1, "per_page": 50,
    }

    async def _fleet(self, session, **kwargs):
        from api.routes.fleet_api import list_routers

        data = await list_routers(session=session, **(self.DEFAULTS | kwargs))
        return [item["mac"] for item in data["routers"]], data

    @pytest.fixture
    async def session(self):
        from sqlalchemy.dialects.postgresql import JSONB
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.ext.compiler import compiles

        from core.models import Device, User
        from core.models.base import Base

        @compiles(JSONB, "sqlite")
        def _jsonb(_type, _compiler, **_kwargs) -> str:
            return "JSON"

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            owner = User(tg_id=1, username="zebra")
            session.add(owner)
            await session.flush()
            # Третий роутер без показаний вовсе — он и должен уходить вниз.
            rows = (
                ("A0:00:00:00:00:01", 100, 5, 5, owner.id),
                ("A0:00:00:00:00:02", 900, 40, 1, None),
                ("A0:00:00:00:00:03", 500, None, 9, None),
            )
            for mac, uptime, cpu, clients, user_id in rows:
                session.add(
                    Device(
                        mac=mac, uptime_sec=uptime, cpu_pct=cpu, clients_wifi=clients,
                        clients_dhcp=0, user_id=user_id, frp_online=False,
                    )
                )
            await session.commit()
            yield session
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_biggest_first(self, session):
        order, _ = await self._fleet(session, sort="uptime", direction="desc")
        assert order == ["A0:00:00:00:00:02", "A0:00:00:00:00:03", "A0:00:00:00:00:01"]

    @pytest.mark.asyncio
    async def test_smallest_first(self, session):
        order, _ = await self._fleet(session, sort="uptime", direction="asc")
        assert order == ["A0:00:00:00:00:01", "A0:00:00:00:00:03", "A0:00:00:00:00:02"]

    @pytest.mark.asyncio
    async def test_empty_readings_stay_at_the_bottom(self, session):
        """И при «сначала большие», и при «сначала малые»."""
        down, _ = await self._fleet(session, sort="cpu", direction="desc")
        up, _ = await self._fleet(session, sort="cpu", direction="asc")
        assert down[-1] == "A0:00:00:00:00:03"
        assert up[-1] == "A0:00:00:00:00:03"

    @pytest.mark.asyncio
    async def test_client_column_sorts_too(self, session):
        """Телеграм клиента — свойство модели, а не колонка: считается после
        выборки. Роутеры без клиента всё равно внизу."""
        order, _ = await self._fleet(session, sort="client", direction="asc")
        assert order[0] == "A0:00:00:00:00:01"

    @pytest.mark.asyncio
    async def test_unknown_column_changes_nothing(self, session):
        """Колонка из чужой ссылки не должна ни падать, ни рисовать стрелку
        у заголовка, который ничего не сортирует."""
        order, data = await self._fleet(session, sort="teleport")
        assert data["sort"] == ""
        assert order == ["A0:00:00:00:00:03", "A0:00:00:00:00:02", "A0:00:00:00:00:01"]

    @pytest.mark.asyncio
    async def test_pages_do_not_repeat_rows(self, session):
        """Второй ключ по номеру: без него база вправе переставлять строки
        с одинаковым значением, и роутер попадёт на обе страницы."""
        first, _ = await self._fleet(session, sort="clients", page=1, per_page=25)
        second, _ = await self._fleet(session, sort="clients", page=1, per_page=25)
        assert first == second

    def test_sorting_is_carried_between_pages(self):
        """Забытая в ссылке сортировка сбрасывается на второй странице,
        и оператор видит не тот порядок, который выбрал."""
        source = (
            Path(__file__).resolve().parents[1] / "bot/web_admin/routes/routers_fleet.py"
        ).read_text(encoding="utf-8")
        keys = source[source.index("FLEET_FILTER_KEYS = ") :]
        keys = keys[: keys.index("\n")]
        assert '"sort"' in keys and '"dir"' in keys

    def test_headers_are_links(self):
        page = (
            Path(__file__).resolve().parents[1] / "bot/web_admin/templates/routers_fleet.html"
        ).read_text(encoding="utf-8")
        assert "sort_th(" in page
        assert "{{ sort_th('uptime'" in page
