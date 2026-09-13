"""Витрина обещает ровно то, что клиент потом найдёт.

Главное, чем этот роутер отличается от коробки с маркетплейса, — им можно
управлять из переписки: перезагрузить, сменить страну, выключить и включить
сервис, обновить прошивку, продлить срок. Всё это построено и работает,
а витрина об этом молчала: её рассказ был про «оно само работает, мы всё
берём на себя» — то есть про то, что клиент делать НЕ будет.

Здесь проверяется связка в обе стороны. Обещание без кнопки — это обман,
который всплывёт после оплаты. Кнопка без обещания — работа, за которую
никто не заплатил, потому что о ней не узнали.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LANDING = (ROOT / "core" / "services" / "landing.py").read_text(encoding="utf-8")
APP = (ROOT / "api" / "static" / "miniapp" / "app.js").read_text(encoding="utf-8")
SHOP_TEXTS = (ROOT / "bot" / "src" / "shop_texts.py").read_text(encoding="utf-8")

STOREFRONT = " ".join(
    LANDING[LANDING.index(block) : LANDING.index(block) + 3000]
    for block in ("FEATURES:", "HOME_POINTS:", "COMPARISON:")
).lower()
"""Весь продающий текст витрины одной строкой. Его читают и сайт,
и приложение, и бот — источник один, поэтому и проверка одна."""


class TestTheControlStoryIsTold:
    """То, ради чего это и затевалось."""

    @pytest.mark.parametrize(
        "word",
        ["перезагруз", "стран", "продлить", "прошивк", "панел"],
    )
    def test_the_verb_is_named(self, word):
        """Общее «всё под контролем» не работает: человек не догадывается,
        что именно ему можно, и считает, что ничего."""
        assert word in STOREFRONT, f"витрина не говорит про «{word}»"

    def test_telegram_is_named_as_the_place(self):
        assert "telegram" in STOREFRONT

    def test_the_full_panel_is_not_hidden(self):
        """«Простой путь и полный путь» — разные вещи, и вторую покупатель
        роутера спрашивает первым делом: не заперли ли ему устройство."""
        assert "полная панель" in STOREFRONT or "полный" in STOREFRONT


class TestEveryPromiseHasAButton:
    """Обещание, за которым нет кнопки, всплывёт после оплаты."""

    @pytest.mark.parametrize(
        ("promise", "proof"),
        [
            ("перезагруз", "'/router/reboot'"),
            ("стран", "'/router/node'"),
            ("прошивк", "'/router/update'"),
            ("панел", "panel_url"),
        ],
    )
    def test_the_app_can_actually_do_it(self, promise, proof):
        assert promise in STOREFRONT, f"обещания «{promise}» на витрине нет"
        assert proof in APP, f"витрина обещает «{promise}», а в приложении нет {proof}"


class TestTheNewcomerSeesWhyBeforeWhat:
    """Первый экран того, у кого ещё ничего нет."""

    def _screen(self) -> str:
        head = APP.index("if (!active && !d.router_available && !recent.length)")
        return APP[head : APP.index("bindCatalog();", head)]

    def test_the_three_arguments_are_shown(self):
        """Сервер присылал их в том же ответе, а экран выбрасывал: человек
        видел список свойств товара, не узнав, зачем товар нужен."""
        assert "p.value" in self._screen()

    def test_they_stand_before_the_features(self):
        screen = self._screen()
        assert screen.index("p.value") < screen.index("p.features")

    def test_the_features_keep_their_own_heading(self):
        """Иначе два списка подряд читаются одним длинным перечнем."""
        assert "Что это даёт дома" in self._screen()


class TestTheBotTellsItToo:
    """В чате каталог открывали и видели голый список моделей."""

    def _texts(self) -> dict[str, str]:
        for node in ast.walk(ast.parse(SHOP_TEXTS)):
            target = None
            if isinstance(node, ast.AnnAssign):
                target = getattr(node.target, "id", None)
            elif isinstance(node, ast.Assign):
                target = getattr(node.targets[0], "id", None)
            if target == "CATALOG_TEXTS":
                return {key: value for key, value, _ in ast.literal_eval(node.value)}
        raise AssertionError("CATALOG_TEXTS не нашёлся")

    def _legacy(self) -> dict[str, str]:
        for node in ast.walk(ast.parse(SHOP_TEXTS)):
            target = None
            if isinstance(node, ast.AnnAssign):
                target = getattr(node.target, "id", None)
            elif isinstance(node, ast.Assign):
                target = getattr(node.targets[0], "id", None)
            if target == "LEGACY_TEXTS":
                return ast.literal_eval(node.value)
        raise AssertionError("LEGACY_TEXTS не нашёлся")

    def test_the_catalog_intro_says_what_you_get(self):
        intro = self._texts()["text_catalog_intro"].lower()
        assert "переписк" in intro
        assert "перезагрузка" in intro

    def test_it_reaches_a_live_bot(self):
        """На живой базе текст уже засеян старым значением, и `INSERT OR
        IGNORE` его не тронет. Обновится только то, что совпадает с прежним
        дефолтом, — правленное оператором остаётся его решением."""
        legacy = self._legacy()
        assert "text_catalog_intro" in legacy, "без прежнего значения перепосев пройдёт мимо"
        assert legacy["text_catalog_intro"] != self._texts()["text_catalog_intro"]

    def test_the_mark_was_bumped(self):
        """Прежняя отметка уже стоит на сервере, и без нового номера круг
        не пройдёт заново — правка осталась бы только в репозитории."""
        assert 'REDESIGN_MARK = "ui_redesign_2026_08_v5_applied"' in SHOP_TEXTS


class TestTheExampleScreen:
    """Экран владельца, показанный тому, кто ещё не купил.

    Отзывов у магазина пока нет, потрогать роутер человек не может, а главное
    отличие от коробки с маркетплейса — как раз то, что происходит после
    покупки. Рассказать об этом словами мы уже пробовали; показать — честнее.

    Две вещи, которые тут легко испортить и обе дорого. Пример, не назвавший
    себя примером, — это выдуманные показания, выданные за настоящие. Пример,
    ходящий на сервер, — это запрос про роутер, которого нет, и ошибка на
    экране у того, кого мы только уговариваем.
    """

    def _view(self) -> str:
        head = APP.index("views.router = function (view)")
        return APP[head : APP.index("screen.querySelectorAll('[data-dev]')", head)]

    def test_it_says_it_is_an_example(self):
        assert "Это пример" in self._view()

    def test_the_heading_does_not_pretend_it_is_theirs(self):
        """«Мой роутер» над чужими показаниями — это обман в одно слово."""
        assert "'Так это выглядит'" in self._view()

    def test_it_never_asks_the_server(self):
        """Роутера нет, спрашивать про него нечего, а ошибка на витрине
        стоит дороже, чем весь этот экран."""
        assert "Promise.resolve(demoRouter())" in self._view()

    def test_the_real_handlers_are_skipped(self):
        """За каждым стоит запрос к роутеру: перезагрузка, прошивка, узлы.

        Срез кончается ровно на первом настоящем обработчике, поэтому
        достаточно проверить, что до него из ветки примера есть выход.
        """
        view = self._view()
        assert "if (demo) {" in view
        assert "return;" in view[view.index("if (demo) {") :]

    def test_the_buttons_answer_instead_of_doing_nothing(self):
        """Молчащая кнопка читается как сломанная — и как сломанный товар."""
        assert "Это пример экрана" in self._view()

    def test_the_service_block_is_shown_without_handlers(self):
        """Смена страны — половина рассказа: без неё пример показывает
        только показания, а управление опять остаётся на словах."""
        view = self._view()
        assert "renderAccess(accessSlot, DEMO_NODES, 0)" in view
        head = APP.index("function renderAccess")
        assert "if (!deviceId) { return; }" in APP[head : APP.index("views.router", head)]

    def test_it_ends_with_the_only_button_that_works(self):
        """Ради неё пример и показывается."""
        view = self._view()
        assert "openTab('catalog')" in view

    def test_the_numbers_are_counted_from_now(self):
        """Пример, собранный однажды, через полгода показывал бы подписку
        до прошлого марта рядом с «опрошен 4 минуты назад»."""
        head = APP.index("function demoRouter()")
        body = APP[head : APP.index("var DEMO_NODES", head)]
        assert "Date.now()" in body
        assert body.count("now") >= 4

    @pytest.mark.parametrize(
        "entry",
        [
            "Что видит владелец роутера",  # первый экран новичка
            "Посмотреть, как это выглядит",  # «Мой роутер» без роутера
        ],
    )
    def test_there_is_a_way_in(self, entry):
        assert entry in APP

    def test_both_ways_lead_to_the_example(self):
        assert APP.count("go({ name: 'router', demo: true })") == 2


def test_nothing_promises_a_trial_of_the_hardware():
    """Пробного периода у роутера нет: железо покупают. Обещание «попробуйте
    бесплатно» на витрине означало бы разговор в поддержке о том, чего мы
    не предлагали."""
    assert "бесплатно попроб" not in STOREFRONT
    assert "пробный период" not in STOREFRONT
