"""Реестр стилизуемых inline-кнопок бота.

Каждая запись описывает одну логическую кнопку: ключ настройки (используется
как `app_conf.get(key, ...)` для текста), группу, читаемый ярлык, и дефолтные
text/style/icon, которые применяются если в БД для соответствующего ключа
ещё ничего не сохранено.

Хранение в БД (таблица `settings`):
- `<key>`            — текст кнопки (ровно как было раньше)
- `<key>__style`     — стиль: '' | 'primary' | 'success' | 'danger'
- `<key>__icon`      — `icon_custom_emoji_id` (Telegram Premium custom emoji)

Сборка кнопки происходит через `button_helpers.btn(key, ...)`.
"""

from typing import Any

# Допустимые значения стилей. Пустая строка = «без стиля» (обычная серая кнопка).
ALLOWED_STYLES = ('', 'primary', 'success', 'danger')

# Допустимые значения «kind» — режим открытия для кнопок группы «Подключение».
# 'url'    — открыть ссылку в браузере;
# 'webapp' — открыть Telegram WebApp поверх чата.
ALLOWED_KINDS = ('url', 'webapp')

# Кнопки, у которых поддерживается переключение url/webapp.
# Группа «Подключение» вырезана вместе с кнопками: она отдавала конфиг
# приложению на телефоне, а роутер настраивается сам по MAC при отгрузке.
KIND_AWARE_KEYS: tuple[str, ...] = ()

# Кнопки, у которых поддерживается отдельный тумблер видимости
# (помимо тумблера всей группы). Хранится в settings под ключом `<key>__enabled`.
# Значение '0' = скрыто; '1' (или нет ключа) = показывается.
PER_BUTTON_TOGGLE_KEYS: tuple[str, ...] = ()

# Группа «Подключение» имеет один общий тумблер «включена / выключена»,
# который скрывает весь ряд iPhone/Android/Connect.
GROUP_TOGGLE_KEYS: dict[str, str] = {}
"""group_id -> ключ настройки ('0' = группа отключена)."""

# Описание групп для UI админки.
BUTTON_GROUPS = [
    ('main_menu', '🏠 Главное меню'),
    ('shop',      '🛒 Каталог роутеров'),
    ('payment',   '💳 Оплата'),
    ('service',   'ℹ️ Сервис и навигация'),
    ('referral',  '🎁 Рефералы и партнёры'),
    ('renew',     '🔄 Продление (CloudTips)'),
]


def _b(key: str, group: str, label: str, default_text: str,
       default_style: str = '', default_icon: str = '',
       default_kind: str = '') -> dict:
    return {
        'key': key,
        'group': group,
        'label': label,
        'default_text': default_text,
        'default_style': default_style,
        'default_icon': default_icon,
        'default_kind': default_kind,
    }


