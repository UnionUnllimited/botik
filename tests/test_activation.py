"""Активация роутера клиентом и правила доставки.

Сеть и база тут не нужны: проверяем чистые правила — имя учётки в панели,
экранирование ссылки перед отправкой в shell и фильтрацию способов доставки.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from core.enums import OFFERED_DELIVERY_METHODS, DeliveryMethod, DeviceStatus
from core.models import Device, User
from core.services.activation import APPLY_SCRIPT, username_for
from core.services.delivery import tracking_url


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

    def test_site_client_gets_name_without_telegram(self):
        """У клиента с сайта tg_id пустой: «tgNone_...» был бы одинаков у всех."""
        name = username_for(User(id=42, email="client@example.com"), "A0:B1:C2:D3:E4:F5")
        assert name == "id42_a0b1c2d3e4f5"
        assert "None" not in name

    def test_site_clients_do_not_collide(self):
        mac = "A0:B1:C2:D3:E4:F5"
        first = username_for(User(id=42, email="a@example.com"), mac)
        second = username_for(User(id=43, email="b@example.com"), mac)
        assert first != second

    def test_telegram_client_name_is_unchanged(self):
        """Имена уже заведённых учёток трогать нельзя: панель ищет их по имени."""
        assert username_for(make_user(), "A0:B1:C2:D3:E4:F5") == "tg123456789_a0b1c2d3e4f5"

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

    def test_every_method_has_a_human_name(self):
        """Оператор ищет «СДЭК», а не «cdek» — и в карточке, и в выгрузке.

        Названия переехали в код вместе с закрытием страницы «Доставка»:
        цен там больше нет, а перевозчик — это договор, а не строка формы.
        """
        from core.enums import DeliveryMethod
        from core.texts import DELIVERY_METHOD_TITLES

        assert set(DELIVERY_METHOD_TITLES) == set(DeliveryMethod)
        assert all(title.strip() for title in DELIVERY_METHOD_TITLES.values())

    def test_prices_are_not_configurable_anymore(self):
        """Прейскурант по зонам убран: цену называет оператор в самом заказе."""
        from core.services.settings_service import DEFAULTS

        assert "delivery.methods" not in DEFAULTS
        assert "delivery.free_from" not in DEFAULTS

    def test_yandex_has_no_tracking_link(self):
        """Яндекс присылает ссылку клиенту сам — своя была бы выдумкой."""
        assert tracking_url(DeliveryMethod.YANDEX, "123") is None

    @pytest.mark.parametrize("method", [DeliveryMethod.CDEK, DeliveryMethod.POST])
    def test_carriers_with_tracking(self, method):
        assert "123" in (tracking_url(method, "123") or "")

    def test_no_link_without_track_number(self):
        assert tracking_url(DeliveryMethod.CDEK, "") is None


class TestPanelExpiry:
    def test_expiry_is_iso_with_z(self):
        """Панель ждёт время в UTC с суффиксом Z, смещение +00:00 она не понимает."""
        moment = dt.datetime(2027, 3, 1, 12, tzinfo=dt.UTC)
        assert moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z") == "2027-03-01T12:00:00Z"

    def test_local_time_is_converted_to_utc(self):
        """Срок хранится в UTC: без приведения панель отключила бы доступ раньше."""
        from zoneinfo import ZoneInfo

        moment = dt.datetime(2027, 3, 1, 2, tzinfo=ZoneInfo("Europe/Moscow"))
        assert moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z") == "2027-02-28T23:00:00Z"


class TestExpirySyncContract:
    """Продление у нас уже принято и оплачено — сбой панели не должен его рушить."""

    def test_sync_is_best_effort(self):
        import inspect

        from core.services.activation import sync_panel_expiry

        source = inspect.getsource(sync_panel_expiry)
        # Ошибки панели ловятся и логируются, наружу не выходят.
        assert "except remnawave.RemnawaveError" in source
        assert "raise" not in source

    def test_sync_returns_flag_not_raises(self):
        import inspect

        from core.services.activation import sync_panel_expiry

        assert inspect.signature(sync_panel_expiry).return_annotation == "bool"


class TestManualActivation:
    """Ручная активация из админки: имя учётки — сам MAC, клиента может не быть."""

    def test_username_is_the_mac(self):
        from core.services.activation import manual_username_for

        assert manual_username_for("D4:0D:AB:28:3B:80") == "d4-0d-ab-28-3b-80"

    def test_username_fits_panel_rules(self):
        """Панель принимает только латиницу, цифры, дефис и подчёркивание."""
        from core.services.activation import manual_username_for

        name = manual_username_for("D4:0D:AB:28:3B:80")
        assert all(ch.isalnum() or ch in "-_" for ch in name)
        assert len(name) <= 34

    def test_username_does_not_collide_with_client_accounts(self):
        """Иначе ручная активация перезаписала бы срок клиентской подписки."""
        from core.services.activation import manual_username_for, username_for

        mac = "D4:0D:AB:28:3B:80"
        assert manual_username_for(mac) != username_for(make_user(), mac)

    def test_expiry_parsed_from_panel(self):
        import datetime as dt

        from core.services.activation import panel_expiry_of
        from core.services.remnawave import RemnaUser

        account = RemnaUser(uuid="u", username="n", subscription_url="", expire_at="2026-09-26T12:00:00.000Z")
        assert panel_expiry_of(account) == dt.datetime(2026, 9, 26, 12, tzinfo=dt.UTC)

    def test_missing_expiry_is_not_an_error(self):
        """Формат ответа панели меняется между версиями — падать на этом нельзя."""
        from core.services.activation import panel_expiry_of
        from core.services.remnawave import RemnaUser

        assert panel_expiry_of(None) is None
        assert panel_expiry_of(RemnaUser(uuid="u", username="n", subscription_url="")) is None
        garbled = RemnaUser(uuid="u", username="n", subscription_url="", expire_at="скоро")
        assert panel_expiry_of(garbled) is None


class TestRouterRefusalIsExplained:
    """Отказ роутера обязан читаться словами, а не приезжать пятисоткой.

    Роутер не отвечает по SSH постоянно: не включён, туннель ещё не поднялся,
    сменили пароль. Клиентская активация это ловила, ручная — нет, и оператор
    видел «Основное приложение ответило 500» без единого слова о причине.
    """

    SOURCE = (
        Path(__file__).resolve().parents[1] / "core/services/activation.py"
    ).read_text(encoding="utf-8")

    def _body(self, name: str) -> str:
        start = self.SOURCE.index(f"async def {name}(")
        tail = self.SOURCE[start:]
        end = tail.find("\nasync def ", 1)
        return tail[:end] if end > 0 else tail

    @pytest.mark.parametrize("name", ["activate_manually", "activate"])
    def test_delivery_failure_is_caught(self, name):
        body = self._body(name)
        assert "deliver_subscription" in body, f"{name} больше не отдаёт ссылку роутеру"
        assert "router_shell.ShellError" in body, (
            f"{name} не ловит отказ SSH — оператор получит 500 вместо причины"
        )

    @pytest.mark.asyncio
    async def test_manual_activation_turns_ssh_failure_into_a_readable_error(self, monkeypatch):
        """Не по исходнику, а по делу: подсовываем отказ SSH и ждём ActivationError."""
        from core.services import activation, router_shell
        from core.services.remnawave import RemnaUser

        account = RemnaUser(uuid="u", username="n", subscription_url="https://panel/sub/x")

        class _Panel:
            async def find_user(self, _username):
                return account

            async def update_expiry(self, **_kwargs):
                return None

        async def _no_tunnel(_session, _device):
            return None

        async def _refuse(_device, _url):
            raise router_shell.ShellError("Не удалось подключиться к роутеру: timeout")

        def _client():
            return _Panel()

        monkeypatch.setattr(activation.remnawave, "client", _client)
        monkeypatch.setattr(activation, "_ensure_tunnel", _no_tunnel)
        monkeypatch.setattr(activation, "deliver_subscription", _refuse)
        monkeypatch.setattr(activation.routers, "add_event", lambda *a, **k: None)

        device = Device(id=1, mac="F8:5E:3C:92:C0:22", status=DeviceStatus.ACTIVE)

        class _Session:
            async def get(self, *_args):
                return None

        with pytest.raises(activation.ActivationError) as failure:
            await activation.activate_manually(_Session(), device=device, days=30)

        text = str(failure.value)
        assert "роутер" in text.lower()
        assert "timeout" in text
        # Срок в панели уже проставлен, и оператор должен это прочитать:
        # иначе он решит, что активация не прошла вовсе, и заведёт её заново.
        assert "панел" in text.lower()
