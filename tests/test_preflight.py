"""Настройки, без которых магазин молча не продаёт, называются при старте.

Базу и Redis проверяют давно: без них ничего не поднимется, и это видно
сразу. Хуже те настройки, без которых всё поднимается и выглядит живым:
приложение не открывается ни у кого, оплатить нечем, тревоги уходят в
никуда. Графики при этом зелёные, и узнать об этом можно было только от
покупателя, который не смог купить.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from core import preflight
from core.config import settings


@pytest.fixture
def shop_ready(monkeypatch):
    """Магазин, у которого всё на месте."""
    monkeypatch.setattr(settings.miniapp, "open_to_all", True)
    monkeypatch.setattr(settings.miniapp, "bot_token", SecretStr("123:token"))
    monkeypatch.setattr(settings.bot, "alerts_chat_id", -100500)
    monkeypatch.setattr(preflight, "get_provider", lambda _name: _Provider(True))
    return settings


class _Provider:
    def __init__(self, configured: bool) -> None:
        self.is_configured = configured


def test_a_ready_shop_complains_about_nothing(shop_ready):
    assert preflight.sale_blockers() == []


def test_a_closed_app_is_named(shop_ready, monkeypatch):
    monkeypatch.setattr(settings.miniapp, "open_to_all", False)
    monkeypatch.setattr(settings.miniapp, "allowed_tg_ids", [])

    (complaint,) = preflight.sale_blockers()
    assert "не открывается ни у кого" in complaint


def test_no_way_to_pay_is_named(shop_ready, monkeypatch):
    monkeypatch.setattr(preflight, "get_provider", lambda _name: _Provider(False))

    (complaint,) = preflight.sale_blockers()
    assert "приём денег" in complaint


def test_alerts_going_nowhere_are_named(shop_ready, monkeypatch):
    monkeypatch.setattr(settings.bot, "alerts_chat_id", 0)
    monkeypatch.setattr(settings.bot, "owner_id", 0)

    (complaint,) = preflight.sale_blockers()
    assert "в никуда" in complaint


def test_the_owner_alone_is_enough_for_alerts(shop_ready, monkeypatch):
    """Отдельного канала может не быть — тогда пишем владельцу."""
    monkeypatch.setattr(settings.bot, "alerts_chat_id", 0)
    monkeypatch.setattr(settings.bot, "owner_id", 777)

    assert preflight.sale_blockers() == []


def test_a_broken_provider_does_not_break_the_check(shop_ready, monkeypatch):
    """Провайдера может не быть вовсе — проверка обязана дойти до конца.

    Иначе одна незаполненная строка прячет две остальные жалобы.
    """

    def _explode(_name):
        raise RuntimeError("провайдер не собрался")

    monkeypatch.setattr(preflight, "get_provider", _explode)
    monkeypatch.setattr(settings.bot, "alerts_chat_id", 0)
    monkeypatch.setattr(settings.bot, "owner_id", 0)

    complaints = preflight.sale_blockers()
    assert len(complaints) == 2
    assert any("приём денег" in c for c in complaints)
    assert any("в никуда" in c for c in complaints)


def test_the_report_says_it_out_loud(shop_ready, monkeypatch):
    """Уровень «ошибка» намеренно: предупреждений при старте много."""
    monkeypatch.setattr(settings.bot, "alerts_chat_id", 0)
    monkeypatch.setattr(settings.bot, "owner_id", 0)
    said: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        preflight.log, "error", lambda event, **kw: said.append((event, kw))
    )

    missing = preflight.report("api")

    assert missing
    (event, fields) = said[0]
    assert event == "preflight.not_ready"
    assert fields["service"] == "api"
