"""Оплаченный срок доезжает до панели, даже если она молчала в тот момент.

Доступ отключает панель, по своей дате. Наша дата — то, что видит оператор
и сам клиент. Переносился срок ровно один раз, в момент проведения оплаты,
и молча сдавался: панель недоступна, учётка не нашлась, ответ не тот.
Клиент платил, у нас всё выглядело продлённым, а доступ обрывался в старую
дату. Узнавали об этом от клиента.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.models import Device, Subscription
from core.services import activation

ROOT = Path(__file__).resolve().parents[1]
MAC = "D4-0D-AB-28-3B-80"


def _account(expire_at: str):
    return SimpleNamespace(
        username=MAC.lower(), uid="u-1", uid_key="uuid", subscription_url="", expire_at=expire_at
    )


def _world(monkeypatch, *, panel_expiry: str):
    """Панель с одной учёткой; записываем всё, что ей велят поставить."""
    written: dict = {}
    account = _account(panel_expiry)

    class _Panel:
        async def users(self):
            return [account]

        async def update_expiry(self, account, *, expire_at):
            written["expire_at"] = expire_at

    monkeypatch.setattr(activation.remnawave, "client", _Panel)
    monkeypatch.setattr(activation.routers, "add_event", lambda *a, **k: None)

    device = Device(id=5, mac=MAC, status=activation.DeviceStatus.ACTIVE)

    class _Session:
        async def get(self, model, key):
            return device if model is Device else None

    return _Session(), written


def _subscription(expires_at: dt.datetime) -> Subscription:
    return Subscription(id=3, user_id=7, device_id=5, expires_at=expires_at)


@pytest.mark.asyncio
async def test_a_matching_panel_is_not_rewritten_every_night(monkeypatch):
    """Сверка ходит по всему парку: лишняя запись — лишний запрос к панели."""
    session, written = _world(monkeypatch, panel_expiry="2026-10-01T12:00:00.000Z")
    same = _subscription(dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC))

    assert await activation.sync_panel_expiry(session, same, only_forward=True) is True
    assert written == {}, "панель уже знает эту дату"


@pytest.mark.asyncio
async def test_a_panel_left_behind_is_caught_up(monkeypatch):
    """Тот самый случай: оплата прошла, панель в тот момент не ответила."""
    session, written = _world(monkeypatch, panel_expiry="2026-09-01T12:00:00.000Z")
    paid_until = dt.datetime(2026, 12, 1, 12, 0, tzinfo=dt.UTC)

    assert await activation.sync_panel_expiry(session, _subscription(paid_until), only_forward=True)
    assert written["expire_at"] == paid_until


@pytest.mark.asyncio
async def test_days_given_out_by_hand_are_not_taken_back(monkeypatch):
    """Оператор продлил учётку прямо в панели — сверка не должна это отменять.

    Иначе выданные им дни исчезали бы каждую ночь, и он бы выдавал их снова.
    """
    session, written = _world(monkeypatch, panel_expiry="2027-01-01T12:00:00.000Z")
    ours = _subscription(dt.datetime(2026, 12, 1, 12, 0, tzinfo=dt.UTC))

    assert await activation.sync_panel_expiry(session, ours, only_forward=True) is True
    assert written == {}


@pytest.mark.asyncio
async def test_right_after_a_payment_our_date_still_wins(monkeypatch):
    """Продление считаем мы, и сразу после оплаты правильная дата наша.

    Без `only_forward` поведение прежнее: иначе оплаченное продление не
    доехало бы до панели, оказавшейся почему-либо впереди.
    """
    session, written = _world(monkeypatch, panel_expiry="2027-01-01T12:00:00.000Z")
    ours = _subscription(dt.datetime(2026, 12, 1, 12, 0, tzinfo=dt.UTC))

    assert await activation.sync_panel_expiry(session, ours) is True
    assert written["expire_at"] == ours.expires_at


@pytest.mark.asyncio
async def test_a_silent_panel_is_not_reported_as_synced(monkeypatch):
    """Ответ False — это сигнал сверке записать в журнал, а не тишина."""
    from core.services import remnawave

    class _Panel:
        async def users(self):
            raise remnawave.RemnawaveError("панель не отвечает")

    monkeypatch.setattr(activation.remnawave, "client", _Panel)
    device = Device(id=5, mac=MAC, status=activation.DeviceStatus.ACTIVE)

    class _Session:
        async def get(self, model, key):
            return device if model is Device else None

    ours = _subscription(dt.datetime(2026, 12, 1, 12, 0, tzinfo=dt.UTC))
    assert await activation.sync_panel_expiry(_Session(), ours, only_forward=True) is False


class TestTheNightlyCheckExists:
    def _task(self) -> str:
        return (ROOT / "worker" / "tasks" / "subscriptions.py").read_text(encoding="utf-8")

    def test_it_only_goes_forward(self):
        body = self._task()
        head = body.index("async def resync_panel_expiry")
        block = body[head : body.index("\ndef next_reminder_run", head)]
        assert "only_forward=True" in block

    def test_it_looks_at_live_subscriptions_with_a_router(self):
        head = self._task().index("async def resync_panel_expiry")
        block = self._task()[head : head + 2000]
        assert "LIVE_STATUSES" in block
        assert "device_id.is_not(None)" in block

    def test_it_is_bounded(self):
        """Каждая подписка — запрос к панели. Без предела один круг это весь парк."""
        head = self._task().index("async def resync_panel_expiry")
        block = self._task()[head : head + 2000]
        assert ".limit(limit)" in block

    def test_a_router_the_panel_does_not_know_is_written_down(self):
        """Клиент с оплаченной подпиской может сейчас сидеть вовсе без доступа."""
        head = self._task().index("async def resync_panel_expiry")
        block = self._task()[head : head + 2000]
        assert "panel_out_of_sync" in block

    def test_it_actually_runs(self):
        scheduler = (ROOT / "worker" / "scheduler.py").read_text(encoding="utf-8")
        assert "resync_panel_expiry" in scheduler
