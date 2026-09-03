"""Разбор ответа роутера про узлы сервиса доступа.

Проверяется не «json.loads работает», а то, ради чего разбор написан отдельно:
клиент не должен остаться без доступа из-за прошивки, которая ответила иначе,
чем ждал сервер, и не должен уметь протащить в командную строку постороннее.
"""

from __future__ import annotations

import json

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


def test_empty_list_is_success_not_refusal():
    """Пустой список узлов — не отказ.

    Отказ на `list` уносит с экрана и переключатель сервиса, а он клиенту
    в этот момент нужнее всего: подписка ещё не прочиталась, доступа нет,
    и единственное, что он может сделать сам, — выключить и включить.
    """
    state = router_nodes.parse('{"enabled":true,"current":"","nodes":[]}')

    assert state.nodes == []
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
        ("no_service", "не настроен"),
        ("commit_failed", "сохранить"),
        ("bad_usage", "не понял"),
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


class TestNamesAsTheClientSeesThem:
    """Названия узлов приходят из подписки и написаны для нас, не для клиента."""

    def test_service_prefix_is_dropped(self):
        """`Router_Германия` говорит, что узел роутерный.

        Клиенту это не сообщает ничего — он и так в приложении своего
        роутера, — а список из семи строк с одинаковым началом читается
        тяжелее, чем список из семи стран.
        """
        state = router_nodes.parse(
            '{"nodes":[{"id":"a","name":"Router_Германия"},'
            '{"id":"b","name":"Router_ Эстония 1"}]}'
        )

        assert [node.name for node in state.nodes] == ["Германия", "Эстония 1"]

    def test_number_that_tells_nodes_apart_stays(self):
        """`Финляндия` и `Финляндия 1` — разные узлы, и цифра их различает."""
        state = router_nodes.parse(
            '{"nodes":[{"id":"a","name":"Router_Финляндия"},'
            '{"id":"b","name":"Router_Финляндия 1"}]}'
        )

        assert [node.name for node in state.nodes] == ["Финляндия", "Финляндия 1"]

    def test_balancer_is_named_and_goes_first(self):
        """Балансер — единственный путь вернуться к автовыбору.

        Клиент, разок ткнувший страну, без него остался бы на ней навсегда,
        поэтому его не прячут, а называют понятным словом и ставят наверх,
        чтобы не искать среди стран.
        """
        state = router_nodes.parse(
            '{"nodes":[{"id":"a","name":"Router_Германия"},'
            '{"id":"b","name":"TitanSwitch"}]}'
        )

        assert state.nodes[0].name == router_nodes.AUTO_TITLE
        assert state.nodes[0].auto is True
        assert state.nodes[1].auto is False

    def test_name_of_only_a_prefix_falls_back_to_id(self):
        state = router_nodes.parse('{"nodes":[{"id":"cfg09","name":"Router_"}]}')

        assert state.nodes[0].name == "cfg09"

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Router_Германия", "\U0001f1e9\U0001f1ea"),
            ("Router_Нидерланды", "\U0001f1f3\U0001f1f1"),
            ("Finland 2", "\U0001f1eb\U0001f1ee"),
            ("TitanSwitch", ""),
            ("Узел 7", ""),
        ],
    )
    def test_flag_by_country_or_nothing(self, name, expected):
        """Незнакомая страна остаётся без флага.

        Список стран неполный намеренно: гадать по двум буквам значит
        однажды показать клиенту чужой флаг, а это хуже, чем его отсутствие.
        """
        state = router_nodes.parse('{"nodes":[{"id":"a","name":%s}]}' % json.dumps(name))

        assert state.nodes[0].flag == expected
