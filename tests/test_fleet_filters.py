"""Фильтры и массовые действия на странице роутеров.

Фильтр, показывающий не то, дороже отсутствующего: оператор отбирает
«подписки нет», видит десять строк и идёт по ним звонить — а там половина
чужих. Поэтому границы каждого значения проверяются поимённо.

«Нет подписки» и «на другом роутере» разведены намеренно: в первом случае
клиент не платил, во втором заплатил, а роутер срока не получил — это и есть
второй роутер, который молча не активировался.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.routes.fleet_api import (
    BULK_LIMIT,
    EXPIRING_SOON_DAYS,
    ROUTERS_PAGE_SIZES,
    _matches_link,
    _matches_sub,
)

NOW = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.UTC)


def device(device_id: int = 1, *, frp_online: bool = False, heartbeat: bool = False):
    return SimpleNamespace(
        id=device_id,
        frp_online=frp_online,
        is_online=lambda threshold_min, now: heartbeat,
    )


def subscription(device_id: int | None, *, expires_in_days: int | None = 30):
    expires = NOW + dt.timedelta(days=expires_in_days) if expires_in_days is not None else None
    return SimpleNamespace(device_id=device_id, expires_at=expires)


class TestLinkFilter:
    def test_empty_filter_keeps_everything(self):
        assert _matches_link(device(), "", now=NOW)

    def test_tunnel_counts_as_online(self):
        """Роутер за туннелем на связи, даже если heartbeat давно не приходил."""
        assert _matches_link(device(frp_online=True), "online", now=NOW)
        assert not _matches_link(device(frp_online=True), "offline", now=NOW)

    def test_fresh_heartbeat_counts_as_online(self):
        assert _matches_link(device(heartbeat=True), "online", now=NOW)

    def test_silent_router(self):
        assert _matches_link(device(), "offline", now=NOW)
        assert not _matches_link(device(), "online", now=NOW)


class TestSubscriptionFilter:
    def test_empty_filter_keeps_everything(self):
        assert _matches_sub(device(), "", {}, now=NOW)

    def test_none_means_no_subscription_at_all(self):
        assert _matches_sub(device(1), "none", {1: None}, now=NOW)
        assert not _matches_sub(device(1), "none", {1: subscription(1)}, now=NOW)

    def test_active_means_active_on_this_router(self):
        """Подписка клиента, лежащая на другом роутере, этот не делает активным."""
        assert _matches_sub(device(1), "active", {1: subscription(1)}, now=NOW)
        assert not _matches_sub(device(1), "active", {1: subscription(2)}, now=NOW)

    def test_elsewhere_finds_the_second_router(self):
        """Клиент заплатил, но срок ушёл первому роутеру — второй не активирован."""
        assert _matches_sub(device(1), "elsewhere", {1: subscription(2)}, now=NOW)
        assert not _matches_sub(device(1), "elsewhere", {1: subscription(1)}, now=NOW)

    def test_elsewhere_needs_a_subscription(self):
        assert not _matches_sub(device(1), "elsewhere", {1: None}, now=NOW)

    @pytest.mark.parametrize("days", [0, 1, EXPIRING_SOON_DAYS])
    def test_expiring_covers_the_next_week(self, days):
        assert _matches_sub(device(1), "expiring", {1: subscription(1, expires_in_days=days)}, now=NOW)

    @pytest.mark.parametrize("days", [EXPIRING_SOON_DAYS + 1, 90])
    def test_far_away_is_not_expiring(self, days):
        assert not _matches_sub(
            device(1), "expiring", {1: subscription(1, expires_in_days=days)}, now=NOW
        )

    def test_already_expired_is_not_expiring(self):
        """Истёкшая не «истекает»: звонить по ней поздно, это другой список."""
        assert not _matches_sub(
            device(1), "expiring", {1: subscription(1, expires_in_days=-1)}, now=NOW
        )

    def test_endless_subscription_is_not_expiring(self):
        assert not _matches_sub(
            device(1), "expiring", {1: subscription(1, expires_in_days=None)}, now=NOW
        )

    def test_expiring_on_another_router_is_not_ours(self):
        assert not _matches_sub(
            device(1), "expiring", {1: subscription(2, expires_in_days=1)}, now=NOW
        )


class TestPageSizes:
    def test_sizes_are_a_closed_list(self):
        """Иначе `per_page=100000` в адресе вернёт нас к странице во весь парк."""
        assert tuple(sorted(ROUTERS_PAGE_SIZES)) == ROUTERS_PAGE_SIZES
        assert all(size > 0 for size in ROUTERS_PAGE_SIZES)


class TestBulkAndFilterWiring:
    """Проверки по исходнику: фильтр, потерянный в ссылке, — молчаливая ошибка."""

    def _source(self, relative: str) -> str:
        return (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")

    def test_pagination_carries_every_filter(self):
        page = self._source("bot/web_admin/templates/routers_fleet.html")
        assert "**nav" in page, (
            "ссылки страниц должны нести все фильтры разом: перечисление по одному "
            "уже теряло их при добавлении нового"
        )

    def test_filter_keys_are_listed_once(self):
        route = self._source("bot/web_admin/routes/routers_fleet.py")
        assert "FLEET_FILTER_KEYS" in route
        for key in ("sub", "state", "model", "per_page"):
            assert f'"{key}"' in route

    def test_bulk_limit_is_enforced(self):
        api = self._source("api/routes/fleet_api.py")
        assert "BULK_LIMIT" in api and str(BULK_LIMIT) in api

    def test_one_failure_does_not_cancel_the_rest(self):
        """Половина парка молчит всегда — опрос тридцати не должен падать целиком."""
        api = self._source("api/routes/fleet_api.py")
        body = api[api.index("async def bulk_routers") :]
        body = body[: body.index('return {"ok": True, "done"')]
        assert body.count("continue") >= 3, "отказ одного роутера должен пропускаться, а не ронять всё"
