"""
Единая сборка aiogram Bot с опциональным прокси до api.telegram.org.

Прокси: HTTP(S) или SOCKS (через aiohttp-socks), строка URL как для curl,
например http://user:pass@host:8080, socks5://127.0.0.1:1080
Пустая строка в настройках — прямое подключение.

Таймаут HTTP: переменная окружения TELEGRAM_HTTP_TIMEOUT (секунды), по умолчанию 120
(через прокси long polling может быть медленнее дефолтных 60 с в aiogram).
"""
from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlsplit

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

_logger = logging.getLogger(__name__)


def normalize_telegram_proxy_url(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _telegram_http_timeout_sec() -> float:
    try:
        return float(os.getenv('TELEGRAM_HTTP_TIMEOUT', '120'))
    except ValueError:
        return 120.0


def _proxy_log_hint(proxy_url: str) -> str:
    try:
        u = urlsplit(proxy_url)
        host = u.hostname or '?'
        if u.port:
            return f'{u.scheme}://{host}:{u.port}'
        return f'{u.scheme}://{host}'
    except Exception:
        return 'proxy'


def make_aiogram_bot(
    token: str,
    proxy_url: Optional[str] = None,
    *,
    parse_mode=ParseMode.HTML,
) -> Bot:
    proxy_url = normalize_telegram_proxy_url(proxy_url)
    timeout = _telegram_http_timeout_sec()
    if proxy_url:
        session = AiohttpSession(proxy=proxy_url, timeout=timeout)
        _logger.info(
            'Telegram API client: proxy=%s, timeout=%ss (TELEGRAM_HTTP_TIMEOUT)',
            _proxy_log_hint(proxy_url),
            timeout,
        )
    else:
        session = AiohttpSession(timeout=timeout)
        _logger.info('Telegram API client: direct, timeout=%ss', timeout)
    return Bot(
        token=token,
        session=session,
        default=DefaultBotProperties(parse_mode=parse_mode),
    )
