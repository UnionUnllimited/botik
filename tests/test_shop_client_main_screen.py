"""Главное меню роутерного клиента без активного срока.

Положений два, и выглядят они в базе почти одинаково — активной подписки нет
ни там, ни там. Но значат они противоположное:

  * даты нет вовсе — роутер оплачен и ещё не включён, отсчёт не начался,
    и ждать действительно нужно;
  * дата есть и она в прошлом — срок кончился, роутер давно на связи,
    и человеку нужна кнопка продления.

Главное меню различало их по «есть ли активная подписка», а истёкшая активной
не считается. Клиент, у которого доступ закончился, читал «подписка оплачена
и ждёт роутера» — то есть его звали ждать того, что случилось месяцы назад,
и ни слова о том, что надо продлить.
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


class TestTheMainScreenUsesIt:
    SOURCE = (BOT / "main.py").read_text(encoding="utf-8-sig")

    def _branch(self) -> str:
        head = self.SOURCE.index("elif is_shop_client:")
        return self.SOURCE[head : self.SOURCE.index("elif is_trial_used", head)]

    def test_the_two_states_are_told_apart(self):
        assert "shop_client_is_waiting" in self._branch()

    def test_the_expired_one_is_offered_a_renewal(self):
        """Текст продления — тот же, что видит обычный клиент без подписки."""
        branch = self._branch()
        assert "text_subscription_expired_main" in branch
        assert "DEFAULT_SUBSCRIPTION_EXPIRED" in branch

    def test_waiting_is_still_said_to_the_one_who_waits(self):
        assert "WAITING_FOR_ROUTER" in self._branch()

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
