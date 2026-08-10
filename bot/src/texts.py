"""
Текстовые шаблоны Telegram-бота.
Константы — для статичных сообщений.
Функции — для сообщений с подстановкой переменных.
"""

# ── Статичные тексты ──────────────────────────────────────────────────────────

TXT_USER_DELETED = (
    "😔 <b>Упсс!</b>\n\n"
    "Похоже, вы были удалены из системы.\n\n"
    "Но не расстраивайтесь — это отличная возможность нам начать сначала! 🚀\n\n"
    "Нажмите кнопку <b>\"Старт\"</b> ниже, чтобы получить пробный период."
)

TXT_BLOCKED = (
    "🚫 <b>Ваш аккаунт заблокирован</b>\n\n"
    "Обратитесь в поддержку для разрешения ситуации."
)

TXT_MAINTENANCE = (
    "К сожалению, бот находится на технических работах. Попробуйте позже."
)

TXT_SUPPORT_FALLBACK = (
    "💬 <b>Поддержка</b>\n\n"
    "Для того, чтобы мы быстро вас нашли, скопируйте ваш ID заранее и отправьте в поддержку с проблемой, которая у вас случилась.\n\n"
    "📋 <b>Ваш ID для копирования:</b>\n\n"
    "<blockquote>{user_id}</blockquote>\n\n"
    "👇 Нажмите на текст выше, чтобы скопировать ваш ID, затем перейдите в поддержку."
)


# ── Оплата подписки ───────────────────────────────────────────────────────────

def txt_payment_renewal(
    days: int,
    limit_ip_display: str,
    price_str: str,
    currency: str,
    description_text: str = "",
) -> str:
    """Карточка оформления продления подписки (YooKassa, YooMoney, Platega, CryptoBot)."""
    return (
        "💳 <b>Оформление продления</b>\n\n"
        f"⏱ <b>Срок:</b> <b><u>{days} дней</u></b>\n"
        f"📱 <b>Лимит устройств:</b> {limit_ip_display}\n"
        f"💰 <b>Стоимость:</b> {price_str} {currency}"
        f"{description_text}\n"
        "Нажмите кнопку оплаты ниже, чтобы перейти к оплате.\n\n"
        "<blockquote>⏰ Ваш успешный платеж будет обработан до 5 минут</blockquote>"
    )


TXT_PAYMENT_GRANT_FAILED = (
    "✅ Оплата получена, но продлить подписку сейчас не удалось. "
    "Мы уже знаем о проблеме и исправим в ближайшее время. "
    "Если подписка так и не продлилась — обратитесь в поддержку."
)

TXT_PAYMENT_TRAFFIC_GRANT_FAILED = (
    "✅ Оплата получена, увеличить трафик сейчас не удалось. "
    "Мы уже знаем о проблеме и исправим в ближайшее время. "
    "Если трафик так и не увеличился — обратитесь в поддержку."
)


def txt_subscription_time_header(days_text: str, limit_ip_display: str) -> str:
    """Блок с остатком времени подписки в меню выбора оплаты."""
    return (
        f"<blockquote>"
        f"⏱ <b>До окончания подписки:</b> <b>{days_text}</b>\n"
        f"📱 <b>Текущий лимит устройств:</b> {limit_ip_display}"
        f"</blockquote>\n\n"
    )


def txt_admin_manual_payment_notify(user_id: int, username: str) -> str:
    """Уведомление администраторам об оплате переводом."""
    return (
        "🔔 Уведомление об оплате переводом\n\n"
        f"Пользователь сообщил, что оплатил переводом.\n"
        f"ID: <code>{user_id}</code>\n"
        f"Username: @{username}\n\n"
        "Пожалуйста, проверьте оплату и при необходимости продлите подписку через веб‑админку по его ID."
    )


def txt_yoomoney_check(payment_id: str, db_payment: tuple) -> str:
    """Сообщение при проверке статуса платежа YooMoney."""
    return (
        f"💰 Проверка платежа YooMoney\n\n"
        f"<b>ID платежа:</b> {payment_id}\n"
        f"<b>Сумма:</b> {db_payment[2]} {db_payment[3]}\n\n"
        f"⚠️ <b>Важно:</b> После оплаты подписка активируется автоматически.\n\n"
        f"Если оплата еще не произведена, завершите оплату и нажмите 'Проверить статус'."
    )


# ── Докупка трафика ───────────────────────────────────────────────────────────

