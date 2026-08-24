"""Окончание подписки и неоплаченная доставка.

Решения заказчика от 21 августа 2026:

  * доступ отключается день в день, льготных дней нет — раньше клиенту их
    обещали, а панель отключала в срок, и обещание было враньём;
  * за неоплаченную доставку напоминаем и ждём, заказ не отменяем и деньги
    за роутер не возвращаем.

Отдельно проверяется, что продление уезжает на тот роутер, к которому
привязана подписка: у владельца двух роутеров прежний код продлевал доступ
последнему активированному — один получал чужие дни, другой отключался
в оплаченный срок.
"""

from __future__ import annotations

from pathlib import Path

from core import texts
from core.config import settings

ROOT = Path(__file__).resolve().parents[1]


class TestNoGracePeriod:
    def test_grace_is_zero(self):
        assert settings.subscription.grace_days == 0

    def test_reminder_does_not_promise_extra_days(self):
        filled = texts.REMINDER_AFTER.format(days="1 день")
        assert "работает ещё" not in filled
        assert "отключён" in filled

    def test_reminder_has_no_grace_placeholder(self):
        """Оставшийся {grace} упал бы на `.format()` прямо в рассылке."""
        assert "{grace}" not in texts.REMINDER_AFTER

    def test_one_reminder_after_cutoff(self):
        assert settings.subscription.reminder_days_after == [1]

    def test_expired_subscriptions_still_get_the_reminder(self):
        """Льготы нет, статус меняется в тот же час — и без EXPIRED
        в выборке напоминание на следующий день не ушло бы никому."""
        source = (ROOT / "worker" / "tasks" / "subscriptions.py").read_text(encoding="utf-8")
        body = source[source.index("async def send_reminders") :]
        body = body[: body.index("async def expire_unactivated")]
        assert "SubscriptionStatus.EXPIRED" in body


class TestRenewalGoesToItsOwnRouter:
    def test_subscription_device_comes_first(self):
        source = (ROOT / "core" / "services" / "activation.py").read_text(encoding="utf-8")
        body = source[source.index("async def sync_panel_expiry") :]
        assert "subscription.device_id" in body
        assert body.index("subscription.device_id") < body.index("Device.user_id"), (
            "сперва роутер этой подписки, и только потом старый способ для тех, "
            "у кого привязки ещё нет"
        )

    def test_legacy_fallback_kept(self):
        """Подписки, заведённые до привязки к устройству, роутера не помнят."""
        source = (ROOT / "core" / "services" / "activation.py").read_text(encoding="utf-8")
        body = source[source.index("async def sync_panel_expiry") :]
        assert "activated_at.desc()" in body


class TestUnpaidDeliveryReminders:
    def _task(self) -> str:
        return (ROOT / "worker" / "tasks" / "orders.py").read_text(encoding="utf-8")

    def test_only_quoted_and_unpaid(self):
        body = self._task()
        assert "Delivery.quoted_at.is_not(None)" in body
        assert "Delivery.paid_at.is_(None)" in body

    def test_free_delivery_is_not_nagged(self):
        """Подаренная доставка помечается оплаченной сразу, но цена у неё ноль —
        без этого условия клиент получал бы счёт на ноль рублей."""
        assert "Delivery.price > 0" in self._task()

    def test_reminder_is_not_repeated(self):
        body = self._task()
        assert "delivery.reminded_day = marker" in body
        assert "delivery.reminded_day == marker" in body

    def test_blocked_client_stops_the_loop(self):
        """Иначе круг спотыкается об этот заказ каждые сутки до скончания века."""
        body = self._task()
        window = body[body.index("bot_blocked") :][:300]
        assert "reminded_day = marker" in window

    def test_no_dead_link_in_the_reminder(self):
        """Ссылка PLATEGA живёт пятнадцать минут — в напоминании она мертва."""
        assert "pay_url" not in self._task()
        assert "Мои заказы" in texts.DELIVERY_REMINDER

    def test_reminders_stop_eventually(self):
        from worker.tasks.orders import REMIND_AFTER_DAYS

        assert len(REMIND_AFTER_DAYS) <= 3
        assert tuple(sorted(REMIND_AFTER_DAYS)) == REMIND_AFTER_DAYS


class TestFreshPaymentLinkOnDemand:
    """Ссылка выдаётся по кнопке: выданная со счётом к вечеру уже мертва."""

    def _api(self) -> str:
        return (ROOT / "api" / "routes" / "catalog_api.py").read_text(encoding="utf-8")

    def test_endpoint_checks_the_owner(self):
        body = self._api()
        body = body[body.index("async def delivery_payment_link") :][:1500]
        assert "user.tg_id != tg_id" in body, "по номеру заказа нельзя выставить счёт чужому"

    def test_paid_delivery_is_not_billed_twice(self):
        body = self._api()
        body = body[body.index("async def delivery_payment_link") :][:1500]
        assert "delivery.paid_at is not None" in body

    def test_unquoted_delivery_has_no_link(self):
        body = self._api()
        body = body[body.index("async def delivery_payment_link") :][:1500]
        assert "delivery.quoted_at is None" in body
