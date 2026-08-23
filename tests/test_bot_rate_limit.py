"""Ограничитель частоты не должен мешать оформлять заказ.

Оформление — это пять сообщений подряд: ФИО, телефон, город, адрес, промокод.
При лимите в десять сообщений на минуту клиент упирался в «Слишком много
сообщений» ещё до того, как доходил до адреса, — а с парой опечаток и вовсе
на первом шаге.

Ответы в начатом диалоге не флуд: их спросили мы сами. Ограничение остаётся
там, где оно нужно, — на сообщениях вне диалога.

Проверки по исходнику: middleware живёт в чужом `main.py`, который тянет
за собой половину бота и не импортируется без его окружения.
"""

from __future__ import annotations

from pathlib import Path

MAIN = (Path(__file__).resolve().parents[1] / "bot" / "main.py").read_text(encoding="utf-8")


def _throttling() -> str:
    """Тело middleware — от объявления класса до его регистрации."""
    start = MAIN.index("class ThrottlingMiddleware")
    return MAIN[start : MAIN.index("dp.message.middleware(throttling_middleware)")]


class TestDialogueIsNotFlood:
    def test_state_is_checked(self):
        body = _throttling()
        assert "state.get_state()" in body, (
            "ответы в диалоге должны пропускаться: их спросили мы сами"
        )

    def test_check_happens_before_the_limit(self):
        """Иначе проверка ничего не изменит: счётчик уже сработает."""
        body = _throttling()
        # Именно место вызова, а не объявление метода: оно стоит выше по файлу.
        assert body.index("state.get_state()") < body.index("await self._check_rate_limit("), (
            "выход по состоянию должен стоять до подсчёта лимита"
        )

    def test_dialogue_passes_to_the_handler(self):
        body = _throttling()
        window = body[body.index("state.get_state()") :][:200]
        assert "return await handler(event, data)" in window

    def test_broken_storage_does_not_open_the_gate(self):
        """Хранилище не ответило — проверяем лимит, а не пропускаем молча:
        иначе сбой в Redis снимал бы ограничение со всех разом."""
        body = _throttling()
        window = body[body.index("state.get_state()") :][:400]
        assert "except Exception" in window
        assert "pass" in window


class TestLimitStillApplies:
    def test_messages_outside_a_dialogue_are_counted(self):
        body = _throttling()
        assert "bot_rate_limit_message_max" in body

    def test_callbacks_have_their_own_budget(self):
        """Кнопок в оформлении больше, чем сообщений, и лимит у них свой."""
        body = _throttling()
        assert "bot_rate_limit_callback_max" in body

    def test_limiter_can_be_switched_off(self):
        assert "bot_rate_limit_enabled" in _throttling()
