"""Платежи: разбор ответов PLATEGA, проверка колбэка, идемпотентность."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.enums import PaymentStatus
from core.payments.platega import (
    METHOD_CARD,
    METHOD_SBP,
    PlategaProvider,
    _parse_expires_in,
)
from core.services.payments import build_payload, parse_payload


@pytest.fixture
def provider(monkeypatch):
    from pydantic import SecretStr

    from core.config import settings

    monkeypatch.setattr(settings.platega, "merchant_id", "merchant-uuid", raising=False)
    monkeypatch.setattr(settings.platega, "secret", SecretStr("super-secret"), raising=False)
    return PlategaProvider()


class TestPayload:
    """Своего externalId у провайдера нет — наш id ездит в поле payload."""

    def test_roundtrip(self):
        assert parse_payload(build_payload(42)) == 42

    @pytest.mark.parametrize("bad", [None, "", "42", "other:42", "rs:", "rs:abc", "garbage"])
    def test_invalid_payload(self, bad):
        assert parse_payload(bad) is None


class TestExpiresIn:
    def test_parses_duration(self):
        now = dt.datetime(2026, 8, 3, 12, tzinfo=dt.UTC)
        assert _parse_expires_in("00:15:00", now=now) == dt.datetime(2026, 8, 3, 12, 15, tzinfo=dt.UTC)

    def test_parses_hours(self):
        now = dt.datetime(2026, 8, 3, 12, tzinfo=dt.UTC)
        assert _parse_expires_in("01:30:00", now=now) == dt.datetime(2026, 8, 3, 13, 30, tzinfo=dt.UTC)

    @pytest.mark.parametrize("bad", [None, "", "15m", "00:15", "aa:bb:cc"])
    def test_invalid_duration(self, bad):
        assert _parse_expires_in(bad) is None


class TestWebhookVerification:
    """Подписи у провайдера нет: подлинность — по возвращённым реквизитам."""

    def test_valid_headers_pass(self, provider):
        headers = {"X-MerchantId": "merchant-uuid", "X-Secret": "super-secret"}
        assert provider.verify_webhook(headers, b"{}") is True

    def test_header_case_does_not_matter(self, provider):
        headers = {"x-merchantid": "merchant-uuid", "x-secret": "super-secret"}
        assert provider.verify_webhook(headers, b"{}") is True

    def test_wrong_secret_rejected(self, provider):
        headers = {"X-MerchantId": "merchant-uuid", "X-Secret": "wrong"}
        assert provider.verify_webhook(headers, b"{}") is False

    def test_wrong_merchant_rejected(self, provider):
        headers = {"X-MerchantId": "someone-else", "X-Secret": "super-secret"}
        assert provider.verify_webhook(headers, b"{}") is False

    def test_missing_headers_rejected(self, provider):
        assert provider.verify_webhook({}, b"{}") is False


class TestWebhookParsing:
    def test_confirmed_maps_to_succeeded(self, provider):
        result = provider.parse_webhook(
            {
                "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "amount": 6900,
                "currency": "RUB",
                "status": "CONFIRMED",
                "paymentMethod": METHOD_SBP,
                "payload": "rs:17",
            }
        )
        assert result.status is PaymentStatus.SUCCEEDED
        assert result.amount == Decimal("6900")
        assert result.payload == "rs:17"
        assert parse_payload(result.payload) == 17

    def test_canceled_maps_to_canceled(self, provider):
        result = provider.parse_webhook({"id": "x", "status": "CANCELED", "amount": 100})
        assert result.status is PaymentStatus.CANCELED

    def test_chargeback_maps_to_refunded(self, provider):
        result = provider.parse_webhook({"id": "x", "status": "CHARGEBACKED", "amount": 100})
        assert result.status is PaymentStatus.REFUNDED

    def test_unknown_status_stays_pending(self, provider):
        result = provider.parse_webhook({"id": "x", "status": "SOMETHING_NEW"})
        assert result.status is PaymentStatus.PENDING

    def test_missing_id_raises(self, provider):
        from core.payments.base import PaymentProviderError

        with pytest.raises(PaymentProviderError):
            provider.parse_webhook({"status": "CONFIRMED"})

    def test_amount_is_decimal_not_float(self, provider):
        result = provider.parse_webhook({"id": "x", "status": "CONFIRMED", "amount": 1990.5})
        assert isinstance(result.amount, Decimal)
        assert result.amount == Decimal("1990.5")


class TestMethodResolution:
    def test_named_method(self, provider):
        assert provider._resolve_method("sbp") == METHOD_SBP
        assert provider._resolve_method("card") == METHOD_CARD

    def test_numeric_method_passes_through(self, provider):
        assert provider._resolve_method("11") == METHOD_CARD

    def test_any_means_client_chooses(self, provider):
        assert provider._resolve_method("any") is None
        assert provider._resolve_method(None) is None


class TestConfiguration:
    def test_requires_credentials(self, provider):
        assert provider.is_configured is True

    def test_not_configured_without_secret(self, monkeypatch):
        from pydantic import SecretStr

        from core.config import settings

        monkeypatch.setattr(settings.platega, "merchant_id", "", raising=False)
        monkeypatch.setattr(settings.platega, "secret", SecretStr(""), raising=False)
        assert PlategaProvider().is_configured is False


class TestPartnerCallbackForwarding:
    """Чужой колбэк передаётся боту, а не проглатывается.

    Провайдер шлёт уведомления по одному адресу на мерчанта, а платежей два
    вида: железо продаём мы, подписку — бот. Раньше чужое уведомление
    получало голый 200: клиент платил за подписку, а она не включалась.
    """

    @pytest.mark.asyncio
    async def test_forwards_body_and_auth_headers(self, monkeypatch):
        from api.routes import webhooks
        from core.config import settings

        monkeypatch.setattr(
            settings.platega, "partner_callback_url", "http://127.0.0.1:8081/platega/callback"
        )
        sent: dict = {}

        class _Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def post(self, url, content=None, headers=None):
                sent.update(url=url, content=content, headers=headers)
                return type("R", (), {"status_code": 200})()

        monkeypatch.setattr(webhooks.httpx, "AsyncClient", _Client)
        await webhooks._forward_to_partner(
            b'{"id":"x"}',
            {"Content-Type": "application/json", "X-MerchantId": "m", "X-Secret": "s", "Host": "h"},
        )

        assert sent["url"].endswith("/platega/callback")
        assert sent["content"] == b'{"id":"x"}'
        assert sent["headers"]["X-MerchantId"] == "m"
        assert sent["headers"]["X-Secret"] == "s"
        # Host подставит клиент: чужой уронил бы запрос на несовпадении.
        assert "Host" not in sent["headers"]

    @pytest.mark.asyncio
    async def test_empty_url_means_do_not_forward(self, monkeypatch):
        from api.routes import webhooks
        from core.config import settings

        monkeypatch.setattr(settings.platega, "partner_callback_url", "")

        def _boom(**_kwargs):
            raise AssertionError("не должно было ходить никуда")

        monkeypatch.setattr(webhooks.httpx, "AsyncClient", _boom)
        await webhooks._forward_to_partner(b"{}", {})

    @pytest.mark.asyncio
    async def test_unreachable_partner_does_not_raise(self, monkeypatch):
        """Провайдеру мы уже ответили 200 и обязаны отвечать: повторы вечны."""
        import httpx as real_httpx

        from api.routes import webhooks
        from core.config import settings

        monkeypatch.setattr(settings.platega, "partner_callback_url", "http://127.0.0.1:8081/x")

        class _Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def post(self, *_args, **_kwargs):
                raise real_httpx.ConnectError("бот перезапускается")

        monkeypatch.setattr(webhooks.httpx, "AsyncClient", _Client)
        await webhooks._forward_to_partner(b"{}", {})


class TestAmountMismatchCallsPeople:
    """Расхождение суммы обязано дойти до человека.

    Платёж помечается «не прошёл», деньги при этом у провайдера, и разобрать
    это может только оператор. Текст алерта был написан, канал заведён,
    а вызова не было ни одного — узнать о таком можно было лишь из логов.
    """

    @pytest.mark.asyncio
    async def test_operator_is_alerted(self, monkeypatch):
        import datetime as dt
        from decimal import Decimal

        from core.enums import PaymentProviderName, PaymentPurpose, PaymentStatus
        from core.models import Payment
        from core.services import notifier
        from core.services import payments as payment_service

        told: dict = {}

        async def _alert(payment, received, *, session=None):
            told.update(payment_id=payment.id, received=received, session=session)

        monkeypatch.setattr(notifier, "notify_amount_mismatch", _alert)

        payment = Payment(
            id=17,
            user_id=1,
            provider=PaymentProviderName.PLATEGA,
            purpose=PaymentPurpose.ORDER,
            status=PaymentStatus.PENDING,
            idempotency_key="key-17",
            amount=Decimal("9299.00"),
            currency="RUB",
            created_at=dt.datetime.now(dt.UTC),
        )

        class _Session:
            def add(self, _item):
                return None

        session = _Session()
        applied = await payment_service.apply_status(
            session, payment, status=PaymentStatus.SUCCEEDED, amount=Decimal("1.00")
        )

        assert applied is False
        assert payment.status is PaymentStatus.FAILED
        assert told.get("payment_id") == 17, "о расхождении суммы никому не сообщили"
        assert told.get("received") == "1.00"
        assert told.get("session") is session, "без сессии алерт не ляжет в очередь"


class TestCommissionOnTopIsNotAMismatch:
    """Комиссия платёжной системы приходит сверху, и точное совпадение
    заворачивало все оплаты подряд: счёт на 92,99 приходил как 100,43,
    платёж падал в «не прошёл», а оператор получал алерт на каждый заказ.

    Больше выставленного — зачисляем, меньше — по-прежнему нет.
    """

    @staticmethod
    def _payment():
        import datetime as dt
        from decimal import Decimal

        from core.enums import PaymentProviderName, PaymentPurpose, PaymentStatus
        from core.models import Payment

        return Payment(
            id=8,
            user_id=1,
            provider=PaymentProviderName.PLATEGA,
            purpose=PaymentPurpose.ORDER,
            status=PaymentStatus.PENDING,
            idempotency_key="key-8",
            amount=Decimal("92.99"),
            currency="RUB",
            created_at=dt.datetime.now(dt.UTC),
        )

    @pytest.mark.asyncio
    async def test_charge_with_commission_is_credited(self, monkeypatch):
        from decimal import Decimal

        from core.enums import PaymentStatus
        from core.services import notifier
        from core.services import payments as payment_service

        alerts: list = []

        async def _alert(payment, received, *, session=None):
            alerts.append(received)

        async def _granted(session, payment):
            return None

        monkeypatch.setattr(notifier, "notify_amount_mismatch", _alert)
        monkeypatch.setattr(payment_service, "_apply_success", _granted)

        payment = self._payment()
        applied = await payment_service.apply_status(
            object(), payment, status=PaymentStatus.SUCCEEDED, amount=Decimal("100.43")
        )

        assert applied is True, "оплата с комиссией сверху должна зачисляться"
        assert payment.status is PaymentStatus.SUCCEEDED
        assert not alerts, "переплата на комиссию — не повод звать людей"
        assert payment.amount == Decimal("92.99"), (
            "счёт остаётся нашим: комиссия до нас не доходит и выручкой не становится"
        )

    @pytest.mark.asyncio
    async def test_underpayment_still_stops_everything(self, monkeypatch):
        from decimal import Decimal

        from core.enums import PaymentStatus
        from core.services import notifier
        from core.services import payments as payment_service

        alerts: list = []

        async def _alert(payment, received, *, session=None):
            alerts.append(received)

        monkeypatch.setattr(notifier, "notify_amount_mismatch", _alert)

        payment = self._payment()
        applied = await payment_service.apply_status(
            object(), payment, status=PaymentStatus.SUCCEEDED, amount=Decimal("92.98")
        )

        assert applied is False
        assert payment.status is PaymentStatus.FAILED
        assert alerts == ["92.98"], "недоплата обязана дойти до человека"


class TestWebhookSavesWhatItQueued:
    """Подтверждение оплаты должно пережить ответ провайдеру.

    Сообщение клиенту кладётся в очередь строкой в базе. Пока `commit` шёл
    раньше него, строка не сохранялась вовсе: зависимость `get_session`
    сама не коммитит, и при закрытии сессии всё написанное после пропадало.
    Сейчас колбэк уходит другому боту, поэтому баг не виден, — но приёмник
    держат ровно ради того дня, когда его переведут на нас.
    """

    @pytest.mark.asyncio
    async def test_notification_is_queued_before_the_commit(self, monkeypatch):
        import datetime as dt
        from decimal import Decimal

        from api.routes import webhooks
        from core.config import settings
        from core.enums import PaymentProviderName, PaymentPurpose, PaymentStatus
        from core.models import Payment

        monkeypatch.setattr(settings.platega, "allowed_ips", [])

        payment = Payment(
            id=3,
            user_id=1,
            provider=PaymentProviderName.PLATEGA,
            purpose=PaymentPurpose.ORDER,
            status=PaymentStatus.SUCCEEDED,
            idempotency_key="key-3",
            amount=Decimal("9299.00"),
            currency="RUB",
            created_at=dt.datetime.now(dt.UTC),
        )

        steps: list[str] = []

        class _Session:
            def add(self, _item):
                steps.append("queued")

            async def commit(self):
                steps.append("commit")

        class _Provider:
            def verify_webhook(self, _headers, _body):
                return True

        async def _handle(_session, *, provider_name, data):
            return payment, True

        async def _notify(session, _payment):
            session.add(object())

        class _Request:
            def __init__(self):
                self.headers = {"Content-Type": "application/json"}
                self.client = SimpleNamespace(host="127.0.0.1")

            async def body(self):
                return b'{"transactionId": "x"}'

        monkeypatch.setattr(webhooks, "get_provider", lambda _name: _Provider())
        monkeypatch.setattr(webhooks.payment_service, "handle_webhook", _handle)
        monkeypatch.setattr(webhooks, "notify_payment_result", _notify)

        session = _Session()
        response = await webhooks.platega_webhook(_Request(), session)

        assert response.status_code == 200
        assert steps, "обработчик ничего не сделал"
        assert steps[-1] == "commit", (
            "сообщение клиенту кладётся после коммита и пропадает вместе с сессией"
        )
