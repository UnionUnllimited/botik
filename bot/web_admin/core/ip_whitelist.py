"""
IP-whitelist для веб-админки.

Хранение в таблице ``settings`` (без миграции схемы):
  • ``admin_ip_whitelist``          — строка через запятую (например, ``"1.2.3.4, 5.6.7.0/24, 2001:db8::/32"``)
  • ``admin_ip_whitelist_enabled``  — ``"0"`` / ``"1"``

Loopback (127.0.0.0/8, ::1/128) **всегда** разрешён — иначе systemd / curl
к /api/system_stats со стороны самого хоста, manual_backup и health-check
перестанут работать. Это и резервный bypass: если случайно отрезали себе
доступ, заходим по SSH и:

    sqlite3 router_bot.db "UPDATE settings SET value='0' WHERE key='admin_ip_whitelist_enabled'"

Реальный IP клиента берём из заголовков, **только** если запрос пришёл с
доверенного прокси (по умолчанию loopback). Так обратный прокси
(``proxy_set_header X-Real-IP $remote_addr``) даёт нам правильный исходный IP,
а сторонний клиент не сможет подсунуть свой ``X-Forwarded-For``.
"""
from __future__ import annotations

import ipaddress
import logging
import time
from typing import Iterable

# Quart / Flask request — типизация не критична, импорт ленивый внутри resolve_client_ip.

# ─── Типы для удобства ────────────────────────────────────────────────────
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


# Доверенные прокси по умолчанию: только loopback.
# Если когда-нибудь поставят cloudflare/любой ещё прокси перед nginx —
# расширим конфигурируемым ключом, пока хватит хардкода.
_DEFAULT_TRUSTED_PROXIES: tuple[IpNetwork, ...] = (
    ipaddress.ip_network('127.0.0.0/8'),
    ipaddress.ip_network('::1/128'),
)

# in-memory кэш правил (TTL 30 сек — баланс между точностью и нагрузкой на SQLite).
_CACHE_TTL_SEC = 30.0
_cache: dict[str, object] = {
    'fetched_at': 0.0,
    'enabled': False,
    'networks': [],
    'raw': '',
}


logger = logging.getLogger(__name__)


# ─── Парсинг CIDR / IP ────────────────────────────────────────────────────
def parse_cidr_list(text: str | None) -> tuple[list[IpNetwork], list[str]]:
    """
    Принимает строку вида ``"1.2.3.4, 5.6.7.0/24, 2001:db8::/32"`` и возвращает
    кортеж ``(networks, errors)`` — отдельный сетевой объект для каждого
    валидного фрагмента и список нераспознанных строк (для UI-фидбека).
    """
    if not text:
        return [], []
    networks: list[IpNetwork] = []
    errors: list[str] = []
    for raw in text.replace(';', ',').replace('\n', ',').split(','):
        token = raw.strip()
        if not token:
            continue
        try:
            # одиночный IP без маски → /32 или /128
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            errors.append(token)
    return networks, errors


# ─── Резолв клиентского IP ────────────────────────────────────────────────
def _is_trusted_proxy(addr: IpAddress, trusted: Iterable[IpNetwork]) -> bool:
    return any(addr in net for net in trusted)


