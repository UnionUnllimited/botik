"""Разбор данных роутеров и имён туннелей frp."""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import SecretStr

from core.models import Device
from core.services.frp import mac_from_proxy_name, proxy_kind, proxy_names_for
from core.services.routers import LEGACY_ACTIVE_FIELD, apply_stats, parse_stats


class TestProxyNames:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("lucia0b1c2d3e4f5", "A0:B1:C2:D3:E4:F5"),
            ("LUCIA0B1C2D3E4F5", "A0:B1:C2:D3:E4:F5"),
            ("ssha0b1c2d3e4f5", "A0:B1:C2:D3:E4:F5"),
        ],
    )
    def test_mac_extracted_from_proxy(self, name, expected):
        assert mac_from_proxy_name(name) == expected

    @pytest.mark.parametrize("name", ["", "web-server", "luci123", "sshzzzzzzzzzzzz"])
    def test_foreign_names_ignored(self, name):
        """На frps живут и чужие туннели — их нельзя принять за роутер."""
        assert mac_from_proxy_name(name) == ""

    def test_kind_detection(self):
        assert proxy_kind("lucia0b1c2d3e4f5") == "luci"
        assert proxy_kind("ssha0b1c2d3e4f5") == "ssh"
        assert proxy_kind("something") == "unknown"

    def test_names_are_built_back(self):
        luci, ssh = proxy_names_for("A0:B1:C2:D3:E4:F5")
        assert luci == "lucia0b1c2d3e4f5"
        assert ssh == "ssha0b1c2d3e4f5"
        assert mac_from_proxy_name(luci) == "A0:B1:C2:D3:E4:F5"


class TestParseStats:
    payload: ClassVar[dict] = {
        "mac": "a0:b1:c2:d3:e4:f5",
        "board": "zbt-z8103ax",
        "fw": "23.05.3",
        "uptime_sec": 86400,
        "load": 0.42,
        "cpu_pct": 17,
        "ram": {"total_kb": 512000, "used_kb": 256000, "pct": 50},
        "temp_c": 46.5,
        "network": {"wan_ip": "10.20.30.40", "rx_bytes": 1024, "tx_bytes": 2048},
        "clients": {"wifi": 4, "dhcp": 7},
        "service_active": True,
        "frpc_running": True,
    }

    def test_all_fields_parsed(self):
        stats = parse_stats(self.payload)
        assert stats.board == "zbt-z8103ax"
        assert stats.fw_version == "23.05.3"
        assert stats.cpu_pct == 17
        assert stats.ram_pct == 50
        assert stats.temp_c == 46.5
        assert stats.clients_wifi == 4
        assert stats.clients_dhcp == 7
        assert stats.wan_ip == "10.20.30.40"
        assert stats.service_active is True
        assert stats.tunnel_running is True

    def test_legacy_field_name_supported(self):
        """Старые прошивки называют флаг сервиса иначе — данные не теряем."""
        legacy = dict(self.payload)
        del legacy["service_active"]
        legacy[LEGACY_ACTIVE_FIELD] = True
        assert parse_stats(legacy).service_active is True

    def test_empty_payload_does_not_crash(self):
        stats = parse_stats({})
        assert stats.cpu_pct is None
        assert stats.clients_wifi == 0
        assert stats.service_active is False

    def test_garbage_values_are_ignored(self):
        stats = parse_stats({"cpu_pct": "нет данных", "ram": {"pct": None}, "temp_c": "null", "load": "n/a"})
        assert stats.cpu_pct == 0
        assert stats.ram_pct is None
        assert stats.temp_c is None
        assert stats.load_avg is None

    def test_string_numbers_are_accepted(self):
        stats = parse_stats({"uptime_sec": "3600", "network": {"rx_bytes": "512"}})
        assert stats.uptime_sec == 3600
        assert stats.rx_bytes == 512


class TestSshPassword:
    """Прошивка назначает пароль root по MAC — админка должна считать так же."""

    def test_matches_firmware_algorithm(self):
        from core.services.router_shell import derive_password

        # printf "%stests" d40dab034bce | sha256sum | cut -c1-16
        assert derive_password("D4:0D:AB:03:4B:CE", "tests") == "7600a1bd651d639c"

    @pytest.mark.parametrize(
        "mac", ["D4:0D:AB:03:4B:CE", "d4:0d:ab:03:4b:ce", "d4-0d-ab-03-4b-ce", "d40dab034bce"]
    )
    def test_mac_format_does_not_matter(self, mac):
        from core.services.router_shell import derive_password

        assert derive_password(mac, "tests") == "7600a1bd651d639c"

    def test_different_routers_get_different_passwords(self):
        from core.services.router_shell import derive_password

        first = derive_password("D4:0D:AB:03:4B:CE", "tests")
        second = derive_password("D4:0D:AB:28:32:18", "tests")
        assert first != second
        assert len(first) == len(second) == 16

    def test_salt_changes_result(self):
        from core.services.router_shell import derive_password

        assert derive_password("D4:0D:AB:03:4B:CE", "other") != "7600a1bd651d639c"


