"""Пароль Wi-Fi из бота и приложения.

Стерегутся три вещи. Разбор `uci` — что клиенту показываются именно его
сети, а выключенные и клиентские режимы не попадают. Правило пароля —
одно на бот и приложение. И главное: пароль клиента не попадает в командную
строку роутера — ни как текст для оболочки, ни в журнал команд.
"""

from __future__ import annotations

import pytest

from core.services import router_shell, router_wifi

UCI = """\
wireless.radio0=wifi-device
wireless.radio0.type='mac80211'
wireless.radio0.band='2g'
wireless.radio0.channel='1'
wireless.radio1=wifi-device
wireless.radio1.type='mac80211'
wireless.radio1.band='5g'
wireless.radio1.channel='36'
wireless.radio2=wifi-device
wireless.radio2.hwmode='11a'
wireless.radio2.disabled='1'
wireless.default_radio0=wifi-iface
wireless.default_radio0.device='radio0'
wireless.default_radio0.mode='ap'
wireless.default_radio0.ssid='Titan'
wireless.default_radio0.encryption='psk2'
wireless.default_radio0.key='it'\\''s-secret'
wireless.default_radio1=wifi-iface
wireless.default_radio1.device='radio1'
wireless.default_radio1.mode='ap'
wireless.default_radio1.ssid='Titan'
wireless.default_radio1.encryption='sae-mixed'
wireless.default_radio1.key='it'\\''s-secret'
wireless.cfg053579=wifi-iface
wireless.cfg053579.device='radio0'
wireless.cfg053579.ssid='Titan Guest'
wireless.cfg053579.encryption='none'
wireless.wwan=wifi-iface
wireless.wwan.device='radio0'
wireless.wwan.mode='sta'
wireless.wwan.ssid='Neighbour'
wireless.wwan.key='nope'
wireless.off=wifi-iface
wireless.off.device='radio1'
wireless.off.ssid='Old'
wireless.off.disabled='1'
wireless.dead=wifi-iface
wireless.dead.device='radio2'
wireless.dead.ssid='On a dead radio'
"""


class TestParse:
    def test_only_access_points_on_live_radios(self):
        networks = router_wifi.parse(UCI)
        assert [network.id for network in networks] == ["default_radio0", "default_radio1", "cfg053579"]

    def test_bands_and_passwords(self):
        first, second, guest = router_wifi.parse(UCI)
        assert (first.ssid, first.band, first.password, first.open) == (
            "Titan",
            "2,4 ГГц",
            "it's-secret",
            False,
        )
        assert second.band == "5 ГГц"
        # Открытая гостевая сеть — без пароля, и это не «пароль пустой».
        assert guest.open is True and guest.password == ""

    def test_old_firmware_band_from_hwmode_and_channel(self):
        payload = (
            "wireless.r=wifi-device\nwireless.r.hwmode='11a'\n"
            "wireless.n=wifi-iface\nwireless.n.device='r'\nwireless.n.ssid='A'\nwireless.n.key='12345678x'\n"
            "wireless.n.encryption='psk2'\n"
            "wireless.s=wifi-device\nwireless.s.channel='6'\n"
            "wireless.m=wifi-iface\nwireless.m.device='s'\nwireless.m.ssid='B'\nwireless.m.encryption='psk'\n"
        )
        bands = {network.ssid: network.band for network in router_wifi.parse(payload)}
        assert bands == {"A": "5 ГГц", "B": "2,4 ГГц"}

    def test_garbage_is_ignored(self):
        assert router_wifi.parse("uci: Entry not found\n\n") == []


class TestPasswordRule:
    @pytest.mark.parametrize("value", ["Dacha-2024!", "kV9#mQ2z", "a" * 30 + "B1"])
    def test_good(self, value):
        assert router_wifi.check_password(value) == ""

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "short1",
            "x" * 64,
            "пароль-кириллицей",
            "with space1",
            "quote'inside1",
            'double"quote1',
            "back\\slash12",
            # `$` в шаблоне пропускал перевод строки в конце: восемь «символов»
            # на проверке, семь на роутере — и сеть не поднимается.
            "1234567\n",
            "Dacha-2024\n",
        ],
    )
    def test_refused(self, value):
        assert router_wifi.check_password(value) != ""

    def test_shell_looking_text_is_just_a_password(self):
        """`$(reboot)` проходит как пароль: опасным он был бы только в командной
        строке, а туда пароль не попадает вовсе (см. ниже)."""
        assert router_wifi.check_password("$(reboot)x") == ""

    @pytest.mark.parametrize("value", ["12345678", "password", "qwerty123", "aaaaaaaaaa", "abababab"])
    def test_too_simple(self, value):
        assert "простой" in router_wifi.check_password(value)


def test_password_is_not_in_repr():
    """Случайный вывод сети в журнал не должен унести пароль."""
    network = router_wifi.Network("a", "Titan", "5 ГГц", "Dacha-2024!", False)
    assert "Dacha" not in repr(network)


class _Shell:
    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    async def run(self, device, command, *, stdin=None, **_kwargs):
        self.calls.append((command, stdin))
        if command == router_wifi.READ_COMMAND:
            return router_shell.CommandResult(command, 0, UCI, "")
        return router_shell.CommandResult(command, 0, "ok\n", "")


class _Device:
    id = 7
    mac = "AA:BB:CC:DD:EE:01"


@pytest.mark.asyncio
async def test_password_goes_to_stdin_not_to_the_command(monkeypatch):
    shell = _Shell()
    monkeypatch.setattr(router_wifi.router_shell, "run", shell.run)
    secret = "$(reboot);Tv-9x"

    networks = await router_wifi.change_password(_Device(), secret)

    command, stdin = shell.calls[-1]
    assert secret not in command
    assert stdin == secret + "\n"
    assert command.startswith("read -r k;")
    # Только закрытые сети, по настоящим именам разделов; гостевая открытая
    # остаётся открытой — закрывать её молча нельзя.
    assert "default_radio0 default_radio1;" in command
    assert "cfg053579" not in command
    assert [n.password for n in networks] == [secret, secret, ""]


@pytest.mark.asyncio
async def test_bad_password_never_reaches_the_router(monkeypatch):
    shell = _Shell()
    monkeypatch.setattr(router_wifi.router_shell, "run", shell.run)
    with pytest.raises(router_wifi.WifiError):
        await router_wifi.change_password(_Device(), "short")
    assert shell.calls == []


@pytest.mark.asyncio
async def test_router_that_did_not_save_is_an_error(monkeypatch):
    async def _run(device, command, *, stdin=None, **_kwargs):
        if command == router_wifi.READ_COMMAND:
            return router_shell.CommandResult(command, 0, UCI, "")
        return router_shell.CommandResult(command, 1, "", "uci: I/O error")

    monkeypatch.setattr(router_wifi.router_shell, "run", _run)
    with pytest.raises(router_wifi.WifiError):
        await router_wifi.change_password(_Device(), "Dacha-2024!")


def test_section_names_are_checked_before_the_command():
    """Имя раздела приходит с роутера и уходит в командную строку."""
    assert router_wifi.parse("wireless.bad;reboot=wifi-iface\nwireless.bad;reboot.ssid='x'\n") == []
