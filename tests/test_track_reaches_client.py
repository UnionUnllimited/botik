"""Трек-номер и счёт на доставку доходят до клиента.

Текст для клиента собирает основное приложение, а отправляет тот, у кого есть
токен, — веб-админка или топик заказа. Топик этот ответ выбрасывал: оператор
называл цену доставки, а счёт клиенту не уходил; вписывал трек-номер — клиент
видел его, только если сам открывал карточку заказа.

Сам трек к тому же уезжал лишь приложением к «Заказ отправлен». Перевозчик
выдаёт номер когда придётся, часто уже после отгрузки, — и такой номер
не доходил вовсе.

Проверка по исходнику: `order_topics` тянет `aiogram` и `loguru` из окружения
бота, а у тестов своё.
"""

from __future__ import annotations

from pathlib import Path

from core import texts

ROOT = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class TestShippingEndpointBuildsTheNotice:
    API = _source("api/routes/catalog_api.py")

    def _body(self) -> str:
        start = self.API.index('@router.post("/manage/orders/{order_id}/shipping")')
        return self.API[start : self.API.index('@router.post("/manage/orders/{order_id}/device")')]

    def test_notice_and_recipient_are_returned(self):
        """Отправляет не ручка: у основного приложения нет токена бота."""
        body = self._body()
        assert "TRACK_ASSIGNED" in body
        assert '"tg_id"' in body and '"notice"' in body

    def test_removed_track_says_nothing(self):
        """«Трека больше нет» — сообщение, которое клиенту не нужно."""
        body = self._body()
        assert "changed" in body, "пустой номер не должен рождать уведомление"

    def test_text_names_the_order(self):
        filled = texts.TRACK_ASSIGNED.format(number="R-260830-0012")
        assert "R-260830-0012" in filled


class TestBothSendersDeliverIt:
    def test_topic_does_not_drop_the_answer(self):
        """Ответ ручки доходит до отправки, а не теряется в `_apply_text`."""
        source = _source("bot/src/order_topics.py")
        body = source[source.index("async def _apply_text") : source.index("@router.message")]
        assert "return error, data" in body, "ответ ручки нужен целиком: в нём notice"
        assert "async def _push_notice" in source
        assert "_push_notice(message.bot, result)" in source

    def test_topic_puts_long_links_on_buttons(self):
        """Адрес отслеживания и оплаты строкой в тексте никто не нажимает."""
        source = _source("bot/src/order_topics.py")
        body = source[source.index("async def _push_notice") :]
        body = body[: body.index("@router.message")]
        assert "pay_url" in body and "tracking_url" in body

    def test_web_admin_sends_it_too(self):
        """Второе место, где оператор вписывает трек, — форма в админке."""
        source = _source("bot/web_admin/routes/orders_shop.py")
        body = source[source.index("async def order_shop_shipping") :][:1400]
        assert "send_telegram_message" in body
        assert "tracking_url" in body
