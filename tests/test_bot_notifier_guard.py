"""Их уведомитель об истечении не должен дублировать наш.

Срок подписки роутера приезжает в их базу зеркалом, и по нему их
`notifications.py` шлёт «заканчивается завтра» и «истекла». Наш воркер
шлёт свои напоминания — за 7/3/1/0 дней и через день после. Клиент получал
бы два разных текста об одном и том же, и второй читал бы как сбой.

Проверка по исходнику, как и остальные тесты их базы: `db_helpers` при
импорте тянет `config`, а тот прогоняет миграцию боевой базы.
"""

from __future__ import annotations

from pathlib import Path

BOT = Path(__file__).resolve().parents[1] / "bot"
DB_HELPERS = (BOT / "db_helpers.py").read_text(encoding="utf-8")
SHOP_SYNC = (BOT / "src" / "shop_sync.py").read_text(encoding="utf-8")

GUARD = "COALESCE(shop_subscription, 0) = 0"


def _function(name: str) -> str:
    start = DB_HELPERS.index(f"async def {name}(")
    return DB_HELPERS[start : DB_HELPERS.index("\nasync def ", start + 1)]


def test_expiring_notifier_skips_mirrored_clients():
    assert GUARD in _function("get_users_with_expiring_subscriptions")


def test_expired_notifier_skips_mirrored_clients():
    assert GUARD in _function("get_users_with_expired_subscriptions")


def test_the_flag_is_created_by_their_migrations():
    """Колонку заводит их init_db, а не только круг зеркала: уведомитель
    и админка читают её независимо от того, дошёл ли круг."""
    assert '"shop_subscription", "INTEGER DEFAULT 0"' in DB_HELPERS


def test_the_mirror_raises_the_flag():
    assert "shop_subscription = 1" in SHOP_SYNC
