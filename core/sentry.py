"""Sentry: включается только если задан SENTRY_DSN."""

from __future__ import annotations

from typing import Any

import structlog

from core.config import settings

log = structlog.get_logger("sentry")


def _scrub(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
    """Не отправляем в Sentry токены и телефоны."""
    request = event.get("request") or {}
    headers = request.get("headers")
    if isinstance(headers, dict):
        for key in list(headers):
            if key.lower() in {"authorization", "x-signature", "cookie", "x-telegram-bot-api-secret-token"}:
                headers[key] = "[filtered]"
    return event


def init_sentry(service: str) -> None:
    if not settings.sentry.enabled or not settings.sentry.dsn:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry.dsn,
        environment=settings.app.env,
        traces_sample_rate=settings.sentry.traces_sample_rate,
        send_default_pii=False,
        before_send=_scrub,
        release="router-shop@1.0.0",
    )
    sentry_sdk.set_tag("service", service)
    log.info("sentry.enabled", service=service)
