"""Оплата подтверждается опросом, а не колбэком.

Колбэк PLATEGA настроен на другого бота того же мерчанта — решение заказчика
от 21 августа 2026. Значит об оплате мы узнаём только из опроса статусов,
и всё, что раньше было страховкой, стало основным путём.

Отсюда два требования, каждое из которых стоит денег, если его не соблюсти:
платёж нельзя гасить по своим часам, не спросив провайдера, и ждать
подтверждения клиент должен минуты, а не пять минут.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAYMENTS = (ROOT / "core" / "services" / "payments.py").read_text(encoding="utf-8")
TASK = (ROOT / "worker" / "tasks" / "payments.py").read_text(encoding="utf-8")
SCHEDULER = (ROOT / "worker" / "scheduler.py").read_text(encoding="utf-8")


def _expiry_body() -> str:
    start = PAYMENTS.index("async def expire_stale_payments")
    return PAYMENTS[start:]


class TestExpiryAsksTheProvider:
    """Клиент мог заплатить в последнюю минуту жизни ссылки."""

    def test_status_is_checked_before_cancelling(self):
        body = _expiry_body()
        assert body.index("sync_pending_payment") < body.index("PaymentStatus.CANCELED"), (
            "спрашивать провайдера надо до того, как гасим платёж, а не после"
        )

    def test_paid_payment_is_not_cancelled(self):
        """Успевший платёж пропускается, а не переводится в отменённые."""
        body = _expiry_body()
        window = body[body.index("sync_pending_payment") :][:400]
        assert "continue" in window

    def test_unreachable_provider_leaves_payment_pending(self):
        """Вечно висящий платёж чинится глазами, потерянная оплата — скандалом."""
        body = _expiry_body()
        handler = body[body.index("except Exception") :]
        # Обработчик кончается на `continue` — до него платёж не гасится.
        handler = handler[: handler.index("continue")]
        assert "CANCELED" not in handler, "недоступный провайдер не повод гасить платёж"


class TestPollingIsFastEnough:
    def test_payment_is_asked_about_within_a_minute(self):
        """Каждая лишняя минута — минута, которую клиент смотрит
        на «ждёт оплаты» уже после того, как заплатил."""
        from worker.tasks.payments import MIN_AGE

        assert dt.timedelta(minutes=1) >= MIN_AGE

    def test_polling_runs_every_minute(self):
        block = SCHEDULER[SCHEDULER.index('"sync_pending_payments"') :][:400]
        assert "IntervalTrigger(minutes=1)" in block

    def test_only_recent_payments_are_polled(self):
        """Иначе опрос будет вечно дёргать провайдера по мёртвым ссылкам."""
        from worker.tasks.payments import MAX_AGE

        assert dt.timedelta(days=1) >= MAX_AGE

    def test_batch_is_bounded(self):
        from worker.tasks.payments import BATCH

        assert 0 < BATCH <= 100


class TestForeignCallbackStillForwarded:
    """Если колбэк однажды вернут нам, чужое должно уходить дальше."""

    def test_missing_partner_url_is_logged(self):
        source = (ROOT / "api" / "routes" / "webhooks.py").read_text(encoding="utf-8")
        assert "webhook.partner_url_missing" in source

    def test_unknown_payment_is_not_swallowed(self):
        source = (ROOT / "api" / "routes" / "webhooks.py").read_text(encoding="utf-8")
        assert "_forward_to_partner" in source


class TestPollingCoversEveryPurpose:
    def test_task_does_not_filter_by_purpose(self):
        """Доставка оплачивается вторым платежом — его тоже надо подтвердить."""
        body = TASK[TASK.index("async def sync_pending_payments") :]
        assert "purpose" not in body, "опрос должен брать все висящие платежи, а не только заказы"


class TestHangingPaymentsExpire:
    """Платёж без срока висел «ждёт оплаты» вечно.

    Срок ссылки приходит в ответе провайдера полем `expiresIn`, но приходит
    не всегда. Выборка просроченных смотрела только на него, поэтому платёж
    без срока не попадал в неё никогда — счёт на доставку так и висел сутки
    после выставления.
    """

    def _source(self, relative: str) -> str:
        return (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")

    def test_fallback_ttl_is_configurable(self):
        from core.config import settings

        assert settings.platega.link_ttl_min >= 1

    def test_new_payment_always_gets_a_deadline(self):
        source = self._source("core/services/payments.py")
        body = source[source.index("    result = await provider.create_payment(request)") :]
        body = body[: body.index("log.info(")]
        assert "result.expires_at or" in body, "провайдер молчит — ставим свой срок"

    def test_old_payments_without_deadline_are_expired_too(self):
        """Те, что заведены до появления запасного значения, тоже надо гасить —
        иначе они останутся в списке навсегда."""
        source = self._source("core/services/payments.py")
        body = source[source.index("async def expire_stale_payments") :]
        body = body[: body.index("return count")]
        assert "Payment.expires_at.is_(None)" in body
        assert "Payment.created_at <" in body

    def test_provider_is_asked_before_cancelling(self):
        """Клиент мог заплатить в последнюю минуту: гасить вслепую дороже."""
        source = self._source("core/services/payments.py")
        body = source[source.index("async def expire_stale_payments") :]
        body = body[: body.index("return count")]
        assert "sync_pending_payment" in body


class TestManualCancel:
    """Оператор может погасить висящий платёж руками."""

    def _source(self, relative: str) -> str:
        return (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")

    def test_endpoint_asks_the_provider_first(self):
        api = self._source("api/routes/catalog_api.py")
        body = api[api.index("async def manage_payment_cancel") :]
        body = body[: body.index("\n@router")]
        assert "sync_pending_payment" in body
        assert "PaymentStatus.CANCELED" in body

    def test_only_pending_can_be_cancelled(self):
        api = self._source("api/routes/catalog_api.py")
        body = api[api.index("async def manage_payment_cancel") :]
        body = body[: body.index("\n@router")]
        assert "is not PaymentStatus.PENDING" in body

    def test_silent_provider_leaves_the_payment_alone(self):
        """Не ответил — не отменяем: потерянная оплата дороже висящей строки."""
        api = self._source("api/routes/catalog_api.py")
        body = api[api.index("async def manage_payment_cancel") :]
        body = body[: body.index("\n@router")]
        assert "не ответил" in body

    def test_button_only_for_pending(self):
        page = self._source("bot/web_admin/templates/payments_shop.html")
        assert "{% if item.status == 'pending' %}" in page
        assert "admin.payment_shop_cancel" in page


class TestLockedPaymentIsReadAfresh:
    """Блокировка строки не помогает, если объект берут из памяти сессии.

    `SELECT ... FOR UPDATE` действительно ждёт чужую транзакцию, но SQLAlchemy
    возвращает уже загруженный объект как есть: без `populate_existing` его
    поля остаются теми, какими были до ожидания. Так и выходит двойное
    зачисление — круги опроса и погашения идут рядом и берут одни и те же
    платежи: второй видит `processed_at` пустым и проводит оплату заново,
    то есть продлевает подписку ещё на период и снова начисляет бонус.
    """

    async def _factory(self, tmp_path):
        from sqlalchemy import BigInteger
        from sqlalchemy.dialects.postgresql import JSONB
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.ext.compiler import compiles

        from core.models import Payment, User
        from core.models.base import Base

        @compiles(JSONB, "sqlite")
        def _jsonb_as_json(_type, _compiler, **_kwargs) -> str:
            return "JSON"

        @compiles(BigInteger, "sqlite")
        def _bigint_as_integer(_type, _compiler, **_kwargs) -> str:
            return "INTEGER"

        # База файлом, а не в памяти: нужны две настоящие сессии, которые
        # видят коммиты друг друга, — в этом вся суть проверки.
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'payments.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=[User.__table__, Payment.__table__]
                )
            )
        # `expire_on_commit=False` — как в бою (`core/db.py`): именно поэтому
        # объект и остаётся в памяти сессии со старыми полями.
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    async def test_lock_returns_the_row_as_it_is_now(self, tmp_path):
        from decimal import Decimal

        from sqlalchemy import select, update

        from core.enums import PaymentProviderName, PaymentPurpose, PaymentStatus
        from core.models import Payment, User
        from core.services import payments as payment_service

        engine, factory = await self._factory(tmp_path)
        try:
            async with factory() as setup:
                setup.add(User(id=1, tg_id=614685408))
                setup.add(
                    Payment(
                        id=1,
                        user_id=1,
                        provider=PaymentProviderName.PLATEGA,
                        purpose=PaymentPurpose.ORDER,
                        status=PaymentStatus.PENDING,
                        idempotency_key="key-1",
                        amount=Decimal("100.00"),
                        currency="RUB",
                    )
                )
                await setup.commit()

            async with factory() as polling:
                # Круг опроса прочитал платёж и ушёл спрашивать провайдера.
                seen = await polling.scalar(select(Payment).where(Payment.id == 1))
                assert seen.processed_at is None
                await polling.commit()

                # Пока он ходил, соседний круг провёл этот же платёж.
                async with factory() as expiry:
                    await expiry.execute(
                        update(Payment)
                        .where(Payment.id == 1)
                        .values(
                            processed_at=dt.datetime.now(dt.UTC),
                            status=PaymentStatus.SUCCEEDED,
                        )
                    )
                    await expiry.commit()

                locked = await payment_service._lock_payment(polling, 1)

                assert locked is not None
                assert locked.processed_at is not None, (
                    "под блокировкой взят снимок из памяти: платёж проведут второй раз"
                )
                assert locked.status is PaymentStatus.SUCCEEDED
        finally:
            await engine.dispose()
