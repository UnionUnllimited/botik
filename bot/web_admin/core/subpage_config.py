"""Конфигурация Subpage, хранящаяся в таблице settings одним JSON.

Архитектура:
- Один ключ в settings: SETTING_KEY = 'subpage_config'.
- Внутри — три блока: main / links / headers / stubs.
- Веб-админка читает/пишет через load_config() и save_config().
- xuiweb endpoint /api/settings/info отдаёт ровно этот dict (см. xuiweb/run.py).
- Subpage при старте дёргает /api/settings/info и оверрайдит свои переменные.
  Если поле пустое или API не отвечает — Subpage падает на .env (фолбэк).

Все значения — простые JSON-типы (str / bool / int / list[str] / dict[str,str]).
Никаких чувствительных данных тут не должно быть (endpoint публичный).
"""
from __future__ import annotations

import json
import re
from typing import Any

from web_admin.async_db import async_query_db, async_execute_db


SETTING_KEY = 'subpage_config'
# Решение «использовать ли конфиг из админки» принимает сам subpage
# по флагу API_CUSTOM_SETTINGS в его .env. На уровне БД флага больше нет —
# xuiweb всегда отдаёт текущий конфиг.

# Темы страницы подписки. Совпадает с SUBSCRIPTION_PATH_THEMES в subpage,
# плюс отдельная «по умолчанию» (subscription).
ALLOWED_THEMES: tuple[str, ...] = ('subscription', 'happcrypt', 'subscription_custom')

# Известные User-Agent VPN-приложений (подстроки, lowercase).
# Используем их как пресеты в multi-select; админ может добавить и свои.
KNOWN_APP_USER_AGENTS: tuple[tuple[str, str], ...] = (
    ('happ',          'Happ'),
    ('incy',          'INCY'),
    ('clash',         'Clash / ClashX / Clash for Windows'),
    ('clash-meta',    'Clash Meta'),
    ('clash-verge',   'Clash Verge'),
    ('mihomo',        'mihomo'),
    ('hiddify',       'Hiddify / Hiddify Next'),
    ('shadowrocket',  'Shadowrocket (iOS)'),
    ('shadowsocks',   'Shadowsocks'),
    ('sing-box',      'sing-box'),
    ('v2ray',         'V2Ray'),
    ('v2rayn',        'V2RayN (Windows)'),
    ('v2rayng',       'V2RayNG (Android)'),
    ('v2raytun',      'V2RayTun'),
    ('v2box',         'V2Box (iOS)'),
    ('nekoray',       'Nekoray'),
    ('nekobox',       'Nekobox'),
    ('streisand',     'Streisand'),
    ('foxray',        'FoxRay'),
)


def _default_links() -> dict[str, str]:
    """Дефолты ссылок — те же, что зашиты в subpage/run.py."""
    return {
        'LINK_IOS':              'https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973',
        'LINK_IOS_GLOBAL':       'https://apps.apple.com/us/app/happ-proxy-utility/id6504287215',
        'LINK_ANDROID':          'https://play.google.com/store/apps/details?id=com.happproxy',
        'LINK_ANDROID_APK':      'https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk',
        'LINK_WINDOWS':          'https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe',
        'LINK_MAC':              'https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973',
        'LINK_LINUX':            'https://github.com/Happ-proxy/happ-desktop/releases/',
        'LINK_INCY_IOS':         'https://apps.apple.com/us/app/incy/id6756943388',
        'LINK_INCY_ANDROID':     'https://play.google.com/store/apps/details?id=llc.itdev.incy',
        'LINK_INCY_ANDROID_APK': 'https://github.com/INCY-DEV/incy-platforms/releases/latest/download/Incy.apk',
        'LINK_INCY_WINDOWS':     'https://github.com/INCY-DEV/incy-platforms/releases/latest/download/incy-windows-setup.exe',
        'LINK_INCY_MAC':         'https://github.com/INCY-DEV/incy-platforms',
        'LINK_INCY_LINUX':       'https://github.com/INCY-DEV/incy-platforms',
    }


# Список ключей ссылок в каноническом порядке (используется и UI, и валидатором).
LINK_KEYS: tuple[str, ...] = tuple(_default_links().keys())


