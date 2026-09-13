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


def test_nothing_promises_a_trial_of_the_hardware():
    """Пробного периода у роутера нет: железо покупают. Обещание «попробуйте
    бесплатно» на витрине означало бы разговор в поддержке о том, чего мы
    не предлагали."""
    assert "бесплатно попроб" not in STOREFRONT
    assert "пробный период" not in STOREFRONT
