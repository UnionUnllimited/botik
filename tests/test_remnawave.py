"""Клиент Remnawave: разбор ответов панели и правила импорта узлов.

Панель развивается быстро и меняет форму ответа между версиями, поэтому
проверяем именно терпимость разбора: конверт, регистр имён полей, мусор
вместо списка. Сеть здесь не нужна — тестируем чистые функции.
"""

from __future__ import annotations

import pytest

from api.admin.routes.remnawave import host_uuid_of, protocol_for, remarks_for
from core.enums import NodeProtocol
from core.models import Node
from core.services.remnawave import (
    PanelStatus,
    RemnaHost,
    RemnaNode,
    as_list,
    unwrap,
)


class TestUnwrap:
    def test_response_envelope(self):
        assert unwrap({"response": {"total": 3}}) == {"total": 3}

    def test_nested_envelopes(self):
        assert unwrap({"response": {"data": [1, 2]}}) == [1, 2]

    def test_plain_payload_untouched(self):
        assert unwrap([{"uuid": "a"}]) == [{"uuid": "a"}]

    def test_does_not_loop_forever(self):
        """Самоссылающийся конверт не должен вешать процесс."""
        payload: dict = {}
        payload["response"] = payload
        assert unwrap(payload) is payload


class TestAsList:
    @pytest.mark.parametrize(
        "payload",
        [
            [{"uuid": "a"}],
            {"response": [{"uuid": "a"}]},
            {"response": {"nodes": [{"uuid": "a"}]}},
            {"data": {"items": [{"uuid": "a"}]}},
        ],
    )
    def test_finds_list_in_any_shape(self, payload):
        assert as_list(payload) == [{"uuid": "a"}]

    @pytest.mark.parametrize("payload", [None, 42, "строка", {}, {"response": {"total": 0}}])
    def test_returns_empty_on_garbage(self, payload):
        assert as_list(payload) == []

    def test_skips_non_dict_items(self):
        assert as_list([{"uuid": "a"}, "мусор", None]) == [{"uuid": "a"}]


class TestNodeParsing:
    def test_camel_case_fields(self):
        node = RemnaNode.parse(
            {
                "uuid": "n1",
                "name": "Amsterdam",
                "address": "45.10.0.1",
                "port": 443,
                "countryCode": "nl",
                "isNodeOnline": True,
                "usersOnline": 17,
                "trafficUsedBytes": 1024,
                "xrayVersion": "25.3.6",
            }
        )
        assert node.country_code == "NL"
        assert node.is_online is True
        assert node.users_online == 17
        assert node.state_label == "на связи"
        assert node.tone == "ok"

    def test_snake_case_fields_also_work(self):
        node = RemnaNode.parse({"uuid": "n1", "name": "x", "address": "y", "is_node_online": True})
        assert node.is_online is True

    def test_disabled_wins_over_online(self):
        node = RemnaNode.parse({"uuid": "n1", "name": "x", "address": "y", "isDisabled": True})
        assert node.tone == "muted"
        assert node.state_label == "выключен"

    def test_offline_node(self):
        node = RemnaNode.parse({"uuid": "n1", "name": "x", "address": "y"})
        assert node.tone == "bad"
        assert node.state_label == "недоступен"

    def test_missing_fields_do_not_crash(self):
        node = RemnaNode.parse({})
        assert node.name == "без имени"
        assert node.port == 0

    def test_string_booleans(self):
        """Часть версий панели отдаёт флаги строками."""
        node = RemnaNode.parse({"uuid": "n", "name": "x", "address": "y", "isNodeOnline": "true"})
        assert node.is_online is True


class TestHostParsing:
    def test_connection_config_keeps_protocol_params(self):
        host = RemnaHost.parse(
            {
                "uuid": "h1",
                "remark": "Netherlands",
                "address": "nl.example.net",
                "port": 443,
                "sni": "example.com",
                "publicKey": "abc",
                "isDisabled": False,
            }
        )
        config = host.connection_config
        assert config["sni"] == "example.com"
        assert config["publicKey"] == "abc"
        # Служебные поля панели в параметры подключения не попадают.
        assert "uuid" not in config
        assert "address" not in config
        assert "remark" not in config

    def test_none_values_are_dropped(self):
        host = RemnaHost.parse({"uuid": "h", "remark": "r", "address": "a", "alpn": None})
        assert "alpn" not in host.connection_config


class TestImportRules:
    def test_remarks_get_required_prefix(self):
        assert remarks_for("Netherlands").startswith("Router_")

    def test_existing_prefix_is_not_doubled(self):
        assert remarks_for("Router_NL") == "Router_NL"

    def test_whitespace_is_collapsed(self):
        assert remarks_for("  Netherlands   Reality ") == "Router_Netherlands Reality"

    def test_empty_remark_still_valid(self):
        assert remarks_for("") == "Router_node"

    def test_long_remark_is_trimmed_to_column(self):
        """`Node.remarks` — String(120), длиннее в базу не влезет."""
        assert len(remarks_for("я" * 200)) == 120

    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            ({"publicKey": "x", "shortId": "y"}, NodeProtocol.VLESS_REALITY),
            ({"security": "reality"}, NodeProtocol.VLESS_REALITY),
            ({"path": "/ws", "sni": "example.com"}, NodeProtocol.VLESS_WS_TLS),
            ({}, NodeProtocol.VLESS_WS_TLS),
        ],
    )
    def test_protocol_is_guessed_from_params(self, config, expected):
        assert protocol_for(config) is expected

    def test_host_uuid_read_from_node_config(self):
        node = Node(remarks="Router_NL", host="h", port=443, config={"remnawave": {"host_uuid": "h1"}})
        assert host_uuid_of(node) == "h1"

    @pytest.mark.parametrize("config", [{}, {"remnawave": "не объект"}, {"sni": "x"}, None])
    def test_own_node_has_no_host_uuid(self, config):
        node = Node(remarks="Router_own", host="h", port=443, config=config)
        assert host_uuid_of(node) == ""


class TestPanelStatus:
    def test_not_configured_is_neutral_not_broken(self):
        """Ненастроенная интеграция — не авария: нельзя пугать оператора красным."""
        status = PanelStatus(configured=False)
        assert status.tone == "muted"
        assert status.label == "не настроена"

    def test_configured_but_unreachable_is_an_error(self):
        status = PanelStatus(configured=True, ok=False, error="таймаут")
        assert status.tone == "bad"
        assert status.label == "нет связи"

    def test_connected(self):
        status = PanelStatus(configured=True, ok=True)
        assert status.tone == "ok"
        assert status.label == "связь есть"


class TestSettings:
    def test_missing_keys_are_named_precisely(self):
        from core.config import RemnawaveSettings

        config = RemnawaveSettings(enabled=False, base_url="", token="")
        assert config.missing_keys == ["REMNAWAVE_ENABLED=true", "REMNAWAVE_BASE_URL", "REMNAWAVE_TOKEN"]
        assert config.is_configured is False

    def test_fully_configured(self):
        from core.config import RemnawaveSettings

        config = RemnawaveSettings(enabled=True, base_url="http://panel:3000", token="secret")
        assert config.missing_keys == []
        assert config.is_configured is True