def default_config() -> dict[str, Any]:
    """Полный дефолтный конфиг. Используется при первом открытии админки."""
    return {
        'main': {
            'PROJECT_NAME':           '',
            'SUPPORT_URL':            '',
            'WEBSITE_URL':            '',
            'ANNOUNCE_TEXT':          '',
            'ENABLE_COPY_KEYS':       False,
            'UPDATE_INTERVAL_HOURS':  1,
            'SUBSCRIPTION_THEME':     'subscription',
            'ALLOWED_APP_USER_AGENTS': [],
            'REQUIRE_HWID':           False,
            # Фильтр ссылок по меткам [PC]/[MOBILE]/[ROUTER]/[TV] в названии (fragment).
            # Subpage: только по X-Device-OS; без заголовка — все сервера (выкл по умолчанию).
            'PLATFORM_FILTER_ENABLED': False,
        },
        'links':   _default_links(),
        'headers': {},
        # Кастомные тексты заглушек. Логика разная:
        #   • HWID / UA   → отдаются subpage'ом одной base64-vless-строкой,
        #                   из списка выбирается СЛУЧАЙНАЯ фраза
        #                   (защита от детектирования по фиксированному тексту);
        #   • BLOCKED     → отдаётся xuiweb в JSON подписки (links: [...]),
        #                   ВСЕ фразы списка показываются как отдельные ссылки;
        #   • DEVICE_LIMIT → то же что BLOCKED, но для превышения HWID-лимита;
        #   • EXPIRED      → как DEVICE_LIMIT для истёкшей подписки, но только для HWID,
        #                   уже учтённых в лимите; новые сверх лимита получают DEVICE_LIMIT.
        #                   Каждый элемент — текст или готовая ссылка vless/vmess/… .
        'stubs': {
            'HWID':         [],
            'UA':           [],
            'BLOCKED':      [],
            'DEVICE_LIMIT': [],
            'EXPIRED':      [],
        },
        'info': {
            'enabled': True,
            'sections': {
                'subscription': True,
                'devices': True,
                'referral': True,
                'share': True,
                'renew': True,
                'faq': True,
            },
            'texts': {
                'page_title': 'О подписке',
                'share_hint': (
                    'Скопируйте ссылку и откройте её на другом устройстве — '
                    'там будет инструкция по подключению.'
                ),
                'renew_label_site': 'Продлить на сайте',
                'renew_label_tg': 'Продлить в Telegram',
                'devices_empty': 'Устройства появятся после первого подключения VPN-клиента.',
                'referral_hint': (
                    'Приглашайте друзей — получайте бесплатные дни подписки! '
                    'Отправьте ссылку ниже, бонусы начисляются автоматически.'
                ),
                'last_update_label': 'Обновлялась подписка',
            },
            'referral': {
                'link_bot': '',
                'link_site': '',
            },
            'renew': {
                'site_path': '/cabinet',
                'bot_url': '',
            },
            'faq_items': [],
        },
    }


# ─────────────────────────────────────────────────────────────────────────
# Загрузка / сохранение
# ─────────────────────────────────────────────────────────────────────────

async def load_config() -> dict[str, Any]:
    """Возвращает текущий конфиг, объединённый с дефолтами (на случай если
    в БД не хватает каких-то ключей после обновления приложения).
    """
    row = await async_query_db(
        "SELECT value FROM settings WHERE key = ?", (SETTING_KEY,), one=True,
    )
    raw: dict[str, Any] = {}
    if row and row.get('value'):
        try:
            parsed = json.loads(row['value'])
            if isinstance(parsed, dict):
                raw = parsed
        except (ValueError, TypeError):
            raw = {}
    return _merge_with_defaults(raw)


