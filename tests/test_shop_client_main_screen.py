"""Главное меню роутерного клиента без активного срока.

Положений три, и в базе бота они выглядят почти одинаково — активной подписки
нет ни в одном. Но значат они разное:

  * даты нет, подписка ждёт активации — роутер оплачен и ещё не включён,
    отсчёт не начался, и ждать действительно нужно;
  * дата есть и она в прошлом — срок кончился, роутер давно на связи,
    и человеку нужна кнопка продления;
  * даты нет и уже не будет — оплаченное сгорело, роутер так и не вышел
    на связь за отведённый срок.

Главное меню различало их по «есть ли активная подписка», а истёкшая активной
не считается. Клиент, у которого доступ закончился, читал «подписка оплачена
и ждёт роутера» — его звали ждать того, что случилось месяцы назад. Клиент,
у которого подписка сгорела, читал то же самое плюс «дни доставки не сгорают»
— прямо против письма, которое пришло ему в день сгорания.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parents[1] / "bot"


@pytest.fixture(scope="module")
def shop_texts():
    package = types.ModuleType("src")
    package.__path__ = [str(BOT / "src")]
    saved = sys.modules.get("src")
    sys.modules["src"] = package
    try:
        spec = importlib.util.spec_from_file_location(
            "src.shop_texts", BOT / "src" / "shop_texts.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["src.shop_texts"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if saved is None:
            sys.modules.pop("src", None)
        else:
            sys.modules["src"] = saved


class TestWhoIsWaitingForTheRouter:
    def test_no_date_means_waiting(self, shop_texts):
        """Зеркало магазина оставляет поле пустым, пока срок там не пошёл."""
        assert shop_texts.shop_client_is_waiting(None) is True
        assert shop_texts.shop_client_is_waiting("") is True

    def test_blank_is_not_a_date(self, shop_texts):
        assert shop_texts.shop_client_is_waiting("   ") is True

    @pytest.mark.parametrize(
        "moment",
        [
            "2025-01-01T00:00:00+00:00",  # давно прошла
            "2030-01-01T00:00:00+00:00",  # ещё впереди
        ],
    )
    def test_any_date_means_the_countdown_started(self, shop_texts, moment):
        """Прошлое или будущее — роутер уже выходил на связь, ждать нечего.

        Сюда мы попадаем только когда активной подписки нет, то есть дата
        в прошлом. Будущая проверяется на всякий случай: и она означает, что
        отсчёт начался, а не что надо ждать.
        """
        assert shop_texts.shop_client_is_waiting(moment) is False


class TestWhoseSubscriptionBurned:
    """Даты нет и уже не будет: подписка сгорела, не начав идти."""

    def test_a_dead_state_without_a_date_is_burned(self, shop_texts):
        assert shop_texts.shop_client_burned(None, "expired") is True
        assert shop_texts.shop_client_burned("", "cancelled") is True

    def test_such_a_client_is_not_waiting(self, shop_texts):
        assert shop_texts.shop_client_is_waiting(None, "expired") is False

    def test_a_pending_one_is_still_waiting(self, shop_texts):
        assert shop_texts.shop_client_is_waiting(None, "pending") is True
        assert shop_texts.shop_client_burned(None, "pending") is False

    def test_a_date_in_the_past_is_an_ordinary_expiry(self, shop_texts):
        """Срок шёл и кончился — это продление, а не сгорание."""
        assert shop_texts.shop_client_burned("2025-01-01T00:00:00+00:00", "expired") is False

    def test_an_unknown_state_keeps_the_old_answer(self, shop_texts):
        """До первого круга зеркала статус пуст у всех. Считать по нему
        сгоревшими значило бы сказать это каждому, кто просто ждёт роутер."""
        assert shop_texts.shop_client_is_waiting(None, "") is True
        assert shop_texts.shop_client_burned(None, "") is False
        assert shop_texts.shop_client_is_waiting(None, None) is True

    def test_the_case_of_the_state_does_not_matter(self, shop_texts):
        assert shop_texts.shop_client_burned(None, "EXPIRED") is True

    def test_it_says_the_same_as_the_letter(self, shop_texts):
        """Человек приходит в меню сразу после письма о сгорании."""
        assert "сгорела" in shop_texts.BURNED
        assert "поддержку" in shop_texts.BURNED

    def test_it_does_not_promise_days_do_not_burn(self, shop_texts):
        assert "не сгорают" not in shop_texts.BURNED


class TestTheMirrorCarriesTheState:
    """Без статуса из магазина бот эти положения не различит."""

    SOURCE = (BOT / "src" / "shop_sync.py").read_text(encoding="utf-8")

    def test_the_column_exists(self):
        assert '"shop_subscription_status": "TEXT"' in self.SOURCE

    def test_it_is_written_when_there_is_no_date(self):
        """Самый важный случай: именно здесь и ждущий, и сгоревший."""
        head = self.SOURCE.index("# Срок ещё не идёт")
        tail = self.SOURCE.index("continue", head)
        assert "shop_subscription_status" in self.SOURCE[head:tail]

    def test_it_is_written_when_there_is_one(self):
        head = self.SOURCE.index("UPDATE users SET subscription_end_date")
        assert "shop_subscription_status" in self.SOURCE[head : head + 400]

    def test_the_bot_creates_the_column_at_startup(self):
        """Админка читает колонки, не дожидаясь круга зеркала."""
        helpers = (BOT / "db_helpers.py").read_text(encoding="utf-8-sig")
        assert '("shop_subscription_status", "TEXT")' in helpers

    def test_the_shop_sends_it(self):
        """Статус в снимке — то, откуда он берётся."""
        api = (BOT.parent / "api" / "routes" / "catalog_api.py").read_text(encoding="utf-8")
        head = api.index("async def subscriptions_snapshot")
        assert '"status": str(state)' in api[head : head + 2000]


class TestTheMainScreenUsesIt:
    SOURCE = (BOT / "main.py").read_text(encoding="utf-8-sig")

    def _branch(self) -> str:
        head = self.SOURCE.index("elif is_shop_client:")
        return self.SOURCE[head : self.SOURCE.index("elif is_trial_used", head)]

    def test_the_three_states_are_told_apart(self):
        branch = self._branch()
        assert "shop_client_is_waiting" in branch
        assert "shop_client_burned" in branch

    def test_the_expired_one_is_offered_a_renewal(self):
        """Текст продления — тот же, что видит обычный клиент без подписки."""
        branch = self._branch()
        assert "text_subscription_expired_main" in branch
        assert "DEFAULT_SUBSCRIPTION_EXPIRED" in branch

    def test_waiting_is_still_said_to_the_one_who_waits(self):
        assert "WAITING_FOR_ROUTER" in self._branch()

    def test_the_burned_one_is_told_so(self):
        assert "BURNED" in self._branch()

    def test_waiting_is_asked_about_first(self):
        """Сгорание — редкий случай, ожидание роутера — основной."""
        branch = self._branch()
        assert branch.index("shop_client_is_waiting(") < branch.index("shop_client_burned(")

    def test_the_renew_button_is_there_for_him(self):
        """Текст зовёт продлить — кнопка обязана быть, и она не зависит
        от того, активна ли подписка."""
        keyboards = (BOT / "keyboards.py").read_text(encoding="utf-8")
        head = keyboards.index("if key == 'btn_renew_sub':")
        body = keyboards[head : keyboards.index("if key == 'btn_traffic_renewal':", head)]
        assert "has_active_sub" not in body


def test_the_expired_text_speaks_about_the_router(shop_texts):
    """Умолчание досталось от подписки для телефона — проверяем, что его
    успели переписать: роутерному клиенту «подключите приложение» бессмысленно."""
    source = (BOT / "main.py").read_text(encoding="utf-8-sig")
    head = source.index("DEFAULT_SUBSCRIPTION_EXPIRED = ")
    line = source[head : source.index("\n", head)]
    assert "роутер" in line.lower()
