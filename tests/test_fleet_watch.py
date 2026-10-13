"""Сторож парка: весь парк пропал разом — сообщение сразу и с подсказкой.

4 октября 2026 ушли все роутеры, а узнали мы случайно и час искали, где
рвётся. Логика сторожа — чистая функция `decide`, и здесь пройдены все её
переходы: разовый сбой молчит, держащаяся поломка — одна тревога, возврат —
одно «восстановилось», и ни одного повторного сообщения посреди простоя.
"""

from __future__ import annotations

import datetime as dt

import pytest

from core.services.fleet_watch import (
    CONFIRM_RUNS,
    FLEET,
    FRPS,
    TUNNELS,
    Observation,
    State,
    decide,
)

T0 = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.UTC)


def run(state: State, *observations: Observation, step_min: int = 2) -> tuple[State, list[str]]:
    texts = []
    for index, obs in enumerate(observations):
        state, text = decide(state, obs, T0 + dt.timedelta(minutes=step_min * index))
        if text:
            texts.append(text)
    return state, texts


OK6 = Observation(reachable=True, online=6, tunnels_ok=True)
NONE = Observation(reachable=True, online=0, tunnels_ok=None)
DEAD = Observation(reachable=False, error="ConnectTimeout")


class TestQuietWhenFine:
    def test_steady_fleet(self):
        state, texts = run(State(), OK6, OK6, OK6)
        assert texts == []
        assert state.status == "ok"
        assert state.baseline == 6

    def test_clients_switching_off_one_by_one(self):
        """По одному за ночь — это клиенты, а не поломка."""
        steps = [Observation(reachable=True, online=n, tunnels_ok=True) for n in (6, 5, 4, 3, 2)]
        _, texts = run(State(), *steps)
        assert texts == []

    def test_small_fleet_is_not_a_mass_drop(self):
        """Два роутера из двух выключили на ночь — тревоги нет."""
        two = Observation(reachable=True, online=2, tunnels_ok=True)
        _, texts = run(State(), two, NONE, NONE, NONE)
        assert texts == []


class TestOneBlipIsNotAnAlarm:
    def test_single_failed_round(self):
        state, texts = run(State(), OK6, NONE, OK6)
        assert texts == []
        assert state.status == "ok"

    def test_needs_consecutive_rounds(self):
        assert CONFIRM_RUNS >= 2


class TestMassDrop:
    def test_alert_once_with_numbers(self):
        state, texts = run(State(), OK6, NONE, NONE, NONE, NONE)
        assert len(texts) == 1, "посреди простоя сторож не должен повторяться"
        assert state.status == "down"
        assert state.reason == FLEET
        assert "было 6, сейчас 0" in texts[0]

    def test_recovered_once(self):
        state, texts = run(State(), OK6, NONE, NONE, OK6, OK6)
        assert len(texts) == 2
        assert texts[1].startswith("✅")
        assert "6 из 6" in texts[1]
        assert state.status == "ok"

    def test_partial_return_is_not_recovery(self):
        three = Observation(reachable=True, online=3, tunnels_ok=True)
        state, texts = run(State(), OK6, NONE, NONE, three)
        assert len(texts) == 1
        assert state.status == "down"

    def test_baseline_survives_the_outage(self):
        """Иначе через круг «стало ноль» превратилось бы в «было ноль»."""
        state, _ = run(State(), OK6, NONE, NONE, NONE)
        assert state.baseline == 6


class TestWhereItBreaks:
    def test_frps_unreachable(self):
        state, texts = run(State(), OK6, DEAD, DEAD)
        assert state.reason == FRPS
        assert "frps не отвечает" in texts[0]
        assert "ConnectTimeout" in texts[0]

    def test_frps_down_from_a_cold_start(self):
        """Воркер поднялся, а frps уже лежит — точки отсчёта нет, но молчать нельзя."""
        _, texts = run(State(), DEAD, DEAD)
        assert len(texts) == 1

    def test_tunnels(self):
        broken = Observation(reachable=True, online=6, tunnels_ok=False)
        state, texts = run(State(), OK6, broken, broken)
        assert state.reason == TUNNELS
        assert "frpc" in texts[0]

    def test_problem_must_be_the_same_to_count(self):
        """Сбой одного рода, потом другого — это два разовых сбоя, не тревога."""
        broken = Observation(reachable=True, online=6, tunnels_ok=False)
        _, texts = run(State(), OK6, DEAD, broken, OK6)
        assert texts == []


class TestLongOutage:
    def test_a_day_later_it_becomes_the_new_normal(self):
        """Роутеры сняли, парк уменьшился — ждать их вечно сторож не должен."""
        two = Observation(reachable=True, online=2, tunnels_ok=True)
        state, _ = run(State(), OK6, two, two)
        assert state.status == "down"
        state, text = decide(state, two, T0 + dt.timedelta(hours=25))
        assert state.status == "ok"
        assert state.baseline == 2
        assert "новой нормой" in text


class TestState:
    def test_round_trip(self):
        state = State("down", FLEET, 6, 2, T0.isoformat())
        assert State.load(state.dump()) == state

    @pytest.mark.parametrize("raw", [None, "", [], {"status": "ok", "garbage": 1}])
    def test_broken_value_is_a_clean_start(self, raw):
        assert State.load(raw).status == "ok"
