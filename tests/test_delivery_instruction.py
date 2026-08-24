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
    def test_default_address_is_our_page(self):
        """Умолчание — страница витрины, а не адрес в домашней сети.

        Раньше инструкция лежала на самом роутере: она не расходилась
        с прошивкой и открывалась до появления интернета. Но кнопка нужна
        раньше — когда посылка ещё едет, а роутера в сети нет вовсе,
        и адрес `192.168.*` в этот момент никуда не ведёт.
        """
        source = (ROOT / "api/routes/catalog_api.py").read_text(encoding="utf-8")
        body = source[source.index("async def _instruction_url") :]
        body = body[: body.index("\ndef ")]
        assert "/instruction" in body
        assert "192.168." not in body

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
        assert api.count("router.instruction_url") == 1, (
            "настройка читается одной функцией: адрес нужен в четырёх местах, "
            "и разойтись умолчаниям нельзя"
        )
        assert "async def _instruction_url" in api
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


class TestActivatedStatus:
    """«Активирован» — последний статус живого заказа.

    Ставит его не оператор, а активация: только она знает, что ссылка доехала
    до устройства. До этого «Отправлен» висел, пока кто-нибудь не вспомнит
    закрыть заказ руками, — а не вспоминал никто.
    """

    def _source(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_it_follows_shipped(self):
        from core.enums import OrderStatus
        from core.services.orders import ALLOWED_TRANSITIONS

        assert OrderStatus.ACTIVATED in ALLOWED_TRANSITIONS[OrderStatus.SHIPPED]
        assert OrderStatus.ACTIVATED in ALLOWED_TRANSITIONS[OrderStatus.DELIVERED]

    def test_nothing_follows_it_but_a_refund(self):
        from core.enums import OrderStatus
        from core.services.orders import ALLOWED_TRANSITIONS

        assert ALLOWED_TRANSITIONS[OrderStatus.ACTIVATED] == (OrderStatus.REFUNDED,)

    def test_activation_sets_it(self):
        source = self._source("core/services/activation.py")
        assert "async def mark_order_activated" in source
        # Оба пути активации: клиентом по MAC и руками из карточки.
        assert source.count("await mark_order_activated(session, device)") == 2

    def test_client_is_told(self):
        from core.enums import OrderStatus
        from core.texts import ORDER_STATUS_TEXTS

        assert OrderStatus.ACTIVATED in ORDER_STATUS_TEXTS
        assert "{number}" in ORDER_STATUS_TEXTS[OrderStatus.ACTIVATED]

    def test_every_side_knows_the_name(self):
        """Три словаря на трёх сторонах: у нас, в админке бота и в самом боте.
        Забытый статус показывается кодом `activated` живому человеку."""
        from core.enums import OrderStatus
        from core.texts import ORDER_STATUS_TITLES

        assert ORDER_STATUS_TITLES[OrderStatus.ACTIVATED]
        admin = self._source("bot/web_admin/routes/orders_shop.py")
        assert '"activated"' in admin
        bot = self._source("bot/src/router_catalog.py")
        assert '"activated"' in bot

    def test_instruction_button_disappears_after_activation(self):
        """Кнопка отвечает на вопрос «что делать с коробкой». После активации
        вопрос решён, и держать её на экране незачем."""
        api = self._source("api/routes/catalog_api.py")
        body = api[api.index("def _order_payload") :]
        body = body[: body.index("\n@router")]
        assert "OrderStatus.SHIPPED, OrderStatus.DELIVERED" in body
        assert "ACTIVATED" not in body

    def test_bot_shows_the_button_from_the_order(self):
        bot = self._source("bot/src/router_catalog.py")
        start = bot.index("def order_keyboard")
        end = bot.index("\ndef ", bot.index("\n", start))
        keyboard = bot[start:end]
        assert 'order.get("instruction_url")' in keyboard
        assert "btn_router_instruction" in keyboard