DEFAULT_TEXT_TRAFFIC_RENEWAL_SELECT = (
    "📈 <b>Докупить гигабайты</b>{traffic_info}\n\n"
    "💳 <b>Выберите тариф:</b>"
)

DEFAULT_TEXT_TRAFFIC_RENEWAL_CONFIRM = (
    "📈 <b>Докупить гигабайты</b>\n\n"
    "📦 <b>Выбранный тариф:</b> {tariff_name}\n"
    "➕ <b>Будет добавлено:</b> {tariff_gb} GB\n"
    "📊 <b>Новый лимит:</b> {new_traffic_limit_gb} GB\n\n"
    "💰 <b>Стоимость:</b> {price} ₽\n\n"
    "💳 <b>Выберите способ оплаты:</b>"
)


def txt_traffic_renewal_select(
    traffic_info: str = '',
    template: str | None = None,
) -> str:
    """Экран выбора тарифа докупки трафика."""
    return _format_text_template(
        template or '',
        DEFAULT_TEXT_TRAFFIC_RENEWAL_SELECT,
        traffic_info=traffic_info,
    )


def txt_traffic_renewal_confirm(
    tariff_name: str,
    tariff_gb: int | float,
    new_traffic_limit_gb: float,
    price_str: str,
    template: str | None = None,
) -> str:
    """Экран после выбора тарифа — выбор способа оплаты."""
    gb_added = int(tariff_gb) if tariff_gb == int(tariff_gb) else tariff_gb
    new_limit_str = f"{new_traffic_limit_gb:.2f}"
    return _format_text_template(
        template or '',
        DEFAULT_TEXT_TRAFFIC_RENEWAL_CONFIRM,
        tariff_name=tariff_name,
        tariff_gb=gb_added,
        new_traffic_limit_gb=new_limit_str,
        price=price_str,
    )


def txt_buy_gb_header(
    tariff_name: str,
    tariff_gb: int,
    new_traffic_limit_gb: float,
    price_str: str,
    template: str | None = None,
) -> str:
    """Заголовок экрана докупки гигабайт (обратная совместимость)."""
    return txt_traffic_renewal_confirm(
        tariff_name, tariff_gb, new_traffic_limit_gb, price_str, template=template,
    )


def txt_traffic_renewal_payment(
    traffic_to_add_gb: int | float,
    price_str: str,
    currency: str,
    template: str | None = None,
) -> str:
    """Единый экран оплаты докупки трафика (все провайдеры)."""
    traffic_gb = (
        int(traffic_to_add_gb)
        if traffic_to_add_gb == int(traffic_to_add_gb)
        else traffic_to_add_gb
    )
    default = (
        "💳 <b>Оплата продления трафика</b>\n\n"
        "➕ <b>Будет добавлено:</b> {traffic_gb} GB\n"
        "💰 <b>Стоимость:</b> {price} {currency}\n\n"
        "Нажмите кнопку оплаты ниже, чтобы перейти к оплате.\n\n"
        "<blockquote>⏰ Ваш успешный платеж будет обработан до 5 минут</blockquote>"
    )
    return _format_text_template(
        template or '',
        default,
        traffic_gb=traffic_gb,
        price=price_str,
        currency=currency,
    )


def txt_manual_traffic_renewal(
    default_traffic_limit_gb: int,
    price_str: str,
    user_id: int,
) -> str:
    """Инструкция к ручной оплате продления трафика."""
    return (
        f"💸 <b>Ручная оплата продления трафика</b>\n\n"
        f"➕ Будет добавлено: {default_traffic_limit_gb} GB\n"
        f"💰 Сумма: {price_str} ₽\n\n"
        f"Скопируйте и вставьте <b>ваш ID</b> в комментарий к оплате, чтобы мы могли быстро обработать запрос.\n\n"
        f"<blockquote>Ваш ID: <code>{user_id}</code></blockquote>\n\n"
        f"После оплаты нажмите кнопку ниже для подтверждения."
    )


# ── Партнёрская программа ─────────────────────────────────────────────────────

DEFAULT_TEXT_PARTNER_PROGRAM = (
    "🤝 <b>Партнёрская программа</b>\n\n"
    "<blockquote>"
    "💰 <b>Баланс:</b> {balance_str} ₽\n"
    "📊 <b>Процент отчислений:</b> {percent}%\n"
    "👥 <b>Приглашено пользователей:</b> {ref_count}\n"
    "💳 <b>Оплат от приглашённых:</b> {pay_count}\n"
    "</blockquote>\n"
    "{link_line}\n"
    "Каждый раз когда ваш реферал оплачивает подписку — вы получаете "
    "<b>{percent}%</b> от суммы на баланс.\n\n"
    "<i>Минимальная сумма вывода: {min_withdraw} ₽</i>"
)

