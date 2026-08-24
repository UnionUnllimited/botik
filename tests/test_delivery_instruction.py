"""Инструкция по подключению и уведомление о доставке.

Момент, когда клиент держит коробку, — единственный, где инструкция ему
и нужна. Раньше сообщение о доставке говорило «включайте роутер и активируйте
подписку», хотя активировать ничего не надо, а куда смотреть — не сказано.

Инструкция лежит на самом роутере: так она не расходится с прошивкой
и открывается ещё до того, как появится интернет.
"""

from __future__ import annotations

from pathlib import Path

from core import texts
from core.enums import OrderStatus

ROOT = Path(__file__).resolve().parents[1]


class TestInstructionText:
    def test_default_address_is_the_router_itself(self):
        assert texts.DEFAULT_INSTRUCTION_URL.endswith("/instruction.html")
        assert "192.168." in texts.DEFAULT_INSTRUCTION_URL, (
            "адрес домашней сети: инструкция открывается с самого роутера"
        )

    def test_instruction_has_a_place_for_the_address(self):
        assert "{instruction}" in texts.DELIVERY_INSTRUCTION

    def test_steps_are_numbered(self):
        """Человек с коробкой читает по шагам, а не абзацем."""
        body = texts.DELIVERY_INSTRUCTION
        for step in ("1.", "2.", "3."):
            assert step in body

    def test_it_does_not_ask_to_activate_anything(self):
        """Подписка включается сама — просить об этом значит запутать."""
        filled = texts.DELIVERY_INSTRUCTION.format(instruction="x")
        assert "включится сама" in filled

    def test_delivered_notice_no_longer_asks_to_activate(self):
        assert "активируйте" not in texts.ORDER_STATUS_TEXTS[OrderStatus.DELIVERED]


class TestNoticeAssembly:
    def _source(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_instruction_is_attached_to_delivery(self):
        api = self._source("api/routes/catalog_api.py")
        end = '@router.post("/manage/orders/{order_id}/status")'
        body = api[api.index("def _status_notice") : api.index(end)]
        assert "OrderStatus.DELIVERED" in body
        assert "DELIVERY_INSTRUCTION" in body

    def test_address_comes_from_one_place(self):
        """Адрес читается из настройки, а запасной — один на весь проект.

        Два разных умолчания разъехались бы: клиент получил бы в сообщении
        один адрес, а на экране «Мой роутер» другой.
        """
        api = self._source("api/routes/catalog_api.py")
        assert api.count("router.instruction_url") == 2, (
            "настройка читается в уведомлении и на экране роутера — и больше нигде"
        )
        bot = self._source("bot/src/router_catalog.py")
        assert "192.168." not in bot, "бот не должен знать адрес наизусть, он приходит с данными"

    def test_bot_shows_it_as_a_button(self):
        """Кнопкой, а не адресом в тексте: её ищут тогда, когда читать нечего.

        Клавиатура одна на оба экрана — и на «роутер ещё не ожил», и на
        рабочий, — поэтому кнопка заводится в одном месте.

        Проверка по исходнику, а не вызовом: `router_catalog` тянет `loguru`
        и `aiogram` из окружения бота, а у тестов своё — сборка падала
        на импорте, ничего не проверив.
        """
        bot = self._source("bot/src/router_catalog.py")
        start = bot.index("def my_router_keyboard")
        # До следующей функции верхнего уровня, а не до конкретного имени:
        # соседа переименуют или вставят между ними новый — и проверка
        # начнёт смотреть в пустоту, ничего об этом не сказав.
        end = bot.index("\ndef ", bot.index("\n", start))
        keyboard = bot[start:end]
        assert 'data.get("instruction_url")' in keyboard
        assert "btn_router_instruction" in keyboard

    def test_the_button_is_registered(self):
        """Незарегистрированная кнопка покажет клиенту имя ключа."""
        registry = self._source("bot/button_registry.py")
        assert "btn_router_instruction" in registry