# --- Реестр кнопок -----------------------------------------------------------
BUTTON_REGISTRY: list[dict[str, Any]] = [

    # 🏠 Главное меню
    _b('btn_renew_sub',       'main_menu', 'Продлить подписку',         '🔄 Продлить',                ''),
    _b('btn_free_renew',      'main_menu', 'Продлить бесплатно (newsletter)', '🆓 Продлить подписку бесплатно', ''),
    _b('btn_traffic_renewal', 'main_menu', 'Докупить гигабайты',        '📈 Докупить гигабайты',      ''),
    _b('btn_website_access',     'main_menu', 'Личный кабинет (главное меню)', '🌐 Личный кабинет',         ''),
    _b('btn_website_open',       'main_menu', 'Открыть личный кабинет (magic link)', '🌐 Открыть личный кабинет', ''),
    _b('btn_website_link_email', 'main_menu', 'Привязать email (в кабинете)',  '📧 Привязать email',        ''),
    _b('btn_support',         'main_menu', 'Поддержка',                 '💬 Поддержка',               ''),
    _b('btn_about_service',   'main_menu', 'О сервисе',                 'ℹ️ О сервисе',               ''),
    _b('btn_bot_custom_url',  'main_menu', 'КастомURL',                 '🔗 КастомURL',               ''),
    _b('btn_bot_channel',     'main_menu', 'Наш канал',                 '📣 Наш канал',               ''),

    # 🛒 Каталог роутеров (товар, а не подписка: продаётся железо)
    # Кнопки со сборным текстом — модель с ценой, номер заказа — в реестр
    # не вносятся: их подпись собирается из данных каталога, и правка
    # в админке всё равно ни на что не влияла бы.
    _b('btn_catalog',              'shop', 'Каталог роутеров (главное меню)', '🛒 Купить роутер',      ''),
    _b('btn_my_router',            'shop', 'Мой роутер (главное меню)',       '📡 Мой роутер',         ''),
    _b('btn_my_orders',            'shop', 'Мои заказы (главное меню)',       '📦 Мои заказы',         ''),
    _b('btn_my_router_refresh',    'shop', 'Обновить показания роутера',      '🔄 Обновить',           ''),
    _b('btn_my_router_switch',     'shop', 'Переключиться на другой роутер',  '📡 Другой роутер',      ''),
    _b('btn_router_panel',         'shop', 'Админка роутера (домашняя сеть)', '⚙️ Перейти в админку',  ''),
    _b('btn_router_instruction',   'shop', 'Инструкция на роутере',           '📘 Инструкция',         ''),
    _b('btn_shop_buy',             'shop', 'Заказать модель',                 '🛒 Заказать',           ''),
    _b('btn_shop_back_to_list',    'shop', 'Назад к списку моделей',          '⬅️ К списку моделей',   ''),
    _b('btn_shop_cancel_order',    'shop', 'Отменить оформление',             '✖️ Отменить',           ''),
    _b('btn_shop_speed',           'shop', 'Вариант скорости доставки',       '🚚 Доставка',           ''),
    _b('btn_shop_to_pvz',          'shop', 'Доставка в пункт выдачи',         '🏬 В пункт выдачи',     ''),
    _b('btn_shop_to_door',         'shop', 'Доставка курьером',               '🚪 Курьером до двери',  ''),
    _b('btn_shop_back_to_speed',   'shop', 'Назад к выбору доставки',         '⬅️ К выбору доставки',  ''),
    _b('btn_shop_promo_skip',      'shop', 'Без промокода',                   '➡️ Без промокода',      ''),
    _b('btn_shop_confirm',         'shop', 'Оформить заказ',                  '✅ Оформить заказ',     ''),
    _b('btn_shop_order_cancel',    'shop', 'Отменить заказ',                  '✖️ Отменить заказ',     ''),
    _b('btn_shop_back_to_orders',  'shop', 'Назад к списку заказов',          '⬅️ К моим заказам',     ''),

    # 💳 Оплата
    _b('btn_payment_yookassa',  'payment', 'YooKassa',          '💳 YooKassa',              ''),
    _b('btn_payment_yoomoney',  'payment', 'YooMoney',          '💰 YooMoney',              ''),
    _b('btn_payment_cryptobot', 'payment', 'CryptoBot',         '💎 CryptoBot',             ''),
    _b('btn_payment_tgstar',    'payment', 'Telegram Stars',    '⭐️ TG Star',               ''),
    _b('btn_payment_platega',   'payment', 'Platega (СБП)',     '🏦 Platega (СБП)',         ''),
    _b('btn_payment_wata',      'payment', 'Wata',              '💳 Wata',                  ''),
    _b('btn_payment_manual',    'payment', 'CloudTips (СБП)',   '💸 CloudTips(СБП-Картой)', ''),
    _b('btn_activate_code',     'payment', 'Оплатить кодом',    '🎟️ Оплатить кодом',        ''),
    # Единая кнопка перехода по ссылке на оплату (YooKassa, Platega, Wata, CryptoBot, YooMoney, расширение лимита)
    _b('btn_payment_pay_link',  'payment', 'Перейти к оплате (ссылка)', '💳 Оплатить',              ''),
    # История платежей пользователя — открывает список последних транзакций
    _b('btn_payment_history',   'payment', 'История платежей',  '📖 История платежей',      ''),

    # ℹ️ Сервис и навигация
    _b('btn_back_to_main',        'service', 'Назад в главное меню',          '⬅️ В главное меню',                       ''),
    _b('btn_back',                'service', 'Назад (внутри подменю)',        '⬅️ Назад',                       ''),
    _b('btn_user_agreement',      'service', 'Пользовательское соглашение',   '📄 Пользовательское соглашение', ''),
    _b('btn_privacy_policy',      'service', 'Политика конфиденциальности',   '🔒 Политика конфиденциальности', ''),
    _b('btn_support_link',        'service', 'Перейти в поддержку (ссылка)',  '💬 Перейти в поддержку',         ''),
    _b('btn_support_custom_link', 'service', 'Кастомная ссылка поддержки',    '📞 Кастомная поддержка',         ''),

    # 🎁 Рефералы и партнёры
    _b('btn_referral',          'referral', 'Реферальная программа',          '🎁 Реферальная программа', ''),
    _b('btn_referral_share',    'referral', 'Поделиться (рефералы)',          '📤 Поделиться',            ''),
    _b('btn_referral_free_days','referral', 'Бесплатные дни (после оплаты)',  '🎁 Бесплатные дни',        ''),
    _b('btn_my_referrals',      'referral', 'Мои рефералы (список)',          '👥 Мои рефералы',          ''),
    _b('btn_partner_program',   'referral', 'Партнёрская программа',          '🤝 Партнёрская программа', ''),
    _b('btn_partner_accruals',  'referral', 'История начислений (партнёрка)', '📜 История начислений',    ''),
    _b('btn_partner_withdraw',  'referral', 'Запросить вывод (партнёрка)',    '💸 Запросить вывод',       ''),

    # 🔄 Продление (CloudTips quick links)
    _b('btn_renew_30', 'renew', 'Продлить на 30 дней (CloudTips)', 'Продлить на 30 дней', ''),
    _b('btn_renew_60', 'renew', 'Продлить на 60 дней (CloudTips)', 'Продлить на 60 дней', ''),
    _b('btn_renew_90', 'renew', 'Продлить на 90 дней (CloudTips)', 'Продлить на 90 дней', ''),
]