DEFAULT_TEXT_PARTNER_WITHDRAW = (
    "💸 <b>Запрос на вывод средств</b>\n\n"
    "💰 Ваш текущий баланс: <b>{balance_str} ₽</b>\n\n"
    "Для вывода средств обратитесь в поддержку, указав:\n"
    "• Ваш Telegram ID: <code>{user_id}</code>\n"
    "• Желаемую сумму вывода\n"
    "• Реквизиты для перевода"
)


def _format_text_template(template: str, fallback: str, **kwargs) -> str:
    for tpl in (template, fallback):
        if not tpl:
            continue
        try:
            return tpl.format(**kwargs)
        except (KeyError, ValueError):
            continue
    return fallback.format(**kwargs)


def txt_partner_program(
    balance_str: str,
    percent: int | float,
    ref_count: int,
    pay_count: int,
    link_line: str,
    min_withdraw: int | float,
    template: str | None = None,
) -> str:
    """Главный экран партнёрской программы."""
    return _format_text_template(
        template or '',
        DEFAULT_TEXT_PARTNER_PROGRAM,
        balance_str=balance_str,
        percent=percent,
        ref_count=ref_count,
        pay_count=pay_count,
        link_line=link_line,
        min_withdraw=min_withdraw,
    )


def txt_withdraw_request(
    balance_str: str,
    user_id: int,
    template: str | None = None,
) -> str:
    """Экран запроса на вывод средств."""
    return _format_text_template(
        template or '',
        DEFAULT_TEXT_PARTNER_WITHDRAW,
        balance_str=balance_str,
        user_id=user_id,
    )


# ── Личный кабинет (сайт) ─────────────────────────────────────────────────────

DEFAULT_TEXT_WEBSITE_CABINET_NO_EMAIL = (
    "🌐 <b>Личный кабинет</b>\n\n"
    "<blockquote>⚠️ <b>Необходима привязка Email</b>\n"
    "Для доступа к сайту и сохранения вашей подписки, пожалуйста, "
    "привяжите адрес электронной почты.</blockquote>\n\n"
    "<i>Альтернативный доступ к подписке если Telegram не работает.</i>"
)

DEFAULT_TEXT_WEBSITE_CABINET_ACTIVE = (
    "🌐 <b>Личный кабинет</b>\n\n"
    "<b>В кабинете вы можете:</b>\n"
    "<i>• 💳 Оплатить или продлить подписку\n"
    "• 🔑 Получить ключ подключения\n"
    "• 📊 Следить за трафиком и сроком\n"
    "• 📱 Управлять устройствами</i>\n\n"
    "Нажмите кнопку ниже — вы войдёте автоматически.\n"
    "<i>Ссылка действует 10 минут.</i>"
)

DEFAULT_TEXT_WEBSITE_CABINET_EXPIRED = (
    "🌐 <b>Личный кабинет</b>\n\n"
    "❌ У вас закончилась подписка — продлите для полного доступа к личному кабинету.\n\n"
    "Нажмите кнопку ниже — вы войдёте автоматически.\n"
    "<i>Ссылка действует 10 минут.</i>"
)


def setting_text(template: str | None, default: str) -> str:
    """Возвращает текст из настроек или дефолт, если значение пустое."""
    stripped = (template or '').strip()
    return stripped or default


# ── Расширение лимита устройств ───────────────────────────────────────────────

DEFAULT_TEXT_DEVICE_UPGRADE_SELECT = (
    "📱 <b>Расширение лимита устройств</b>\n\n"
    "📊 Текущий лимит: <b>{current_limit}</b>\n"
    "📅 Подписка до: <b>{subscription_end_date}</b>\n"
    "⏳ Осталось: <b>{days_left} {days_left_word}</b>\n\n"
    "Выберите новый лимит:"
)

DEFAULT_TEXT_DEVICE_UPGRADE_CONFIRM = (
    "📱 <b>Расширение лимита устройств</b>\n\n"
    "🔄 Лимит: {old_limit} → <b>{new_limit} {new_limit_word}</b>\n"
    "📅 Срок подписки: до <b>{subscription_end_date}</b>\n"
    "💰 К оплате: <b>{price}</b> (за {days_left} {days_left_word})\n"
    "{monthly_line}\n"
    "Выберите способ оплаты:"
)