class TestStockRouterStillOpens:
    """Парк смешанный: часть роутеров ещё со стоковым паролем, часть уже
    считает его из MAC.

    Раньше выбирался ровно один пароль: заданная соль отменяла статический,
    и стоковый роутер не открывался вовсе — хотя пароль от него известен
    и лежит в настройках.
    """

    def _device(self):
        from core.models import Device

        return Device(id=7, mac="D4:0D:AB:03:4B:CE", model="")

    def test_salt_no_longer_hides_the_stock_password(self, monkeypatch):
        from core.config import settings
        from core.services import router_shell

        monkeypatch.setattr(settings.frp, "ssh_password_salt", SecretStr("tests"))
        monkeypatch.setattr(settings.frp, "ssh_password", SecretStr("stock-secret"))

        candidates = router_shell.passwords_for(self._device())
        assert candidates == ["7600a1bd651d639c", "stock-secret"]

    def test_stock_password_alone_is_enough(self, monkeypatch):
        from core.config import settings
        from core.services import router_shell

        monkeypatch.setattr(settings.frp, "ssh_password_salt", SecretStr(""))
        monkeypatch.setattr(settings.frp, "ssh_password", SecretStr("stock-secret"))

        assert router_shell.passwords_for(self._device()) == ["stock-secret"]

    def test_no_duplicates(self, monkeypatch):
        """Одинаковый пароль дважды — лишний отказ логина в журнале роутера
        и лишняя секунда ожидания на каждой команде."""
        from core.config import settings
        from core.services import router_shell

        monkeypatch.setattr(settings.frp, "ssh_password_salt", SecretStr("tests"))
        monkeypatch.setattr(settings.frp, "ssh_password", SecretStr("7600a1bd651d639c"))

        assert router_shell.passwords_for(self._device()) == ["7600a1bd651d639c"]

    def test_nothing_configured_is_still_an_error(self, monkeypatch):
        from core.config import settings
        from core.services import router_shell

        monkeypatch.setattr(settings.frp, "ssh_password_salt", SecretStr(""))
        monkeypatch.setattr(settings.frp, "ssh_password", SecretStr(""))

        assert router_shell.passwords_for(self._device()) == []

    def test_operator_is_shown_the_most_precise_one(self, monkeypatch):
        """В карточке показываем один пароль — тот, которым войдём первым."""
        from core.config import settings
        from core.services import router_shell

        monkeypatch.setattr(settings.frp, "ssh_password_salt", SecretStr("tests"))
        monkeypatch.setattr(settings.frp, "ssh_password", SecretStr("stock-secret"))

        assert router_shell.password_for(self._device()) == "7600a1bd651d639c"


class TestModelFromBoard:
    """Модель показывают все экраны, а заполняла её до сих пор пустота.

    Телеметрия приезжает с именем платы и складывалась в `board`, а экраны
    читают `model` — в парке у каждого устройства стояло «—».
    """

    def _stats(self, board: str):
        from core.services.routers import parse_stats

        return parse_stats({"board": board})

    def test_model_filled_from_board(self):
        from core.models import Device
        from core.services.routers import apply_stats

        device = Device(mac="A0:B1:C2:D3:E4:F5", model="")
        apply_stats(device, self._stats("zbt-z8103ax"))
        assert device.model == "zbt-z8103ax"
        assert device.board == "zbt-z8103ax"

    def test_operator_name_is_not_overwritten(self):
        """Оператор мог назвать устройство по-своему — опрос это не затирает."""
        from core.models import Device
        from core.services.routers import apply_stats

        device = Device(mac="A0:B1:C2:D3:E4:F5", model="Роутер Basic (склад)")
        apply_stats(device, self._stats("zbt-z8103ax"))
        assert device.model == "Роутер Basic (склад)"
        assert device.board == "zbt-z8103ax"

    def test_empty_board_changes_nothing(self):
        from core.models import Device
        from core.services.routers import apply_stats

        device = Device(mac="A0:B1:C2:D3:E4:F5", model="")
        apply_stats(device, self._stats(""))
        assert device.model == ""


class TestFirmwareBuild:
    """Номер сборки с роутера: по нему видно, дошло ли обновление.

    `fw_version` («25.12.3») на этот вопрос не отвечает — это версия базы,
    её не с чем сравнивать. Сравнивается целое, то же самое, что `version`
    в манифесте.
    """

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"titan_build": 140}, 140),
            ({"titan_build": "140"}, 140),
            ({"titan_build": "r140"}, 140),
            ({"titan_build": "titan-r140"}, 140),
            # Поле новое: пока прошивка его не шлёт, номер неизвестен.
            ({}, None),
            ({"titan_build": ""}, None),
            ({"titan_build": 0}, None),
            ({"titan_build": "неизвестно"}, None),
            # Ноль — не «сборка 0»: нулём пришлось бы считать все молчащие.
            ({"titan_build": "r0"}, None),
        ],
    )
    def test_build_is_read_from_the_answer(self, payload, expected):
        assert parse_stats(payload).fw_build == expected

    def test_build_lives_apart_from_the_base_version(self):
        stats = parse_stats({"fw": "25.12.3", "titan_build": 140})
        assert (stats.fw_version, stats.fw_build) == ("25.12.3", 140)

    def test_silence_does_not_erase_a_known_build(self):
        """Прошивка без поля не должна стирать номер, который роутер уже называл."""
        device = Device(mac="A0:B1:C2:D3:E4:F5", fw_build=140)
        apply_stats(device, parse_stats({"fw": "25.12.3"}))
        assert device.fw_build == 140

    def test_new_build_replaces_the_old_one(self):
        device = Device(mac="A0:B1:C2:D3:E4:F5", fw_build=140)
        apply_stats(device, parse_stats({"titan_build": 141}))
        assert device.fw_build == 141
