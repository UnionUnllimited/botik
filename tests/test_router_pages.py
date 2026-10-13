"""Страницы роутеров в админке бота: подписка, связь и журнал — плашками.

Переделаны 11 октября 2026. Логика здесь в двух местах, и обе — на границах:

* ручка парка считает, сколько дней осталось (`_days_left`): «сегодня»
  и «вчера кончилась» отличаются на минуты, и путать их нельзя;
* макросы `_partials/router_ui.html` красят подписку по этому числу —
  жёлтая значит «пора напомнить», красная — «уже поздно».

Макросы рисуются настоящим Jinja: шаблонные тесты проверяют, что страница
собирается, а не какого цвета плашка у подписки на последнем дне.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import jinja2
import pytest

from api.routes.fleet_api import EXPIRING_SOON_DAYS, _days_left

TEMPLATES = Path(__file__).resolve().parents[1] / "bot" / "web_admin" / "templates"
NOW = dt.datetime(2026, 10, 11, 12, 0, tzinfo=dt.UTC)


class TestDaysLeft:
    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (dt.timedelta(days=10), 10),
            (dt.timedelta(days=10, hours=23), 10),
            (dt.timedelta(hours=5), 0),
            (dt.timedelta(minutes=-1), -1),
            (dt.timedelta(days=-3), -3),
        ],
    )
    def test_counts_whole_days(self, delta, expected):
        assert _days_left(NOW + delta, now=NOW) == expected

    def test_no_date_is_not_zero(self):
        """Нет срока — это не «кончается сегодня»: иначе подписка без даты
        горела бы жёлтым у каждого роутера."""
        assert _days_left(None, now=NOW) is None


@pytest.fixture(scope="module")
def ui():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)), autoescape=True)
    env.filters["msk_compact"] = lambda value: str(value or "")
    return env.get_template("_partials/router_ui.html").module


def _text(markup) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(markup))).strip()


def _tone(markup) -> str:
    found = re.search(r"um-pill (is-\w+)", str(markup))
    return found.group(1) if found else ""


class TestSubscriptionPill:
    def test_none(self, ui):
        assert _text(ui.sub_pill("", "", None)) == "нет"
        assert _tone(ui.sub_pill("", "", None)) == "is-muted"

    def test_waiting_for_the_router(self, ui):
        pill = ui.sub_pill("pending", "оплачена, ждёт роутера", None)
        assert _tone(pill) == "is-info"
        assert "ждёт роутера" in _text(pill)

    def test_plenty_left(self, ui):
        pill = ui.sub_pill("active", "активна", 40)
        assert _tone(pill) == "is-success"
        assert _text(pill) == "активна · 40 дней"

    def test_soon_is_the_same_threshold_as_the_filter(self, ui):
        """Жёлтая плашка и отбор «истекает» должны находить одни и те же
        роутеры: иначе фильтр показывает зелёные строки."""
        assert _tone(ui.sub_pill("active", "активна", EXPIRING_SOON_DAYS)) == "is-warning"
        assert _tone(ui.sub_pill("active", "активна", EXPIRING_SOON_DAYS + 1)) == "is-success"

    def test_last_day(self, ui):
        pill = ui.sub_pill("active", "активна", 0)
        assert _tone(pill) == "is-warning"
        assert _text(pill) == "кончается сегодня"

    def test_already_over_even_if_the_status_lags(self, ui):
        """Статус меняет воркер раз в несколько минут. Дата уже прошла —
        значит, истекла, что бы ни говорило поле."""
        pill = ui.sub_pill("active", "активна", -1)
        assert _tone(pill) == "is-danger"
        assert _text(pill) == "истекла"

    @pytest.mark.parametrize("status", ["expired", "cancelled"])
    def test_finished(self, ui, status):
        assert _tone(ui.sub_pill(status, "истекла", None)) == "is-danger"

    def test_old_api_without_days(self, ui):
        """Админка может приехать раньше ручки — тогда дней в ответе нет."""
        pill = ui.sub_pill("active", "активна", jinja2.Undefined())
        assert _tone(pill) == "is-success"
        assert _text(pill) == "активна"

    @pytest.mark.parametrize(
        ("days", "word"),
        [
            (1, "день"), (2, "дня"), (4, "дня"), (5, "дней"),
            (11, "дней"), (12, "дней"), (21, "день"), (22, "дня"),
        ],
    )
    def test_russian_plural(self, ui, days, word):
        assert _text(ui.plural_days(days)) == word


class TestLinkAndLog:
    def test_online(self, ui):
        assert _tone(ui.link_pill(True)) == "is-success"

    def test_silent_says_since_when(self, ui):
        pill = ui.link_pill(False, "11.10 14:20")
        assert _tone(pill) == "is-muted"
        assert "с 11.10 14:20" in _text(pill)

    @pytest.mark.parametrize(
        ("level", "word", "tone"),
        [("error", "ошибка", "is-danger"), ("warning", "внимание", "is-warning"),
         ("success", "готово", "is-success"), ("info", "событие", "is-muted")],
    )
    def test_levels_are_russian(self, ui, level, word, tone):
        pill = ui.level_pill(level)
        assert _text(pill) == word
        assert _tone(pill) == tone

    @pytest.mark.parametrize(("pct", "color"), [(10, "#10b981"), (70, "#f59e0b"), (95, "#ef4444")])
    def test_meter_colors(self, ui, pct, color):
        assert color in str(ui.meter(pct, "CPU"))

    def test_meter_without_reading(self, ui):
        assert "—" in _text(ui.meter(None, "CPU"))


def test_card_asks_the_api_for_what_it_draws():
    """Ссылка на клиента, «написать» и заказ рисуются по полям ручки —
    пусть ручка их и присылает."""
    path = Path(__file__).resolve().parents[1] / "api" / "routes" / "fleet_api.py"
    source = path.read_text(encoding="utf-8")
    card = source[source.index("async def router_card") : source.index("async def _device_or_404")]
    for field in ('"tg_id"', '"username"', '"order"', '"days_left"'):
        assert field in card, field


# ── Срок из панели ───────────────────────────────────────────────────────────
# Роутеры, проданные за наличные и включённые вручную, и включённые до
# 27 августа 2026: доступ есть, нашей подписки нет — парк писал «нет».


class TestPanelPill:
    def test_plenty(self, ui):
        pill = ui.panel_pill(68)
        assert _tone(pill) == "is-info", "синяя — чтобы не путать с нашей подпиской"
        assert _text(pill) == "в панели · 68 дней"

    def test_soon(self, ui):
        assert _tone(ui.panel_pill(EXPIRING_SOON_DAYS)) == "is-warning"

    def test_over(self, ui):
        assert _text(ui.panel_pill(-1)) == "в панели истекла"

    def test_unknown(self, ui):
        assert _text(ui.panel_pill(None)) == "нет"


def test_panel_expiries_are_laid_out_by_the_activation_rule():
    from core.models import Device
    from core.services.activation import apply_panel_expiries
    from core.services.remnawave import RemnaUser

    cash = Device(mac="D4:0D:AB:03:4B:CE")
    client = Device(mac="D4:0D:AB:2B:A4:EE")
    stock = Device(mac="D4:0D:AB:03:58:6E")
    accounts = [
        RemnaUser(uid="1", username="d4-0d-ab-03-4b-ce", expire_at="2026-12-21T00:00:00Z"),
        RemnaUser(uid="2", username="tg500_d40dab2ba4ee", expire_at="2026-11-01T00:00:00Z"),
        # Подписка для телефона того же клиента — к роутеру не относится.
        RemnaUser(uid="3", username="tg500", expire_at="2027-01-01T00:00:00Z"),
    ]
    found = apply_panel_expiries([cash, client, stock], accounts, now=NOW)

    assert found == 2
    assert cash.panel_expires_at == dt.datetime(2026, 12, 21, tzinfo=dt.UTC)
    assert client.panel_expires_at == dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
    assert stock.panel_expires_at is None
    assert stock.panel_synced_at == NOW, "«учётки нет» и «не сверяли» — разное"


def test_cash_sale_is_not_called_in_stock():
    """Без клиента, а доступ в панели живой — роутер стоит у покупателя."""
    page = (TEMPLATES / "routers_fleet.html").read_text(encoding="utf-8")
    head = page.index("item.panel_days_left is number and item.panel_days_left >= 0")
    assert "продан вне сайта" in page[head : head + 400]
