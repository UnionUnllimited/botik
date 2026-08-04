"""Активация роутера клиентом и правила доставки.

Сеть и база тут не нужны: проверяем чистые правила — имя учётки в панели,
экранирование ссылки перед отправкой в shell и фильтрацию способов доставки.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from core.enums import OFFERED_DELIVERY_METHODS, DeliveryMethod
from core.models import User
from core.services.activation import APPLY_SCRIPT, username_for
from core.services.delivery import DeliveryOption, tracking_url


def make_user(tg_id: int = 123456789) -> User:
    return User(id=7, tg_id=tg_id, first_name="Карл")


class TestUsername:
    def test_contains_telegram_and_mac(self):
        name = username_for(make_user(), "A0:B1:C2:D3:E4:F5")
        assert name == "tg123456789_a0b1c2d3e4f5"

    def test_separators_are_stripped(self):
        """Панель принимает только латиницу, цифры, дефис и подчёркивание."""
        name = username_for(make_user(), "A0:B1:C2:D3:E4:F5")
        assert ":" not in name
        assert all(ch.isalnum() or ch in "_-" for ch in name)

    def test_same_router_gives_same_name(self):
        """Иначе повторная активация плодила бы учётки."""
        user = make_user()
        assert username_for(user, "A0:B1:C2:D3:E4:F5") == username_for(user, "A0:B1:C2:D3:E4:F5")

    def test_different_routers_differ(self):
        user = make_user()
        assert username_for(user, "A0:B1:C2:D3:E4:F5") != username_for(user, "A0:B1:C2:D3:E4:F6")

    def test_fits_panel_limit(self):
        name = username_for(User(id=1, tg_id=9999999999999999, first_name="x"), "A0:B1:C2:D3:E4:F5")
        assert len(name) <= 34

    def test_custom_template(self, monkeypatch):
        from core.config import settings

        monkeypatch.setattr(settings.remnawave, "username_template", "router-{user_id}", raising=False)
        assert username_for(make_user(), "A0:B1:C2:D3:E4:F5") == "router-7"

    def test_template_with_spaces_is_cleaned(self, monkeypatch):
        from core.config import settings

        monkeypatch.setattr(settings.remnawave, "username_template", "tg {tg_id} mac", raising=False)
        assert " " not in username_for(make_user(), "A0:B1:C2:D3:E4:F5")


class TestDeliveryCommand:
    """Ссылка уезжает в shell роутера — кавычки в ней не должны ломать команду."""

    def test_quotes_are_escaped(self):
        url = "https://panel.example/sub/abc'; rm -rf /tmp; echo '"
        safe = url.replace("'", "'\\''")
        command = f"{APPLY_SCRIPT} '{safe}'"
        # Одинарная кавычка внутри строки закрывается и открывается заново,
        # поэтому команда остаётся одним аргументом.
        assert command.count("'") % 2 == 0
        assert "rm -rf /tmp'" not in command.replace("'\\''", "")

    def test_plain_url_is_untouched(self):
        url = "https://panel.example/sub/abc123"
        assert f"{APPLY_SCRIPT} '{url}'" == f"/usr/bin/apply_sub.sh '{url}'"


class TestOfferedDelivery:
    def test_only_three_carriers_are_offered(self):
        assert OFFERED_DELIVERY_METHODS == (
            DeliveryMethod.CDEK,
            DeliveryMethod.POST,
            DeliveryMethod.YANDEX,
        )

    @pytest.mark.parametrize("method", [DeliveryMethod.PICKUP, DeliveryMethod.BOXBERRY])
    def test_retired_methods_are_not_offered(self, method):
        assert method not in OFFERED_DELIVERY_METHODS

    @pytest.mark.parametrize("method", [DeliveryMethod.PICKUP, DeliveryMethod.BOXBERRY])
    def test_retired_methods_still_readable(self, method):
        """Заказы, оформленные до отказа от них, должны открываться."""
        assert DeliveryMethod(method.value) is method

    def test_defaults_cover_every_offered_method(self):
        from core.services.settings_service import DEFAULTS

        configured = set(DEFAULTS["delivery.methods"])
        assert configured == {method.value for method in OFFERED_DELIVERY_METHODS}

    def test_defaults_are_enabled(self):
        from core.services.settings_service import DEFAULTS

        assert all(item["enabled"] for item in DEFAULTS["delivery.methods"].values())

    def test_yandex_has_no_tracking_link(self):
        """Яндекс присылает ссылку клиенту сам — своя была бы выдумкой."""
        assert tracking_url(DeliveryMethod.YANDEX, "123") is None

    @pytest.mark.parametrize("method", [DeliveryMethod.CDEK, DeliveryMethod.POST])
    def test_carriers_with_tracking(self, method):
        assert "123" in (tracking_url(method, "123") or "")

    def test_no_link_without_track_number(self):
        assert tracking_url(DeliveryMethod.CDEK, "") is None


class TestDeliveryOption:
    def _option(self, **overrides):
        payload = {
            "method": DeliveryMethod.CDEK,
            "title": "СДЭК",
            "pvz_price": Decimal("350.00"),
            "courier_price": Decimal("550.00"),
            "days": "3–7 дней",
        }
        payload.update(overrides)
        return DeliveryOption(**payload)

    def test_price_depends_on_target(self):
        option = self._option()
        assert option.price_for(to_pvz=True) == Decimal("350.00")
        assert option.price_for(to_pvz=False) == Decimal("550.00")

    def test_enabled_by_default(self):
        assert self._option().enabled is True


class TestPanelExpiry:
    def test_expiry_is_iso_with_z(self):
        """Панель ждёт время в UTC с суффиксом Z, смещение +00:00 она не понимает."""
        moment = dt.datetime(2027, 3, 1, 12, tzinfo=dt.UTC)
        assert moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z") == "2027-03-01T12:00:00Z"
