"""Подхват из панели сроков у роутеров, включённых до 27 августа 2026.

Тогда ручная активация заводила одну учётку в панели, а у нас — ничего.
Такие роутеры работают, но в парке у них «подписка: нет», клиенту не уходят
напоминания, а доступ панель отключит в свою дату молча. Скрипт заводит им
подписку на срок из панели. Здесь стерегутся две вещи: что он трогает
только живые сроки и что в истории подписки он не выдаёт себя за кнопку.
"""

from __future__ import annotations

import datetime as dt

import pytest

from core.enums import SubscriptionStatus
from core.services import subscriptions as service
from scripts.adopt_panel_subscriptions import ADOPT, EXPIRED, NO_ACCOUNT, NO_DATE, classify

NOW = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.UTC)


class TestClassify:
    def test_live_date_is_adopted(self):
        assert classify(NOW + dt.timedelta(days=68), has_account=True, now=NOW) == ADOPT

    def test_expired_is_left_to_a_human(self):
        """Заведи её — клиенту уйдёт «подписка закончилась» о доступе,
        который кончился давно и, может быть, по договорённости."""
        assert classify(NOW - dt.timedelta(minutes=1), has_account=True, now=NOW) == EXPIRED

    def test_no_account(self):
        assert classify(None, has_account=False, now=NOW) == NO_ACCOUNT

    def test_account_without_date(self):
        assert classify(None, has_account=True, now=NOW) == NO_DATE


class _Session:
    def __init__(self):
        self.added: list = []

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_adopted_subscription_says_where_it_came_from(monkeypatch):
    session = _Session()
    recorded: dict = {}

    async def _nothing(_session, _device_id):
        return None

    def _record(subscription, event, **kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr(service, "get_for_device", _nothing)
    monkeypatch.setattr(service, "_record", _record)

    until = NOW + dt.timedelta(days=68, hours=5)
    activated = NOW - dt.timedelta(days=60)
    result = await service.grant_manual(
        session,
        user_id=3,
        device_id=9,
        days=68,
        until=until,
        now=activated,
        source="panel",
        comment="подхвачено из панели",
    )

    assert session.added == [result]
    assert result.source == "panel"
    assert result.status is SubscriptionStatus.ACTIVE
    # Срок — ровно тот, что в панели, а не пересчитанный днями: иначе наша
    # дата отстаёт от панельной на часы, и доступ отключается не тогда.
    assert result.expires_at == until
    assert result.started_at == activated
    assert recorded["comment"] == "подхвачено из панели"


@pytest.mark.asyncio
async def test_button_activation_is_still_called_so(monkeypatch):
    session = _Session()
    recorded: dict = {}

    async def _nothing(_session, _device_id):
        return None

    monkeypatch.setattr(service, "get_for_device", _nothing)
    monkeypatch.setattr(service, "_record", lambda _s, _e, **kw: recorded.update(kw))

    result = await service.grant_manual(session, user_id=3, device_id=9, days=30, now=NOW)

    assert result.source == "manual"
    assert recorded["comment"] == "Ручная активация на 30 дн."