def _safe_ip(value: str | None) -> IpAddress | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    # IPv6 в зоне без brackets валиден; ipv6 со скобками (как в Forwarded RFC) — нет.
    if value.startswith('[') and value.endswith(']'):
        value = value[1:-1]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def resolve_client_ip(request, trusted: Iterable[IpNetwork] | None = None) -> IpAddress | None:
    """
    Достаёт реальный IP клиента, защищаясь от подмены заголовков.

    Сценарий деплоя — за обратным прокси, который ставит заголовки:

        proxy_set_header X-Real-IP        $remote_addr;
        proxy_set_header X-Forwarded-For  $proxy_add_x_forwarded_for;

    Особенности:
      • ``X-Real-IP`` nginx **полностью перезаписывает** на свой $remote_addr —
        подделать клиентом нельзя.
      • ``X-Forwarded-For`` nginx **дописывает** $remote_addr в конец —
        клиент мог пожаловать своё значение в самом начале заголовка.
        Поэтому **самый правый** не-trusted IP в X-Forwarded-For — реальный.

    Алгоритм:
      1. peer = ``request.remote_addr`` (TCP-источник).
      2. Если peer **не доверенный** (не loopback) — возвращаем peer.
         Ничему другому не доверяем: подмена X-Forwarded-For/X-Real-IP в обход
         nginx бесполезна, мы всё равно используем peer.
      3. Если peer доверенный (запрос пришёл от nginx по 127.0.0.1):
            а) ``X-Real-IP`` (nginx переписал — доверяем);
            б) самый **правый** не-trusted IP из ``X-Forwarded-For``
               (nginx дописал в конец — это и есть реальный клиент);
            в) фолбэк — peer.
    """
    if trusted is None:
        trusted = _DEFAULT_TRUSTED_PROXIES

    peer = _safe_ip(getattr(request, 'remote_addr', None))
    if peer is None:
        # На совсем странном раннере remote_addr может быть None — значит,
        # запрос пришёл явно не из публичной сети; считаем его loopback.
        return None
    if not _is_trusted_proxy(peer, trusted):
        # Запрос пришёл МИМО nginx (или с другого недоверенного прокси) —
        # заголовки X-* безусловно игнорируем. Никаких подмен.
        return peer

    # X-Real-IP — приоритет, nginx его перезаписывает на $remote_addr.
    real_ip = _safe_ip(request.headers.get('X-Real-IP') or request.headers.get('X-Real-Ip'))
    if real_ip is not None:
        return real_ip

    # X-Forwarded-For: ``<подмена_клиентом>, ..., <peer_от_nginx>``.
    # Идём СПРАВА НАЛЕВО, ищем самый правый не-trusted IP. Всё, что левее,
    # клиент мог написать сам — игнорируем.
    fwd = request.headers.get('X-Forwarded-For') or request.headers.get('X-Forwarded-FOR') or ''
    for raw in reversed(fwd.split(',')):
        ip = _safe_ip(raw)
        if ip is None:
            continue
        if not _is_trusted_proxy(ip, trusted):
            return ip

    return peer


# ─── Проверка ─────────────────────────────────────────────────────────────
def is_ip_allowed(ip: IpAddress | None, networks: Iterable[IpNetwork]) -> bool:
    """
    True, если IP попадает хотя бы в одну сеть. None — считаем «не-публичный»
    (см. resolve_client_ip), пускаем — это либо loopback, либо unix-socket.
    """
    if ip is None:
        return True
    return any(ip in net for net in networks)


def is_loopback(ip: IpAddress | None) -> bool:
    """Возвращает True для 127.0.0.0/8 и ::1/128."""
    if ip is None:
        return True
    return ip.is_loopback


# ─── Кэш правил из БД ─────────────────────────────────────────────────────
async def get_whitelist_state(force: bool = False) -> tuple[bool, list[IpNetwork], str]:
    """
    Возвращает (enabled, networks, raw_text). Кэширует результат на ``_CACHE_TTL_SEC``.
    Импортирует ``async_query_db`` лениво, чтобы не тащить зависимость на старте
    модуля (и не ломать тесты, где БД может быть не готова).
    """
    now = time.monotonic()
    if not force and (now - float(_cache.get('fetched_at') or 0)) < _CACHE_TTL_SEC:
        return (
            bool(_cache['enabled']),
            list(_cache['networks']),  # type: ignore[arg-type]
            str(_cache['raw'] or ''),
        )

    enabled = False
    raw = ''
    try:
        from web_admin.async_db import async_query_db  # local import — см. docstring
        en_row = await async_query_db(
            "SELECT value FROM settings WHERE key = 'admin_ip_whitelist_enabled'", (), one=True,
        )
        wl_row = await async_query_db(
            "SELECT value FROM settings WHERE key = 'admin_ip_whitelist'", (), one=True,
        )
        enabled = bool(en_row and (en_row.get('value') or '').strip() == '1')
        raw = (wl_row.get('value') if wl_row else '') or ''
    except Exception as exc:
        logger.warning('[WHITELIST] cannot load from settings: %s', exc)

    networks, _errors = parse_cidr_list(raw)
    _cache['fetched_at'] = now
    _cache['enabled'] = enabled
    _cache['networks'] = networks
    _cache['raw'] = raw
    return enabled, networks, raw


def invalidate_cache() -> None:
    """Сбросить кэш — вызывается из POST-обработчика settings_general после save."""
    _cache['fetched_at'] = 0.0
