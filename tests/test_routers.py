"""Разбор данных роутеров и имён туннелей frp."""

from __future__ import annotations

from typing import ClassVar

import pytest

from core.services.frp import mac_from_proxy_name, proxy_kind, proxy_names_for
from core.services.routers import parse_stats


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
        legacy["vpn_active"] = True
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
