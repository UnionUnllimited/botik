"""Разбор ответа роутера про узлы сервиса доступа.

Проверяется не «json.loads работает», а то, ради чего разбор написан отдельно:
клиент не должен остаться без доступа из-за прошивки, которая ответила иначе,
чем ждал сервер, и не должен уметь протащить в командную строку постороннее.
"""

from __future__ import annotations

import pytest

from core.services import router_nodes


ANSWER = (
    '{"enabled":true,"current":"cfg02","nodes":['
    '{"id":"cfg01","name":"Amsterdam 1"},'
    '{"id":"cfg02","name":"Frankfurt 2"}]}'
)


def test_reads_nodes_and_choice():
    state = router_nodes.parse(ANSWER)

    assert [node.id for node in state.nodes] == ["cfg01", "cfg02"]
    assert [node.name for node in state.nodes] == ["Amsterdam 1", "Frankfurt 2"]
    assert state.current == "cfg02"
    assert state.enabled is True


def test_node_without_name_shows_its_id():
    """Название узла приходит из подписки, и его может не быть.

    Прятать рабочий узел из-за пропущенного поля нельзя: клиент не увидит
    в списке тот самый, на который его просит переключить поддержка.
    """
    state = router_nodes.parse('{"nodes":[{"id":"cfg07","name":""}]}')

    assert state.nodes[0].name == "cfg07"


def test_missing_enabled_means_service_works():
    """Старая прошивка поля не пишет, а сервис у неё работает.

    Прочитав молчание как «выключено», приложение показало бы клиенту
    выключенный переключатель при работающем доступе — и он бы его «починил»,
    перезапустив сервис на ровном месте.
    """
    state = router_nodes.parse('{"nodes":[],"current":""}')

    assert state.enabled is True


def test_explicit_off_is_respected():
    state = router_nodes.parse('{"enabled":false,"nodes":[],"current":""}')

    assert state.enabled is False


def test_unknown_fields_do_not_break_the_answer():
    """Парк обновляется не в один день: новый ответ обязан читаться старым
    сервером, иначе выкат прошивки требует одновременного выката сервера."""
    state = router_nodes.parse(
        '{"enabled":true,"current":"cfg01","latency_ms":42,'
        '"nodes":[{"id":"cfg01","name":"Amsterdam 1","flag":"nl"}]}'
    )

    assert state.current == "cfg01"
    assert state.nodes[0].name == "Amsterdam 1"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("unknown_node", "больше нет"),
        ("busy", "предыдущую"),
        ("no_nodes", "подписка"),
    ],
)
def test_known_refusals_become_human_text(code, expected):
    with pytest.raises(router_nodes.NodeError) as exc:
        router_nodes.parse('{"error":"%s"}' % code)

    assert expected in str(exc.value)


def test_unknown_refusal_still_refuses():
    """Прошивка может научиться новым кодам раньше, чем сервер о них узнает.

    Молча вернуть «всё хорошо» на незнакомый код — худшее, что тут можно
    сделать: клиент увидит «готово» там, где ничего не применилось.
    """
    with pytest.raises(router_nodes.NodeError):
        router_nodes.parse('{"error":"weather_is_bad"}')


@pytest.mark.parametrize("payload", ["", "not json", "[]", "null"])
def test_garbage_is_refused_not_guessed(payload):
    with pytest.raises(router_nodes.NodeError):
        router_nodes.parse(payload)


@pytest.mark.parametrize(
    "node_id",
    ["cfg01; reboot", "cfg01 && rm -rf /", "$(id)", "`id`", "../etc/passwd", "cfg 01", ""],
)
async def test_shell_metacharacters_never_reach_the_router(node_id, monkeypatch):
    """Идентификатор приходит от клиента через приложение.

    Проверка вида стоит до любого обращения к устройству: если она пропустит,
    строка уйдёт в командную строку роутера целиком. Роутер тоже проверяет
    существование узла, но это вторая линия, а не замена первой.
    """
    calls = []

    async def fail(device, command, **kwargs):
        calls.append(command)
        raise AssertionError("до роутера дойти не должно")

    monkeypatch.setattr(router_nodes.router_shell, "run", fail)

    with pytest.raises(router_nodes.NodeError):
        await router_nodes.select(_device(), node_id)

    assert calls == []


async def test_valid_id_reaches_the_router_as_one_argument(monkeypatch):
    """Обратная сторона проверки: обычный идентификатор проходить обязан,
    и уходить он должен отдельным словом, а не склеенным с командой."""
    seen = []

    async def run(device, command, **kwargs):
        seen.append(command)
        return _Result(ANSWER)

    monkeypatch.setattr(router_nodes.router_shell, "run", run)

    state = await router_nodes.select(_device(), "cfg02")

    assert seen == [f"{router_nodes.SCRIPT} use cfg02"]
    assert state.current == "cfg02"


class _Result:
    """Ответ роутера в том виде, в каком его отдаёт `router_shell.run`."""

    def __init__(self, stdout: str, ok: bool = True):
        self.stdout = stdout
        self.ok = ok


def _device():
    class Fake:
        id = 1
        mac = "aa:bb:cc:dd:ee:ff"

    return Fake()