DEFAULT_TEXT_DEVICE_UPGRADE_PAYMENT = (
    "💳 <b>Оплата расширения лимита</b>\n\n"
    "🔄 Лимит: {old_limit} → <b>{new_limit} {new_limit_word}</b>\n"
    "💰 К оплате: <b>{price}</b>\n"
    "\nНажмите кнопку ниже для оплаты:"
)


def txt_device_upgrade_select(
    *,
    current_limit: str,
    subscription_end_date: str,
    days_left: int,
    days_left_word: str,
    template: str | None = None,
) -> str:
    return _format_text_template(
        template or '',
        DEFAULT_TEXT_DEVICE_UPGRADE_SELECT,
        current_limit=current_limit,
        subscription_end_date=subscription_end_date,
        days_left=days_left,
        days_left_word=days_left_word,
    )


def txt_device_upgrade_confirm(
    *,
    old_limit: int,
    new_limit: int,
    new_limit_word: str,
    subscription_end_date: str,
    price: str,
    days_left: int,
    days_left_word: str,
    monthly_line: str = '',
    template: str | None = None,
) -> str:
    return _format_text_template(
        template or '',
        DEFAULT_TEXT_DEVICE_UPGRADE_CONFIRM,
        old_limit=old_limit,
        new_limit=new_limit,
        new_limit_word=new_limit_word,
        subscription_end_date=subscription_end_date,
        price=price,
        days_left=days_left,
        days_left_word=days_left_word,
        monthly_line=monthly_line,
    )


def txt_device_upgrade_payment(
    *,
    old_limit: int,
    new_limit: int,
    new_limit_word: str,
    price: str,
    template: str | None = None,
) -> str:
    return _format_text_template(
        template or '',
        DEFAULT_TEXT_DEVICE_UPGRADE_PAYMENT,
        old_limit=old_limit,
        new_limit=new_limit,
        new_limit_word=new_limit_word,
        price=price,
    )


DEVICE_UPGRADE_REASON_DEFAULTS: dict[str, str] = {
    'too_few_days_left': (
        'До конца подписки осталось мало дней. Сначала продлите подписку.'
    ),
    'no_active_subscription': (
        'У вас нет активной подписки. Сначала оформите подписку.'
    ),
    'feature_disabled': 'Расширение лимита временно недоступно.',
    'no_options': 'Доступных вариантов расширения нет.',
    'over_max_limit': 'Превышен максимально доступный лимит.',
    'already_unlimited': 'У вас уже безлимит по устройствам — расширять нечего.',
    'not_an_upgrade': 'Новый лимит должен быть больше текущего.',
    'invalid_new_limit': 'Некорректное значение лимита.',
    'user_not_found': 'Профиль не найден. Перезапустите бота /start.',
    'no_payment_methods': 'Способы оплаты временно недоступны. Попробуйте позже.',
}

DEVICE_UPGRADE_REASON_ORDER = (
    'too_few_days_left',
    'no_active_subscription',
    'feature_disabled',
    'no_options',
    'over_max_limit',
    'already_unlimited',
    'not_an_upgrade',
    'invalid_new_limit',
    'user_not_found',
    'no_payment_methods',
)

DEVICE_UPGRADE_REASON_DESCRIPTIONS: dict[str, str] = {
    'too_few_days_left': 'Мало дней до конца подписки — расширение недоступно',
    'no_active_subscription': 'Нет активной подписки',
    'feature_disabled': 'Функция расширения лимита выключена',
    'no_options': 'Нет доступных вариантов расширения',
    'over_max_limit': 'Достигнут максимальный лимит устройств',
    'already_unlimited': 'У пользователя уже безлимит по устройствам',
    'not_an_upgrade': 'Выбранный лимит не больше текущего',
    'invalid_new_limit': 'Некорректное значение лимита',
    'user_not_found': 'Профиль пользователя не найден',
    'no_payment_methods': 'Нет доступных способов оплаты на экране расширения',
}


def device_upgrade_reason_setting_key(reason: str) -> str:
    return f'text_device_upgrade_reason_{reason}'


def txt_device_upgrade_reason(reason: str, template: str | None = None) -> str:
    """Текст отказа/ошибки при расширении лимита устройств."""
    default = DEVICE_UPGRADE_REASON_DEFAULTS.get(
        reason,
        DEVICE_UPGRADE_REASON_DEFAULTS['no_options'],
    )
    return setting_text(template, default)
