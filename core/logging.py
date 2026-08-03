"""Единая настройка логирования: structlog поверх stdlib logging.

В проде — JSON в stdout (docker logging driver забирает), в dev — читаемая консоль.
Чувствительные данные (токены, телефоны, секреты) маскируются процессором,
поэтому даже случайный log.info(payload) не утечёт в логи.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from core.config import settings

_SENSITIVE_KEYS = {
    "token",
    "bot_token",
    "secret",
    "device_secret",
    "secret_key",
    "encryption_key",
    "password",
    "authorization",
    "x-signature",
    "signature",
    "sub_url",
    "sub_token",
    "totp_secret",
    "api_key",
    "shop_secret",
}
_PHONE_RE = re.compile(r"(?<!\d)(\+?7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_BOT_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b")


def mask_secret(value: str, keep: int = 4) -> str:
    """`abcdef123456` -> `abcd…3456` — достаточно для отладки, бесполезно для кражи."""
    if len(value) <= keep * 2:
        return "***"
    return f"{value[:keep]}…{value[-keep:]}"


def mask_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) < 7:
        return "***"
    return f"{digits[:1]}***{digits[-4:]}"


def _mask_value(key: str, value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _mask_value(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_mask_value(key, item) for item in value)
    if not isinstance(value, str):
        return value
    if key.lower() in _SENSITIVE_KEYS:
        return mask_secret(value)
    if key.lower() in {"phone", "customer_phone", "recipient_phone"}:
        return mask_phone(value)
    masked = _BOT_TOKEN_RE.sub(lambda m: mask_secret(m.group(0)), value)
    return _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), masked)


def mask_processor(_logger: Any, _name: str, event_dict: EventDict) -> EventDict:
    return {key: _mask_value(key, value) for key, value in event_dict.items()}


def add_service_name(service: str) -> Processor:
    def processor(_logger: Any, _name: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        event_dict.setdefault("env", settings.app.env)
        return event_dict

    return processor


def configure_logging(service: str) -> None:
    """Вызывается один раз при старте процесса (bot/api/worker)."""
    level = getattr(logging, settings.log.level)
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        add_service_name(service),
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        mask_processor,
    ]

    renderer: Processor
    if settings.log.format == "json":
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Шумные логгеры библиотек приглушаем, но не глушим совсем.
    for name, lvl in {
        "aiogram.event": logging.WARNING,
        "aiohttp.access": logging.WARNING,
        "uvicorn.access": logging.WARNING,
        "uvicorn.error": logging.INFO,
        "sqlalchemy.engine": logging.INFO if settings.log.sql_echo else logging.WARNING,
        "apscheduler.executors.default": logging.WARNING,
        "httpx": logging.WARNING,
    }.items():
        logging.getLogger(name).setLevel(lvl)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[return-value]
