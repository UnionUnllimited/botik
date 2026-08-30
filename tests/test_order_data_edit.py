"""Правка данных получателя оператором — только с причиной.

Клиент ошибается в адресе и телефоне, и до отгрузки это чинится звонком.
Чинить было негде: данные приходили из воронки и дальше только показывались.
Оператор переписывал их в чате перевозчика, а в заказе оставалось старое —
и следующая посылка ехала туда же.

Причина обязательна: это единственное место, где данные заказа меняются задним
числом, а журнала действий в проекте больше нет — его роль играет топик заказа,
куда причина и уходит.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

API = (ROOT / "api/routes/catalog_api.py").read_text(encoding="utf-8")
ADMIN = (ROOT / "bot/web_admin/routes/orders_shop.py").read_text(encoding="utf-8")
CARD = (ROOT / "bot/web_admin/templates/orders_shop_card.html").read_text(encoding="utf-8")


def _endpoint() -> str:
    start = API.index('@router.post("/manage/orders/{order_id}/customer")')
    return API[start : API.index('@router.post("/manage/orders/{order_id}/note")')]


class TestReasonIsRequired:
    def test_empty_reason_is_refused(self):
        body = _endpoint()
        assert "Напишите, почему меняете данные." in body

    def test_reason_reaches_the_topic(self):
        """Топик заказа и есть журнал: больше причину записать некуда."""
        body = _endpoint()
        assert "order_topics.push" in body
        assert "Причина:" in body

    def test_form_will_not_submit_without_it(self):
        block = CARD[CARD.index("order_shop_customer") :][:2000]
        assert 'name="reason"' in block
        assert "required" in block


class TestValuesAreCheckedLikeTheClientsAre:
    """Адрес без дома и телефон не тем форматом одинаково ломают доставку,
    кто бы их ни вводил — клиент в воронке или оператор в админке."""

    def test_same_cleaners_as_the_funnel(self):
        body = _endpoint()
        assert "_CLEANERS[field]" in body
        assert "_COMPLAINTS[field]" in body

    def test_nothing_to_change_is_not_a_save(self):
        body = _endpoint()
        assert "Данные и так такие же." in body


class TestDeliveryKeepsUpWithTheOrder:
    """У доставки свои поля получателя: по ним печатается накладная.
    Разойдясь с заказом, они отправили бы посылку по прежнему адресу."""

    def test_both_sides_are_written(self):
        body = _endpoint()
        for field in ("order.customer_name", "order.customer_phone", "order.customer_city"):
            assert field in body
        for field in ("delivery.recipient_name", "delivery.recipient_phone", "delivery.city"):
            assert field in body

    def test_pickup_address_is_not_overwritten_as_a_street(self):
        """Адрес пункта выдачи лежит в своём поле: записав его в `address`,
        мы получили бы заказ с двумя разными адресами сразу."""
        body = _endpoint()
        assert "delivery.pvz_address" in body


class TestOperatorSeesTheOutcome:
    def test_admin_reports_what_changed(self):
        body = ADMIN[ADMIN.index("async def order_shop_customer") :][:1500]
        assert 'data.get("changes")' in body
        assert "flash" in body
