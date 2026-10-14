"""«Кто в сети»: аренды, соседи и Wi-Fi одним списком.

Главное, что стережётся: провайдер за внешним портом не попадает в список
домашних устройств, а «сейчас в сети» — это ответивший только что сосед
или подключённый по Wi-Fi, а не живая аренда, которой сутки.
"""

from __future__ import annotations

from core.services import home_network

PAYLOAD = """\
1760000000 aa:bb:cc:00:00:01 192.168.14.23 iPhone-Anna 01:aa:bb:cc:00:00:01
1760000000 aa:bb:cc:00:00:02 192.168.14.40 DESKTOP-K2 *
1760000000 aa:bb:cc:00:00:03 192.168.14.9 * *
1760000000 aa:bb:cc:00:00:04 192.168.14.77 Bravia-TV *
#--
192.168.14.23 dev br-lan lladdr aa:bb:cc:00:00:01 REACHABLE
192.168.14.40 dev br-lan lladdr aa:bb:cc:00:00:02 DELAY
192.168.14.9 dev br-lan lladdr aa:bb:cc:00:00:03 STALE
192.168.14.55 dev br-lan lladdr aa:bb:cc:00:00:05 REACHABLE
10.0.0.1 dev eth0.2 lladdr de:ad:be:ef:00:01 REACHABLE
192.168.14.60 dev br-lan  FAILED
#--
Station aa:bb:cc:00:00:01 (on phy0-ap0)
AA:BB:CC:00:00:04  -61 dBm / -95 dBm (SNR 34)  120 ms ago
"""


def _by_mac():
    return {item.mac: item for item in home_network.parse(PAYLOAD)}


def test_provider_gateway_is_not_a_home_device():
    assert "DE:AD:BE:EF:00:01" not in _by_mac()


def test_names_and_kinds():
    found = _by_mac()
    assert found["AA:BB:CC:00:00:01"].name == "iPhone-Anna"
    assert found["AA:BB:CC:00:00:01"].kind == "phone"
    assert found["AA:BB:CC:00:00:02"].kind == "computer"
    assert found["AA:BB:CC:00:00:04"].kind == "tv"
    # «*» в аренде — устройство не назвалось; это пустое имя, а не звёздочка.
    assert found["AA:BB:CC:00:00:03"].name == ""


def test_active_means_answered_just_now_or_on_wifi():
    found = _by_mac()
    assert found["AA:BB:CC:00:00:01"].active is True
    assert found["AA:BB:CC:00:00:02"].active is True
    assert found["AA:BB:CC:00:00:03"].active is False  # STALE — ушёл
    assert found["AA:BB:CC:00:00:04"].active is True  # нет в соседях, но на Wi-Fi


def test_wifi_or_cable():
    found = _by_mac()
    assert found["AA:BB:CC:00:00:01"].wifi is True
    assert found["AA:BB:CC:00:00:02"].wifi is False


def test_without_wifi_data_we_do_not_guess():
    payload = PAYLOAD.rsplit("#--", 1)[0] + "#--\n"
    assert {item.wifi for item in home_network.parse(payload)} == {None}


def test_device_without_lease_still_counts():
    """Статический адрес: аренды нет, а устройство дома и отвечает."""
    found = _by_mac()
    assert found["AA:BB:CC:00:00:05"].ip == "192.168.14.55"
    assert found["AA:BB:CC:00:00:05"].active is True


def test_online_first_then_by_address():
    order = [item.ip for item in home_network.parse(PAYLOAD)]
    assert order == ["192.168.14.23", "192.168.14.40", "192.168.14.55", "192.168.14.77", "192.168.14.9"]


def test_empty_router_answer():
    assert home_network.parse("") == []


async def test_command_takes_nothing_from_the_client(monkeypatch):
    """Команда — постоянная строка: от клиента в неё не попадает ничего."""
    from core.services import router_shell

    seen = []

    async def _run(device, command, *, stdin=None, **_kwargs):
        seen.append((command, stdin))
        return router_shell.CommandResult(command, 0, PAYLOAD, "")

    monkeypatch.setattr(home_network.router_shell, "run", _run)
    found = await home_network.read(object())
    assert seen == [(home_network.COMMAND, None)]
    assert len(found) == 5
