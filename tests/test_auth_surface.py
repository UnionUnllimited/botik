"""Каждая ручка снаружи должна быть чем-то закрыта.

Ручек под сотню, и закрыты они по-разному: каталог и парк — общим токеном,
приложение — подписью Telegram, панель роутера и терминал — разовым билетом
в обмен на сессию, приём образа прошивки — тоже билетом, колбэки оплаты —
заголовками провайдера.

Открытых одиннадцать, и каждая открыта намеренно: за тремя приходит роутер,
которому токен не дашь (адрес зашит в прошивку открытым текстом); четыре —
публичные страницы витрины и оболочка приложения, где про клиента нет
ничего; две спрашивает docker; метрики закрыты не здесь, а на прокси.

Проверка нужна не сегодняшнему дню, а завтрашнему: новая ручка добавляется
одной строкой, и забыть у неё защиту легче всего именно тогда, когда рядом
полсотни закрытых — глазами это не ловится.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROUTES = Path(__file__).resolve().parents[1] / "api" / "routes"

OPEN_ON_PURPOSE = {
    # За манифестом приходит прошивка. Адрес зашит в неё открытым текстом,
    # токена у роутера нет и быть не может.
    ("firmware_api.py", "manifest"),
    # Списки доменов — то же самое: их читает роутер.
    ("lists_api.py", "proxy_domains"),
    ("lists_api.py", "proxy_ip"),
    # Оболочка приложения статическая, а подпись появляется только в
    # браузере Telegram: проверять её на странице нечем и незачем. Всё,
    # что стоит денег и знает про клиента, лежит за `/app/api/*`.
    ("miniapp.py", "app_page"),
    # Знак для заставки — перенаправление на картинку витрины.
    ("miniapp.py", "logo"),
    # Витрина, инструкция и руководство — то, ради чего домен и заведён.
    ("landing.py", "landing_page"),
    ("landing.py", "instruction_page"),
    ("landing.py", "guide_page"),
    # Жив ли процесс и готов ли он — спрашивает docker, а не человек.
    ("health.py", "healthz"),
    ("health.py", "readyz"),
    # Метрики закрыты не здесь, а на прокси: `deny all` во всех трёх
    # конфигах nginx. См. `test_metrics_are_not_public`.
    ("health.py", "metrics"),
}
"""Ручки без замка. Список закрытый: новая строка здесь — это решение,
а не следствие забытой зависимости."""

GUARDS = (
    "require_token",  # общий служебный токен: каталог, парк
    "current_user",  # подпись Telegram: приложение
    "redeem_ticket",  # разовый билет: приём образа прошивки
    "verify_webhook",  # заголовки провайдера: колбэки оплаты
    "_provider_or_404",  # то же, через разбор провайдера
    "_target(",  # сессия панели роутера: разрешает или отвечает отказом
    "terminal_ticket.redeem",  # разовый билет на терминал
    "_proxy(",  # то же, через общий переход к роутеру
    "terminal_ticket.load",  # cookie сессии терминала
    "panel_ticket.redeem",  # разовый билет на панель роутера
)


def _endpoints(path: Path):
    """Функции, повешенные на `@router.<метод>`, и весь их текст."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        routed = any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and isinstance(d.func.value, ast.Name)
            and d.func.value.id == "router"
            for d in node.decorator_list
        )
        if routed:
            yield node.name, ast.unparse(node)


def _module_guard(path: Path) -> bool:
    """Защита на всём роутере сразу: `APIRouter(dependencies=[...])`."""
    source = path.read_text(encoding="utf-8")
    head = source[source.index("router = APIRouter(") :][:400]
    return any(guard in head for guard in GUARDS)


def _all_endpoints():
    for path in sorted(ROUTES.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module_wide = _module_guard(path)
        for name, body in _endpoints(path):
            yield path.name, name, body, module_wide


ENDPOINTS = {(module, name): (body, wide) for module, name, body, wide in _all_endpoints()}


@pytest.mark.parametrize(("module", "endpoint"), sorted(ENDPOINTS))
def test_every_endpoint_is_locked(module, endpoint):
    body, module_wide = ENDPOINTS[(module, endpoint)]
    if (module, endpoint) in OPEN_ON_PURPOSE:
        pytest.skip("открыта намеренно — за ней приходит роутер")
    if module_wide:
        return
    assert any(guard in body for guard in GUARDS), (
        f"{module}:{endpoint} — ручка снаружи без замка. "
        f"Закройте её или внесите в OPEN_ON_PURPOSE с объяснением."
    )


def test_the_open_list_has_not_quietly_grown():
    """Иначе забытую защиту можно «починить», дописав строку в список."""
    assert len(OPEN_ON_PURPOSE) == 11


def test_there_are_endpoints_to_check_at_all():
    """Сломайся разбор — все проверки выше прошли бы на пустом списке."""
    assert len(ENDPOINTS) > 50


class TestTheFirmwareTicket:
    """Единственный замок на приёме прошивки — от него зависит весь парк."""

    SOURCE = (
        Path(__file__).resolve().parents[1] / "core" / "services" / "firmware.py"
    ).read_text(encoding="utf-8")

    def _redeem(self) -> str:
        start = self.SOURCE.index("async def redeem_ticket(")
        rest = self.SOURCE[start + 1 :]
        ends = [rest.index(mark) for mark in ("\ndef ", "\nasync def ") if mark in rest]
        return rest[: min(ends)] if ends else rest

    def test_it_is_unguessable(self):
        body = self.SOURCE[self.SOURCE.index("async def issue_ticket(") :][:600]
        assert "secrets.token_urlsafe(32)" in body

    def test_a_forged_ticket_is_refused(self):
        assert "BadSignature" in self._redeem()

    def test_it_burns_on_first_use(self):
        """`getdel` гасит билет тем же запросом, которым читает.

        Прочитать, а потом удалить — значит дать двум одновременным запросам
        пройти по одному билету.
        """
        assert "getdel" in self._redeem()

    def test_it_does_not_live_forever(self):
        from core.services import firmware

        assert 0 < firmware.TICKET_TTL_SEC <= 30 * 60


class TestMetricsAreNotPublic:
    """На `/metrics` висят выручка за сутки, число заказов и размер парка.

    В самом приложении эта ручка открыта намеренно: её спрашивает Prometheus
    изнутри, и городить там второй секрет незачем. Закрывает её прокси —
    значит проверять надо конфиги прокси, а не код."""

    CONFIGS = (
        "deploy/nginx/templates/default.conf.template",
        "deploy/nginx/origin/default.conf.template",
        "deploy/proxy/shop-site.conf",
    )

    @pytest.mark.parametrize("config", CONFIGS)
    def test_every_proxy_denies_it(self, config):
        text = (Path(__file__).resolve().parents[1] / config).read_text(encoding="utf-8")
        head = text.index("location = /metrics")
        assert "deny all;" in text[head : head + 120], config

    def test_the_smoke_run_checks_it_too(self):
        """Конфиг можно поправить и на сервере, мимо репозитория."""
        smoke = (
            Path(__file__).resolve().parents[1] / "deploy" / "smoke.sh"
        ).read_text(encoding="utf-8")
        assert "/metrics" in smoke