BUTTON_REGISTRY_MAP: dict[str, dict[str, Any]] = {b['key']: b for b in BUTTON_REGISTRY}


def style_meta_keys(key: str) -> tuple[str, str]:
    """Возвращает имена settings-ключей для style и icon данной кнопки."""
    return f"{key}__style", f"{key}__icon"


def kind_key(key: str) -> str:
    """settings-ключ режима открытия (url/webapp) для kind-aware кнопок."""
    return f"{key}__kind"


def is_kind_aware(key: str) -> bool:
    return key in KIND_AWARE_KEYS


def group_enabled_key(group_id: str) -> str | None:
    """settings-ключ тумблера «вся группа включена/выключена» либо None."""
    return GROUP_TOGGLE_KEYS.get(group_id)


def enabled_key(key: str) -> str:
    """settings-ключ персонального тумблера видимости кнопки."""
    return f"{key}__enabled"


def has_per_button_toggle(key: str) -> bool:
    return key in PER_BUTTON_TOGGLE_KEYS


def get_default(key: str) -> dict[str, str]:
    """Возвращает дефолтные text/style/icon/kind для кнопки из реестра.
    Если кнопки нет в реестре — возвращает пустые поля.
    """
    reg = BUTTON_REGISTRY_MAP.get(key) or {}
    return {
        'text':  reg.get('default_text', ''),
        'style': reg.get('default_style', ''),
        'icon':  reg.get('default_icon', ''),
        'kind':  reg.get('default_kind', ''),
    }


# --- Раскладка главного меню -------------------------------------------------

MAIN_MENU_LAYOUT_SETTING = 'main_menu_layout'

# Ключи, которые можно размещать в раскладке главного меню.
MAIN_MENU_LAYOUT_KEYS: frozenset[str] = frozenset({
    'btn_website_access',
    'btn_catalog', 'btn_my_router', 'btn_my_orders',
    'btn_renew_sub', 'btn_traffic_renewal',
    'btn_referral', 'btn_support', 'btn_about_service',
    'btn_bot_custom_url', 'btn_bot_channel',
})

DEFAULT_MAIN_MENU_LAYOUT: list[list[str]] = [
    # Порядок под роутеры: сначала своё устройство и покупка, потом продление.
    # Кнопки подключения для приложений, «Мои устройства», гигабайты и лимит
    # устройств в раскладку не входят — они от подписки для телефона и роутеру
    # не нужны. Ключи остались в реестре: вернуть любую — дело раскладки.
    ['btn_my_router'],
    ['btn_catalog', 'btn_my_orders'],
    ['btn_renew_sub'],
    ['btn_referral', 'btn_support'],
    ['btn_about_service'],
    ['btn_bot_custom_url'],
    ['btn_bot_channel'],
]


def parse_main_menu_layout(raw: str | list | None) -> list[list[str]]:
    """Разбирает JSON-раскладку; неизвестные ключи отбрасываются."""
    import json
    if not raw:
        return [list(row) for row in DEFAULT_MAIN_MENU_LAYOUT]
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, list):
            return [list(row) for row in DEFAULT_MAIN_MENU_LAYOUT]
    except (json.JSONDecodeError, TypeError):
        return [list(row) for row in DEFAULT_MAIN_MENU_LAYOUT]

    result: list[list[str]] = []
    seen: set[str] = set()
    for row in data:
        if isinstance(row, str):
            keys = [k.strip() for k in row.split(',') if k.strip()]
        elif isinstance(row, list):
            keys = [str(k).strip() for k in row if str(k).strip()]
        else:
            continue
        row_keys: list[str] = []
        for key in keys:
            if key not in MAIN_MENU_LAYOUT_KEYS or key in seen:
                continue
            row_keys.append(key)
            seen.add(key)
        if row_keys:
            result.append(row_keys)

    if not result:
        return [list(row) for row in DEFAULT_MAIN_MENU_LAYOUT]

    for default_row in DEFAULT_MAIN_MENU_LAYOUT:
        for key in default_row:
            if key not in seen:
                result.append([key])
                seen.add(key)
    return result