async def save_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Валидирует пришедший конфиг, сохраняет одним JSON и возвращает финальный."""
    cfg = _validate(payload)
    serialized = json.dumps(cfg, ensure_ascii=False)
    # UPSERT в стиле SQLite (подставляем INSERT OR REPLACE через две операции).
    existing = await async_query_db(
        "SELECT 1 FROM settings WHERE key = ?", (SETTING_KEY,), one=True,
    )
    if existing:
        await async_execute_db(
            "UPDATE settings SET value = ? WHERE key = ?",
            (serialized, SETTING_KEY),
        )
    else:
        await async_execute_db(
            "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
            (
                SETTING_KEY, serialized,
                'JSON-конфиг страницы подписки (subpage), редактируется через /settings/subpage',
            ),
        )
    return cfg


# ─────────────────────────────────────────────────────────────────────────
# Внутренние утилиты
# ─────────────────────────────────────────────────────────────────────────

def _merge_with_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    base = default_config()
    if not isinstance(raw, dict):
        return base
    for section in ('main', 'links', 'headers', 'stubs', 'info'):
        v = raw.get(section)
        if isinstance(v, dict):
            if section == 'info':
                _merge_info_section(base['info'], v)
            else:
                base[section].update({k: v[k] for k in v if k in base[section] or section in ('headers',)})
    # Заглушки: должны быть list[str].
    for k in ('HWID', 'UA', 'BLOCKED', 'DEVICE_LIMIT', 'EXPIRED'):
        v = raw.get('stubs', {}).get(k) if isinstance(raw.get('stubs'), dict) else None
        if isinstance(v, list):
            base['stubs'][k] = [str(x) for x in v if str(x).strip()]
    # ALLOWED_APP_USER_AGENTS: list[str].
    main_in = raw.get('main') if isinstance(raw.get('main'), dict) else {}
    if isinstance(main_in.get('ALLOWED_APP_USER_AGENTS'), list):
        base['main']['ALLOWED_APP_USER_AGENTS'] = [
            str(x).strip().lower() for x in main_in['ALLOWED_APP_USER_AGENTS'] if str(x).strip()
        ]
    return base


_HEADER_NAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')
_FAQ_ALLOWED_TAGS = frozenset(('p', 'b', 'strong', 'i', 'em', 'ul', 'ol', 'li', 'br', 'a'))
_FAQ_STRIP_RE = re.compile(r'<(script|style)[^>]*>.*?</\1>', re.IGNORECASE | re.DOTALL)


def _merge_info_section(base: dict[str, Any], incoming: dict[str, Any]) -> None:
    if isinstance(incoming.get('enabled'), bool):
        base['enabled'] = incoming['enabled']
    sec_in = incoming.get('sections')
    if isinstance(sec_in, dict):
        for k in base['sections']:
            if k in sec_in:
                base['sections'][k] = bool(sec_in[k])
    texts_in = incoming.get('texts')
    if isinstance(texts_in, dict):
        for k in base['texts']:
            if k in texts_in:
                base['texts'][k] = str(texts_in[k] or '')
    ref_in = incoming.get('referral')
    if isinstance(ref_in, dict):
        base['referral']['link_bot'] = str(
            ref_in.get('link_bot') or ref_in.get('link_bot_template') or base['referral']['link_bot']
        ).strip()
        base['referral']['link_site'] = str(
            ref_in.get('link_site') or ref_in.get('link_site_template') or base['referral']['link_site']
        ).strip()
    renew_in = incoming.get('renew')
    if isinstance(renew_in, dict):
        if 'site_path' in renew_in:
            base['renew']['site_path'] = str(renew_in.get('site_path') or base['renew']['site_path']).strip()
        base['renew']['bot_url'] = str(
            renew_in.get('bot_url') or renew_in.get('bot_url_template') or base['renew']['bot_url']
        ).strip()
    faq_in = incoming.get('faq_items')
    if isinstance(faq_in, list):
        base['faq_items'] = [_sanitize_faq_item(x) for x in faq_in if isinstance(x, dict)]


def _sanitize_faq_item(raw: dict[str, Any]) -> dict[str, Any]:
    item_id = str(raw.get('id') or '').strip() or f'faq-{abs(hash(str(raw))) % 10**8}'
    html_raw = str(raw.get('html') or '').strip()
    html_raw = _FAQ_STRIP_RE.sub('', html_raw)
    return {
        'id': item_id[:64],
        'enabled': bool(raw.get('enabled', True)),
        'title': str(raw.get('title') or '').strip()[:200],
        'heading': str(raw.get('heading') or '').strip()[:200],
        'image': str(raw.get('image') or '').strip()[:500],
        'html': html_raw[:8000],
    }


def _validate_info_section(info_in: dict[str, Any]) -> dict[str, Any]:
    base = default_config()['info']
    if not isinstance(info_in, dict):
        return base
    base['enabled'] = bool(info_in.get('enabled', True))
    sec_in = info_in.get('sections') if isinstance(info_in.get('sections'), dict) else {}
    for k in base['sections']:
        base['sections'][k] = bool(sec_in.get(k, base['sections'][k]))
    texts_in = info_in.get('texts') if isinstance(info_in.get('texts'), dict) else {}
    for k in base['texts']:
        base['texts'][k] = str(texts_in.get(k) or base['texts'][k]).strip()[:500]
    ref_in = info_in.get('referral') if isinstance(info_in.get('referral'), dict) else {}
    base['referral']['link_bot'] = str(
        ref_in.get('link_bot') or ref_in.get('link_bot_template') or base['referral']['link_bot']
    ).strip()[:500]
    base['referral']['link_site'] = str(
        ref_in.get('link_site') or ref_in.get('link_site_template') or base['referral']['link_site']
    ).strip()[:500]
    renew_in = info_in.get('renew') if isinstance(info_in.get('renew'), dict) else {}
    _site_renew = str(renew_in.get('site_path') or '/cabinet').strip()[:200]
    # Допускаем как полную ссылку (https://сайт/cabinet), так и относительный путь
    # (/cabinet — подставится к WEBSITE_URL). Слэш добавляем только для пути.
    if _site_renew and not _site_renew.startswith(('/', 'http://', 'https://')):
        _site_renew = '/' + _site_renew
    base['renew']['site_path'] = _site_renew
    base['renew']['bot_url'] = str(
        renew_in.get('bot_url') or renew_in.get('bot_url_template') or base['renew']['bot_url']
    ).strip()[:500]
    faq_in = info_in.get('faq_items')
    if isinstance(faq_in, list):
        base['faq_items'] = [_sanitize_faq_item(x) for x in faq_in if isinstance(x, dict)]
    return base


def normalize_header_name(name: str) -> str:
    """Нормализует имя HTTP-заголовка под формат, который subpage ждёт в .env:
    `_` → `-`, lowercase, обрезаем дефисы по краям.
    """
    cleaned = (name or '').strip().replace('_', '-').lower().strip('-')
    return cleaned


def _validate(raw: dict[str, Any]) -> dict[str, Any]:
    """Аккуратно типизирует и санитизирует входящий dict."""
    cfg = default_config()
    if not isinstance(raw, dict):
        return cfg

    main_in = raw.get('main') if isinstance(raw.get('main'), dict) else {}
    main = cfg['main']

    main['PROJECT_NAME']   = str(main_in.get('PROJECT_NAME')   or '').strip()
    main['SUPPORT_URL']    = str(main_in.get('SUPPORT_URL')    or '').strip()
    main['WEBSITE_URL']    = str(main_in.get('WEBSITE_URL')    or '').strip()
    # ANNOUNCE_TEXT: автосжатие пробелов (2+ → 1), strip, hard-лимит 200 символов.
    # Плейсхолдеры [Email], [TelegramID], [UUID], [REGTYPE] подставляет subpage при ответе подписки.
    announce_raw = str(main_in.get('ANNOUNCE_TEXT') or '').strip()
    announce_norm = re.sub(r'[ \t]{2,}', ' ', announce_raw)[:200]
    main['ANNOUNCE_TEXT']  = announce_norm

    main['ENABLE_COPY_KEYS'] = bool(main_in.get('ENABLE_COPY_KEYS'))
    main['REQUIRE_HWID']     = bool(main_in.get('REQUIRE_HWID'))
    main['PLATFORM_FILTER_ENABLED'] = bool(main_in.get('PLATFORM_FILTER_ENABLED'))

    try:
        hours = int(main_in.get('UPDATE_INTERVAL_HOURS') or 1)
    except (ValueError, TypeError):
        hours = 1
    main['UPDATE_INTERVAL_HOURS'] = max(1, min(hours, 24 * 7))

    theme = str(main_in.get('SUBSCRIPTION_THEME') or 'subscription').strip().lower()
    if theme not in ALLOWED_THEMES:
        theme = 'subscription'
    main['SUBSCRIPTION_THEME'] = theme

    ua_raw = main_in.get('ALLOWED_APP_USER_AGENTS') or []
    if isinstance(ua_raw, str):
        ua_raw = [p for p in ua_raw.split(',')]
    main['ALLOWED_APP_USER_AGENTS'] = sorted({
        s.strip().lower() for s in ua_raw if isinstance(s, (str, int)) and str(s).strip()
    })

    # Ссылки.
    links_in = raw.get('links') if isinstance(raw.get('links'), dict) else {}
    for k in LINK_KEYS:
        cfg['links'][k] = str(links_in.get(k) or '').strip()

    # Заголовки. Нормализуем имена, отфильтровываем мусор.
    headers_in = raw.get('headers') if isinstance(raw.get('headers'), dict) else {}
    cleaned_headers: dict[str, str] = {}
    for raw_name, raw_value in headers_in.items():
        name = normalize_header_name(str(raw_name))
        if not name or not _HEADER_NAME_RE.match(name):
            continue
        if not str(raw_value or '').strip():
            continue
        cleaned_headers[name] = str(raw_value).strip()
    cfg['headers'] = cleaned_headers

    # Заглушки.
    stubs_in = raw.get('stubs') if isinstance(raw.get('stubs'), dict) else {}
    for k in ('HWID', 'UA', 'BLOCKED', 'DEVICE_LIMIT', 'EXPIRED'):
        v = stubs_in.get(k) or []
        if isinstance(v, str):
            v = [v]
        cfg['stubs'][k] = [str(x).strip() for x in v if str(x).strip()]

    cfg['info'] = _validate_info_section(
        raw.get('info') if isinstance(raw.get('info'), dict) else {}
    )

    return cfg


__all__ = [
    'SETTING_KEY',
    'ALLOWED_THEMES',
    'KNOWN_APP_USER_AGENTS',
    'LINK_KEYS',
    'default_config',
    'load_config',
    'save_config',
    'normalize_header_name',
]
