"""Шесть вопросов при оформлении называются шестью во всех шести текстах.

Когда в опросе появился выбор перевозчика, шагов стало шесть. Дефолты
переписали, но на живой базе тексты уже засеяны, и разовая правка меняет
только те, что совпали с прежним значением слово в слово. Остальные
остались от прошлой версии — клиент читал «Шаг 3 из 6», а следом «Шаг 4
из 5» и справедливо решал, что его считают за дурака.

Счётчик — не вкус оператора, а факт об опросе. Слова его, число наше.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHOP_TEXTS = ROOT / "bot" / "src" / "shop_texts.py"


def _module():
    spec = importlib.util.spec_from_file_location("shop_texts_under_test", SHOP_TEXTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


texts = _module()


class TestTheDefaultsAgreeWithEachOther:
    def test_every_step_is_numbered_out_of_the_same_total(self):
        totals = {
            int(texts.STEP_COUNTER.search(value).group(2))
            for value in texts.step_counters().values()
        }
        assert len(totals) == 1, f"тексты обещают разное число шагов: {sorted(totals)}"

    def test_the_steps_run_from_one_to_the_total_without_gaps(self):
        numbers = sorted(
            int(texts.STEP_COUNTER.search(value).group(1))
            for value in texts.step_counters().values()
        )
        (total,) = {
            int(texts.STEP_COUNTER.search(value).group(2))
            for value in texts.step_counters().values()
        }
        assert numbers == list(range(1, total + 1))

    def test_the_carrier_question_is_counted(self):
        """Он и есть шестой: без него счётчик и разъехался."""
        assert "text_order_ask_carrier" in texts.step_counters()


class TestAnOldTextIsBroughtInLine:
    @pytest.mark.parametrize(
        ("key", "stale", "expected"),
        [
            ("text_order_ask_promo", "🎟 Шаг 5 из 5. Пришлите промокод.", "Шаг 6 из 6"),
            ("text_order_ask_speed", "🚚 Шаг 4 из 5. Как везём?", "Шаг 4 из 6"),
            ("text_order_ask_city", "🏙 Шаг 3 из 5. В какой город везём?", "Шаг 3 из 6"),
        ],
    )
    def test_the_counter_is_corrected(self, key, stale, expected):
        assert expected in texts.fix_step_counter(key, stale)

    def test_the_operator_keeps_his_own_words(self):
        """Правим число, а не текст: «везём» вместо «отправляем» — его решение."""
        fixed = texts.fix_step_counter("text_order_ask_speed", "🚚 Шаг 4 из 5. Как везём?")
        assert "Как везём?" in fixed and "🚚" in fixed

    def test_a_text_without_a_counter_is_left_alone(self):
        """Оператор убрал счётчик намеренно — возвращать его не наше дело."""
        plain = "Как отправляем?"
        assert texts.fix_step_counter("text_order_ask_speed", plain) == plain

    def test_a_key_that_never_had_one_is_left_alone(self):
        stray = "Шаг 1 из 9 — это про другое"
        assert texts.fix_step_counter("text_catalog_intro", stray) == stray

    def test_running_it_twice_changes_nothing(self):
        """Сверка идёт каждым запуском бота, а не один раз с отметкой."""
        once = texts.fix_step_counter("text_order_ask_promo", "🎟 Шаг 5 из 5. Промокод?")
        assert texts.fix_step_counter("text_order_ask_promo", once) == once


class TestItActuallyRunsOnStartup:
    def test_init_db_checks_the_counter(self):
        """Иначе исправление лежит в модуле и до живой базы не доходит."""
        source = (ROOT / "bot" / "db_helpers.py").read_text(encoding="utf-8")
        assert "fix_step_counter" in source
        assert "step_counters()" in source

    def test_it_is_not_hidden_behind_a_one_shot_mark(self):
        """Отметка «применено» и завела нас сюда: она отработала до того,
        как появился шестой вопрос, и второй раз уже не сработает."""
        source = (ROOT / "bot" / "db_helpers.py").read_text(encoding="utf-8")
        block = source[source.index("for key, correct in step_counters()") :][:600]
        assert "MARK" not in block
