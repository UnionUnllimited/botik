"""Карточка заказа в приложении: доставка и её оплата.

Доставка — отдельный счёт. Её цену называет оператор уже после оформления,
и в стоимость заказа она не входит никогда: `order.delivery_price` остаётся
нулём, а настоящая сумма живёт в `order.delivery.price`.

Карточка читала первое. В итоге клиент, которому в чат минуту назад
выставили 250 ₽ за перевозку, видел в приложении строку «Доставка 0 ₽»
ровно там, где ждал эту сумму, — и читал её как «везём бесплатно».

Оплатить перевозку оттуда было нельзя вовсе: кнопка жила только в чате,
и человек, видевший цену в приложении, шёл искать сообщение оператора
в переписке.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "api" / "static" / "miniapp" / "app.js").read_text(encoding="utf-8")
MINIAPP = (ROOT / "api" / "routes" / "miniapp.py").read_text(encoding="utf-8")
CATALOG = (ROOT / "api" / "routes" / "catalog_api.py").read_text(encoding="utf-8")


def _card() -> str:
    head = APP.index("views.order = function (view)")
    return APP[head : APP.index("views.catalog = function", head)]


class TestDeliveryIsNotPartOfTheTotal:
    def test_the_total_card_has_no_delivery_line(self):
        """Строка «Доставка» в итогах стояла там, где человек ждёт сумму
        перевозки, а показывала ноль — потому что в «Итого» её и нет."""
        card = _card()
        totals = card[card.index("line('Товары'") : card.index("delivery_summary")]
        assert "line('Доставка'" not in totals

    def test_it_says_delivery_is_paid_apart(self):
        assert "Доставка оплачивается" in _card()

    def test_the_real_price_is_shown(self):
        """`delivery` — это поле заказа и оно всегда ноль. Настоящая цена
        приходит отдельным полем."""
        card = _card()
        assert "o.delivery_price" in card
        assert "money(o.delivery, o.currency)" not in card

    def test_the_state_is_named(self):
        """Одной цены мало: 250 ₽ без пометки не отвечают, платить их
        уже надо или уже не надо."""
        card = _card()
        for state in ("not_quoted", "awaiting_payment", "paid"):
            assert state in card

    def test_free_delivery_is_not_zero_roubles(self):
        """Ноль — законная цена, и «0 ₽» на этом месте снова читается
        как «цену не посчитали»."""
        assert "'бесплатно'" in _card()


class TestPayingForDeliveryFromTheApp:
    def test_the_button_appears_only_when_there_is_what_to_pay(self):
        card = _card()
        head = card.index("id=\"pay-ship\"")
        window = card[head - 200 : head]
        assert "awaiting_payment" in window
        assert "shipPrice > 0" in window

    def test_it_asks_our_own_route(self):
        assert "'/orders/' + view.id + '/delivery-payment'" in _card()

    def test_the_route_exists(self):
        assert '@router.post("/api/orders/{order_id}/delivery-payment")' in MINIAPP

    def test_the_route_takes_the_signed_id(self):
        """`tg_id` из подписи, а не из тела: по одному номеру заказа нельзя
        выставлять счёт чужому человеку."""
        head = MINIAPP.index('@router.post("/api/orders/{order_id}/delivery-payment")')
        body = MINIAPP[head : head + 1200]
        assert "user: TelegramUser = Depends(current_user)" in body
        assert '"tg_id": user.tg_id' in body

    def test_the_server_refuses_an_unquoted_or_paid_delivery(self):
        """Проверки уже есть у общей ручки — приложение зовёт её же,
        а не повторяет условия у себя."""
        head = CATALOG.index("async def delivery_payment_link")
        body = CATALOG[head : head + 1500]
        assert "Стоимость доставки ещё не посчитана" in body
        assert "уже оплачена" in body

    def test_the_button_returns_to_normal_after_a_failure(self):
        """Иначе после отказа провайдера кнопка остаётся «Готовим…»
        и выглядит навсегда занятой."""
        card = _card()
        head = card.index("payShip.addEventListener")
        assert "payShip.disabled = false" in card[head:]
        assert "shipLabel" in card[head:]


CLEANER_CASES = {
    "Подписка: 🗓 180 дней": "Подписка: 180 дней",
    "🗓 180 дней": "180 дней",
    "🚀 Быстрая": "Быстрая",
    "СДЭК": "СДЭК",
    "Роутер Basic": "Роутер Basic",
}


def test_the_title_cleaner_reaches_the_middle_of_the_string():
    """Название срока оператор пишет как «🗓 180 дней», и в составе заказа
    оно становится «Подписка: 🗓 180 дней» — значок оказывается в середине,
    и обрезка начала его не достаёт. Так чужая цветная картинка и осталась
    посреди строки на экране, где весь остальной набор значков свой.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node не установлен — выполнить функцию приложения нечем")

    path = json.dumps(str(ROOT / "api/static/miniapp/app.js"))
    script = (
        "const fs=require('fs');"
        f"const src=fs.readFileSync({path},'utf8');"
        "const m=src.match(/var GLYPHS[\\s\\S]*?\\n  }\\n/);"
        "if(!m){throw new Error('plainTitle не нашёлся');}"
        "eval(m[0].replace(/^  /gm,''));"
        f"const cases={json.dumps(list(CLEANER_CASES))};"
        "const out={};for(const v of cases){out[v]=plainTitle(v);}"
        "process.stdout.write(JSON.stringify(out));"
    )
    done = subprocess.run(  # noqa: S603 — свой же файл, свой же node
        [node, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    assert json.loads(done.stdout) == CLEANER_CASES


def test_the_order_items_go_through_the_cleaner():
    assert "esc(plainTitle(it.title))" in _card()
