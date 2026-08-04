"""Навигация бота: выход в меню с любого экрана и карточка «Мой роутер».

Главное, что здесь проверяется, — клиент не может застрять. Раньше кнопка
возврата была не на всех клавиатурах, и из середины оформления заказа
выбраться можно было только командой.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from bot.keyboards import inline
from bot.texts import ru
from core.dates import ago_phrase, uptime_phrase

# Все клавиатуры, кроме самого главного меню: из него возвращаться некуда.
SCREENS = {
    "back_to_menu": inline.back_to_menu,
    "skip_promo": inline.skip_promo,
    "waiting_for_text": inline.waiting_for_text,
    "product_card": lambda: inline.product_card(1, in_stock=True),
    "product_card_out_of_stock": lambda: inline.product_card(1, in_stock=False),
    "confirm_order": lambda: inline.confirm_order(online_enabled=True),
    "payment_link": lambda: inline.payment_link("https://pay.example", 1),
    "retry_payment": lambda: inline.retry_payment(1),
    "subscription_active": lambda: inline.subscription_actions(has_subscription=True),
    "subscription_none": lambda: inline.subscription_actions(has_subscription=False),
    "device_bound": lambda: inline.device_actions(has_device=True, has_subscription=True),
    "device_empty": lambda: inline.device_actions(has_device=False, has_subscription=False),
    "referral": lambda: inline.referral_share("https://t.me/bot?start=ref_1"),
    "delivery_targets": lambda: inline.delivery_targets(
        courier_price=Decimal("500"), pvz_price=Decimal("350")
    ),
}


def rows_of(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


class TestEscapeHatch:
    @pytest.mark.parametrize("name", sorted(SCREENS))
    def test_every_screen_leads_back_to_menu(self, name):
        rows = rows_of(SCREENS[name]())
        assert any(ru.BTN_MENU in text for text in rows[-1]), rows

    def test_main_menu_has_no_self_link(self):
        rows = rows_of(inline.main_menu())
        assert not any(ru.BTN_MENU in text for row in rows for text in row)

    def test_main_menu_lists_every_section(self):
        texts = [text for row in rows_of(inline.main_menu()) for text in row]
        assert texts == [
            ru.BTN_BUY,
            ru.BTN_MY_DEVICE,
            ru.BTN_SUBSCRIPTION,
            ru.BTN_GUIDES,
            ru.BTN_SUPPORT,
            ru.BTN_REFERRAL,
        ]

    @pytest.mark.parametrize("name", sorted(SCREENS))
    def test_navigation_row_stays_readable(self, name):
        """Больше двух кнопок в ряду схлопываются в огрызки на телефоне."""
        assert len(rows_of(SCREENS[name]())[-1]) <= 2

    def test_catalog_offers_menu_even_when_empty(self):
        assert any(ru.BTN_MENU in text for text in rows_of(inline.catalog([]))[-1])


class TestOrderFlowExits:
    """Внутри оформления нужна отмена, а не «назад»: возвращаться пришлось бы по шагам."""

    @pytest.mark.parametrize("name", ["skip_promo", "waiting_for_text", "confirm_order"])
    def test_flow_screens_offer_cancel(self, name):
        last = rows_of(SCREENS[name]())[-1]
        assert ru.BTN_CANCEL in last

    def test_product_card_offers_back_to_catalog(self):
        last = rows_of(inline.product_card(1, in_stock=True))[-1]
        assert ru.BTN_BACK in last

    def test_product_card_hides_choose_when_out_of_stock(self):
        texts = [text for row in rows_of(inline.product_card(1, in_stock=False)) for text in row]
        assert ru.BTN_CHOOSE not in texts


class TestProductCallbacks:
    def test_open_and_take_are_different_actions(self):
        opened = inline.ProductCB(product_id=7).action
        taken = inline.ProductCB(product_id=7, action="take").action
        assert opened == "open"
        assert opened != taken

    def test_card_button_carries_positive_id(self):
        """Идентификатор не кодирует действие знаком: -7 и «взять» — разные вещи."""
        markup = inline.product_card(7, in_stock=True)
        payload = markup.inline_keyboard[0][0].callback_data
        assert inline.ProductCB.unpack(payload).product_id == 7
        assert inline.ProductCB.unpack(payload).action == "take"


class TestDeviceCard:
    def _card(self, **overrides):
        payload = {
            "title": "Роутер Pro",
            "mac": "A0:B1:C2:D3:E4:F5",
            "online": True,
            "last_seen": dt.datetime.now(dt.UTC) - dt.timedelta(minutes=4),
            "service_ok": True,
            "uptime_sec": 3 * 86400,
            "clients_wifi": 8,
            "clients_dhcp": 4,
            "cpu_pct": 14,
            "ram_pct": 38,
            "rx_bytes": 137_438_953_472,
            "tx_bytes": 10_737_418_240,
            "subscription_line": "💎 Подписка: Год, до 1 марта 2027.",
        }
        payload.update(overrides)
        return ru.device_card(**payload)

    def test_online_card_shows_real_numbers(self):
        card = self._card()
        assert "На связи" in card
        assert "Устройств в сети: 12" in card
        assert "процессор 14%" in card
        assert "память 38%" in card
        assert "3 дня" in card
        assert "Подписка: Год" in card

    def test_offline_card_explains_what_to_do(self):
        card = self._card(online=False)
        assert "Нет связи" in card
        assert "поддержку" in card
        # Показания устаревшие — выдавать их за текущие нельзя.
        assert "процессор" not in card

    def test_never_seen_device(self):
        card = self._card(online=False, last_seen=None)
        assert "ещё не выходил на связь" in card

    def test_missing_telemetry_is_skipped_not_zeroed(self):
        card = self._card(cpu_pct=None, ram_pct=None, uptime_sec=0, rx_bytes=0, tx_bytes=0)
        assert "Загрузка:" not in card
        assert "Трафик:" not in card
        assert "без перезагрузки" not in card

    def test_service_stopped_is_stated(self):
        assert "не запущен" in self._card(service_ok=False)


class TestTraffic:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, "0 Б"), (2048, "2,0 КБ"), (137_438_953_472, "128 ГБ"), (1_099_511_627_776, "1,0 ТБ")],
    )
    def test_units(self, value, expected):
        assert ru.traffic(value) == expected


class TestTimePhrases:
    def test_fresh_reading(self):
        now = dt.datetime(2026, 8, 4, 12, tzinfo=dt.UTC)
        assert ago_phrase(now - dt.timedelta(seconds=20), now=now) == "только что"

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (dt.timedelta(minutes=1), "1 минуту назад"),
            (dt.timedelta(minutes=3), "3 минуты назад"),
            (dt.timedelta(minutes=40), "40 минут назад"),
            (dt.timedelta(hours=2), "2 часа назад"),
            (dt.timedelta(days=5), "5 дней назад"),
        ],
    )
    def test_plural_forms(self, delta, expected):
        now = dt.datetime(2026, 8, 4, 12, tzinfo=dt.UTC)
        assert ago_phrase(now - delta, now=now) == expected

    def test_old_reading_falls_back_to_date(self):
        now = dt.datetime(2026, 8, 4, 12, tzinfo=dt.UTC)
        assert "2026" in ago_phrase(now - dt.timedelta(days=200), now=now)

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "—"), (90, "1 минута"), (7200, "2 часа"), (86400, "1 день"), (5 * 86400, "5 дней")],
    )
    def test_uptime(self, seconds, expected):
        assert uptime_phrase(seconds) == expected
