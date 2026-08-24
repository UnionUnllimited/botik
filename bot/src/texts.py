"""
Текстовые шаблоны Telegram-бота.
Константы — для статичных сообщений.
Функции — для сообщений с подстановкой переменных.
"""

# ── Статичные тексты ──────────────────────────────────────────────────────────

TXT_USER_DELETED = (
    "<b>Начать заново</b>\n"
    "○ Профиль не найден\n\n"
    "Нажмите «Старт», чтобы открыть главное меню."
)

TXT_BLOCKED = (
    "<b>Доступ ограничен</b>\n"
    "⚠ Аккаунт заблокирован\n\n"
    "Напишите в поддержку."
)

TXT_MAINTENANCE = (
    "<b>Технические работы</b>\n"
    "○ Сервис временно недоступен\n\n"
    "Попробуйте позже."
)

TXT_SUPPORT_FALLBACK = (
    "<b>Поддержка</b>\n"
    "Нажмите на ID — он скопируется. Отправьте его в чат поддержки вместе с вопросом.\n\n"
    "Ваш ID: <code>{user_id}</code>"
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
        "<b>Продление подписки</b>\n"
        f"○ Срок: <b>{days} дней</b>\n\n"
        f"Лимит: {limit_ip_display}\n"
        f"Стоимость: <b>{price_str} {currency}</b>"
        f"{description_text}\n"
        "<blockquote>Оплата обрабатывается до 5 минут.</blockquote>"
    )


TXT_PAYMENT_GRANT_FAILED = (
    "<b>Оплата получена</b>\n"
    "⚠ Подписку пока не удалось продлить\n\n"
    "Повторно платить не нужно. Если срок не обновится, напишите в поддержку."
)

TXT_PAYMENT_TRAFFIC_GRANT_FAILED = (
    "<b>Оплата получена</b>\n"
    "⚠ Трафик пока не добавлен\n\n"
    "Повторно платить не нужно. Если лимит не обновится, напишите в поддержку."
)


def txt_subscription_time_header(days_text: str, limit_ip_display: str) -> str:
    """Блок с остатком времени подписки в меню выбора оплаты."""
    return (
        f"<blockquote>"
        f"Подписка: <b>{days_text}</b>\n"
        f"Лимит: {limit_ip_display}"
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
        "<b>Проверка оплаты</b>\n"
        f"○ Платёж <code>{payment_id}</code>\n\n"
        f"Сумма: <b>{db_payment[2]} {db_payment[3]}</b>\n"
        "После оплаты нажмите «Проверить статус»."
    )


# ── Докупка трафика ───────────────────────────────────────────────────────────

DEFAULT_TEXT_TRAFFIC_RENEWAL_SELECT = (
    "<b>Докупить трафик</b>{traffic_info}\n"
    "○ Выберите объём"
)

DEFAULT_TEXT_TRAFFIC_RENEWAL_CONFIRM = (
    "<b>Докупить трафик</b>\n"
    "○ Выбран пакет: {tariff_name}\n\n"
    "Добавим: <b>{tariff_gb} GB</b>\n"
    "Новый лимит: {new_traffic_limit_gb} GB\n"
    "Стоимость: <b>{price} ₽</b>"
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
        "<b>Оплата трафика</b>\n"
        "○ Добавим {traffic_gb} GB\n\n"
        "Стоимость: <b>{price} {currency}</b>\n"
        "<blockquote>Оплата обрабатывается до 5 минут.</blockquote>"
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
        "<b>Оплата трафика</b>\n"
        f"○ Добавим {default_traffic_limit_gb} GB\n\n"
        f"Сумма: <b>{price_str} ₽</b>\n"
        f"Ваш ID: <code>{user_id}</code>\n"
        "Укажите ID в комментарии к оплате."
    )


# ── Партнёрская программа ─────────────────────────────────────────────────────

DEFAULT_TEXT_PARTNER_PROGRAM = (
    "<b>Партнёрская программа</b>\n"
    "✓ Баланс: <b>{balance_str} ₽</b>\n"
    "{link_line}\n"
    "Приглашено: {ref_count} · оплат: {pay_count}\n"
    "Начисление: {percent}% · вывод от {min_withdraw} ₽"
)

DEFAULT_TEXT_PARTNER_WITHDRAW = (
    "<b>Вывод средств</b>\n"
    "○ Баланс: <b>{balance_str} ₽</b>\n\n"
    "Ваш ID: <code>{user_id}</code>\n"
    "Отправьте в поддержку сумму и реквизиты."
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
    "<b>Личный кабинет</b>\n"
    "⚠ Email не привязан\n\n"
    "Привяжите email, чтобы входить в кабинет без Telegram."
)

DEFAULT_TEXT_WEBSITE_CABINET_ACTIVE = (
    "<b>Личный кабинет</b>\n"
    "✓ Email привязан\n\n"
    "В кабинете можно продлить подписку, проверить срок и посмотреть заказы.\n"
    "Ссылка для входа действует 10 минут."
)

DEFAULT_TEXT_WEBSITE_CABINET_EXPIRED = (
    "<b>Личный кабинет</b>\n"
    "⚠ Подписка не активна\n\n"
    "Продлите подписку для полного доступа.\n"
    "Ссылка для входа действует 10 минут."
)


# Настраиваемые тексты остальных клиентских экранов. Этот реестр является
# единым источником дефолтов для свежей БД и fallback-текстов в боте.
REST_TEXTS: list[tuple[str, str, str]] = [
    (
        "bot_maintenance_message",
        TXT_MAINTENANCE,
        "Текст сервисного режима",
    ),
    (
        "bot_protection_text",
        "<b>Проверка</b>\n○ Выберите правильный ответ\n\n<b>{question}</b>",
        "Текст защиты от ботов. Переменная: {question}",
    ),
    (
        "bot_protection_success_text",
        "<b>Проверка пройдена</b>\n✓ Всё верно\n\nОткрываем главное меню.",
        "Текст успешной проверки",
    ),
    (
        "bot_protection_wrong_text",
        "<b>Проверка</b>\n✕ Ответ неверный\n\nПопробуйте ещё раз:\n<b>{question}</b>",
        "Текст неверного ответа. Переменная: {question}",
    ),
    (
        "text_trial_success",
        "<b>Пробный период</b>\n✓ Активирован на {days} дней\n\n"
        "Подписка активна до <b>{expiry_date}</b>.",
        "Успешная активация. Переменные: {days}, {expiry_date}",
    ),
    (
        "text_about_service",
        "<b>О сервисе</b>\n"
        "{project_name} — роутеры с подпиской на стабильный доступ к зарубежным ресурсам.\n\n"
        "<blockquote>Подписка начинается с первого выхода роутера на связь — "
        "дни доставки не сгорают.</blockquote>",
        "Экран «О сервисе». Переменная: {project_name}",
    ),
    (
        "text_support",
        TXT_SUPPORT_FALLBACK,
        "Экран поддержки. Переменная: {user_id}",
    ),
    (
        "text_payment_success",
        "<b>Оплата прошла</b>\n✓ Подписка продлена на {days} дней\n\n"
        "Активна до <b>{expiry_date}</b>.",
        "Успешная оплата. Переменные: {days}, {expiry_date}",
    ),
    (
        "text_payment_grant_failed",
        TXT_PAYMENT_GRANT_FAILED,
        "Оплата получена, но подписка пока не продлена",
    ),
    (
        "text_payment_traffic_grant_failed",
        TXT_PAYMENT_TRAFFIC_GRANT_FAILED,
        "Оплата получена, но трафик пока не добавлен",
    ),
    (
        "text_payment_checking",
        "Проверяем оплату",
        "Короткий статус проверки оплаты",
    ),
    (
        "text_payment_not_found",
        "<b>Платёж не найден</b>\n⚠ Не удалось получить статус\n\nПопробуйте позже.",
        "Платёж не найден",
    ),
    (
        "text_payment_pending",
        "Платёж ещё обрабатывается",
        "Платёж ожидает подтверждения",
    ),
    (
        "text_payment_canceled_or_failed",
        "<b>Оплата не прошла</b>\n✕ Платёж отменён\n\nПопробуйте снова или выберите другой способ.",
        "Платёж отменён или завершился ошибкой",
    ),
    (
        "text_promo_code_prompt",
        "<b>Промокод</b>\n○ Введите код\n\nОтправьте его одним сообщением.",
        "Экран ввода промокода",
    ),
    (
        "text_promo_code_invalid",
        "<b>Промокод</b>\n✕ Код не найден\n\nПроверьте написание и попробуйте снова.",
        "Промокод не найден",
    ),
    (
        "text_promo_code_already_used",
        "<b>Промокод</b>\n⚠ Код недоступен\n\nОн уже использован или срок действия закончился.",
        "Промокод недоступен",
    ),
    (
        "text_promo_code_success",
        "<b>Промокод применён</b>\n✓ Код <code>{code}</code>\n\n"
        "Добавлено дней: {days}\nПодписка активна до <b>{expiry_date}</b>.",
        "Промокод применён. Переменные: {code}, {days}, {expiry_date}",
    ),
    (
        "text_referral_program",
        "<b>Реферальная программа</b>\n✓ Бонусы начисляются автоматически\n\n"
        "Ссылка в Telegram: <code>{ref_link}</code>\n"
        "Ссылка на сайт: <code>{ref_link_url}</code>\n"
        "Приглашения: {used_invites}/{max_per_day} · осталось {remaining_invites}\n"
        "Бонус: {join_days} дней за подключение · {payment_days} дней за оплату",
        "Реферальная программа. Переменные: {ref_link}, {ref_link_url}, "
        "{used_invites}, {remaining_invites}, {max_per_day}, {join_days}, {payment_days}",
    ),
    (
        "text_referral_share",
        "Роутер, который работает сразу",
        "Текст для отправки реферальной ссылки",
    ),
    (
        "text_ref_bonus_on_join",
        "<b>Реферальный бонус</b>\n✓ Добавлено {days} дней\n\n"
        "Друг впервые подключил роутер.",
        "Бонус за первое подключение. Переменная: {days}",
    ),
    (
        "text_ref_bonus_on_payment",
        "<b>Реферальный бонус</b>\n✓ Добавлено {days} дней\n\n"
        "Друг оплатил подписку.",
        "Бонус за первую оплату. Переменная: {days}",
    ),
    (
        "text_partner_program",
        DEFAULT_TEXT_PARTNER_PROGRAM,
        "Партнёрская программа. Переменные: {balance_str}, {percent}, {ref_count}, "
        "{pay_count}, {link_line}, {min_withdraw}",
    ),
    (
        "text_partner_withdraw",
        DEFAULT_TEXT_PARTNER_WITHDRAW,
        "Вывод партнёрских средств. Переменные: {balance_str}, {user_id}",
    ),
    (
        "text_subscription_expiring",
        "<b>Подписка заканчивается</b>\n⚠ Остался один день\n\nПродлите подписку, чтобы роутер оставался в сети.",
        "Напоминание за день до окончания подписки",
    ),
    (
        "text_subscription_expired",
        "<b>Подписка закончилась</b>\n⚠ Роутер не выйдет в сеть\n\nПродлите подписку в главном меню.",
        "Уведомление об окончании подписки",
    ),
    (
        "text_subscription_revoke",
        "<b>Данные подписки обновлены</b>\n✓ Новые данные готовы\n\n"
        "Срок подписки и трафик сохранены.\n"
        "Удалите старые данные и подключите новые по кнопке ниже.",
        "Уведомление после сброса данных подписки",
    ),
    (
        "text_remnawave_traffic_exhausted",
        "<b>Трафик закончился</b>\n"
        "⚠ Использовано {used_gb:.2f} из {limit_gb:.2f} GB\n\n"
        "Докупить трафик или продлить подписку можно в меню.",
        "Трафик закончился. Переменные: {used_gb}, {limit_gb}",
    ),
    (
        "text_website_cabinet_no_email",
        DEFAULT_TEXT_WEBSITE_CABINET_NO_EMAIL,
        "Личный кабинет: email не привязан",
    ),
    (
        "text_website_cabinet_active",
        DEFAULT_TEXT_WEBSITE_CABINET_ACTIVE,
        "Личный кабинет: подписка активна",
    ),
    (
        "text_website_cabinet_expired",
        DEFAULT_TEXT_WEBSITE_CABINET_EXPIRED,
        "Личный кабинет: подписка не активна",
    ),
    (
        "text_traffic_renewal_select",
        DEFAULT_TEXT_TRAFFIC_RENEWAL_SELECT,
        "Докупка трафика: выбор объёма. Переменная: {traffic_info}",
    ),
    (
        "text_traffic_renewal_confirm",
        DEFAULT_TEXT_TRAFFIC_RENEWAL_CONFIRM,
        "Докупка трафика: подтверждение. Переменные: {tariff_name}, {tariff_gb}, "
        "{new_traffic_limit_gb}, {price}",
    ),
    (
        "text_traffic_renewal_payment",
        "<b>Оплата трафика</b>\n○ Добавим {traffic_gb} GB\n\n"
        "Стоимость: <b>{price} {currency}</b>\n"
        "<blockquote>Оплата обрабатывается до 5 минут.</blockquote>",
        "Докупка трафика: оплата. Переменные: {traffic_gb}, {price}, {currency}",
    ),
    (
        "text_error_general",
        "<b>Не удалось выполнить действие</b>\n⚠ Временная ошибка\n\nПопробуйте позже.",
        "Общая ошибка",
    ),
    (
        "text_error_creating_user",
        "<b>Не удалось создать подписку</b>\n⚠ Временная ошибка\n\nПопробуйте позже или напишите в поддержку.",
        "Ошибка создания подписки",
    ),
]

REST_TEXT_DEFAULTS: dict[str, str] = {
    key: value for key, value, _ in REST_TEXTS
}

# Значения до редизайна приведены дословно. Миграция обновляет только полное
# совпадение, поэтому любой операторский текст остаётся без изменений.
REST_LEGACY_TEXTS: dict[str, str] = {
    "bot_maintenance_message": "К сожалению, бот находится на технических работах. Попробуйте позже.",
    "bot_protection_text": "🤖 <b>Защита от ботов</b>\n\nДля продолжения решите простую задачу:\n\n<b>{question}</b>",
    "bot_protection_success_text": "✅ <b>Правильно!</b>\n\n⏳ Идет регистрация пробного периода, пожалуйста подождите...",
    "bot_protection_wrong_text": "❌ <b>Неправильно!</b>\n\nПопробуйте еще раз:\n\n<b>{question}</b>",
    "text_trial_success": (
        "🎉 <b>Пробный период активирован!</b>\n\n⏱ <b>Длительность:</b> {days} дней\n\n"
        "📅 <b>Действует до:</b> {expiry_date}\n\n"
        "💡 <b>Для продления подписки используйте кнопку \"Продлить подписку\"</b>"
    ),
    "text_about_service": (
        "{project_name} — роутеры с подпиской на сервис стабильного доступа к зарубежным ресурсам."
    ),
    "text_support": (
        "💬 <b>Поддержка</b>\n\n"
        "Для того, чтобы мы быстро вас нашли, скопируйте ваш ID заранее и отправьте "
        "в поддержку с проблемой, которая у вас случилась.\n\n"
        "📋 <b>Ваш ID для копирования:</b>\n\n"
        "<blockquote>{user_id}</blockquote>\n\n"
        "👇 Нажмите на текст выше, чтобы скопировать ваш ID, затем перейдите в поддержку."
    ),
    "text_payment_grant_failed": (
        "✅ Оплата получена, но продлить подписку сейчас не удалось. "
        "Мы уже знаем о проблеме и исправим в ближайшее время. "
        "Если подписка так и не продлилась — обратитесь в поддержку."
    ),
    "text_payment_traffic_grant_failed": (
        "✅ Оплата получена, увеличить трафик сейчас не удалось. "
        "Мы уже знаем о проблеме и исправим в ближайшее время. "
        "Если трафик так и не увеличился — обратитесь в поддержку."
    ),
    "text_promo_code_success": (
        "Промокод {code} принят: добавлено дней — {days}. "
        "Подписка действует до {expiry_date}."
    ),
    "text_referral_program": (
        "<b>🎁 Реферальная программа</b>\n\nПригласите друзей и получайте бонусы!\n\n"
        "<b>Ваша реферальная ссылка:</b>\n<code>{ref_link}</code>\n\n"
        "<b>Приглашения сегодня:</b> {used_invites}/{max_per_day} "
        "(осталось: {remaining_invites})\n\n"
        "• +{join_days} дня за первое подключение друга по вашей ссылке\n"
        "• +{payment_days} дней за первый платёж друга\n\n"
        "Бонусы начисляются автоматически. Лимит обновляется каждый день!"
    ),
    "text_referral_share": "Присоединяйся!",
    "text_ref_bonus_on_join": (
        "🎁 <b>Реферальный бонус!</b>\n\n"
        "Вы получили {days} дней бонуса за первое подключение приглашённого друга!"
    ),
    "text_ref_bonus_on_payment": (
        "🎁 Вам начислен бонус: +{days} дней подписки за оплату приглашённого пользователя!"
    ),
    "text_partner_program": (
        "🤝 <b>Партнёрская программа</b>\n\n<blockquote>"
        "💰 <b>Баланс:</b> {balance_str} ₽\n"
        "📊 <b>Процент отчислений:</b> {percent}%\n"
        "👥 <b>Приглашено пользователей:</b> {ref_count}\n"
        "💳 <b>Оплат от приглашённых:</b> {pay_count}\n"
        "</blockquote>\n{link_line}\n"
        "Каждый раз когда ваш реферал оплачивает подписку — вы получаете "
        "<b>{percent}%</b> от суммы на баланс.\n\n"
        "<i>Минимальная сумма вывода: {min_withdraw} ₽</i>"
    ),
    "text_partner_withdraw": (
        "💸 <b>Запрос на вывод средств</b>\n\n"
        "💰 Ваш текущий баланс: <b>{balance_str} ₽</b>\n\n"
        "Для вывода средств обратитесь в поддержку, указав:\n"
        "• Ваш Telegram ID: <code>{user_id}</code>\n"
        "• Желаемую сумму вывода\n"
        "• Реквизиты для перевода"
    ),
    "text_subscription_expiring": (
        "⏰ Ваша подписка заканчивается завтра! Не забудьте продлить, чтобы не потерять доступ."
    ),
    "text_subscription_expired": (
        "😔 Ваша подписка истекла. Чтобы возобновить доступ, пожалуйста, продлите ее."
    ),
    "text_subscription_revoke": (
        "⚠️ <b>Данные для подключения вашей подписки были сброшены</b>\n\n"
        "Это могло произойти из-за:\n"
        "• Нарушения правил сервиса\n"
        "• Вашего запроса на сброс\n\n"
        "🔑 <b>Новые данные для подключения готовы</b>\n"
        "📱 Лимит устройств и срок подписки не изменились\n"
        "📊 Использованный трафик сохранён\n\n"
        "⚠️ <b>Важно:</b> После добавления новой подписки обязательно удалите старую "
        "подписку из вашего клиента.\n\n"
        "Нажмите кнопку ниже, чтобы получить новые данные для подключения."
    ),
    "text_remnawave_traffic_exhausted": (
        "⚠️ <b>Трафик закончился</b>\n\n"
        "📊 Использовано: {used_gb:.2f} GB из {limit_gb:.2f} GB\n\n"
        "Безлимитные серверы все равно доступны.\n\n"
        "Для продолжения пользования:\n• Докупите GB\n• Продлите подписку\n\n"
        "Для продления вернитесь на главную."
    ),
    "text_website_cabinet_no_email": (
        "🌐 <b>Личный кабинет</b>\n\n"
        "<blockquote>⚠️ <b>Необходима привязка Email</b>\n"
        "Для доступа к сайту и сохранения вашей подписки, пожалуйста, "
        "привяжите адрес электронной почты.</blockquote>\n\n"
        "<i>Альтернативный доступ к подписке если Telegram не работает.</i>"
    ),
    "text_website_cabinet_active": (
        "🌐 <b>Личный кабинет</b>\n\n<b>В кабинете вы можете:</b>\n"
        "<i>• 💳 Оплатить или продлить подписку\n"
        "• 🔑 Получить ключ подключения\n"
        "• 📊 Следить за трафиком и сроком\n"
        "• 📱 Управлять устройствами</i>\n\n"
        "Нажмите кнопку ниже — вы войдёте автоматически.\n"
        "<i>Ссылка действует 10 минут.</i>"
    ),
    "text_website_cabinet_expired": (
        "🌐 <b>Личный кабинет</b>\n\n"
        "❌ У вас закончилась подписка — продлите для полного доступа к личному кабинету.\n\n"
        "Нажмите кнопку ниже — вы войдёте автоматически.\n"
        "<i>Ссылка действует 10 минут.</i>"
    ),
    "text_traffic_renewal_select": (
        "📈 <b>Докупить гигабайты</b>{traffic_info}\n\n💳 <b>Выберите тариф:</b>"
    ),
    "text_traffic_renewal_confirm": (
        "📈 <b>Докупить гигабайты</b>\n\n"
        "📦 <b>Выбранный тариф:</b> {tariff_name}\n"
        "➕ <b>Будет добавлено:</b> {tariff_gb} GB\n"
        "📊 <b>Новый лимит:</b> {new_traffic_limit_gb} GB\n\n"
        "💰 <b>Стоимость:</b> {price} ₽\n\n"
        "💳 <b>Выберите способ оплаты:</b>"
    ),
    "text_traffic_renewal_payment": (
        "💳 <b>Оплата продления трафика</b>\n\n"
        "➕ <b>Будет добавлено:</b> {traffic_gb} GB\n"
        "💰 <b>Стоимость:</b> {price} {currency}\n\n"
        "Нажмите кнопку оплаты ниже, чтобы перейти к оплате.\n\n"
        "<blockquote>⏰ Ваш успешный платеж будет обработан до 5 минут</blockquote>"
    ),
    "text_error_creating_user": (
        "❌ <b>Ошибка создания пользователя</b>\n\n"
        "Не удалось создать пробный период. Попробуйте позже или обратитесь в поддержку."
    ),
}

REST_REDESIGN_MARK = "ui_redesign_2026_08_rest"


def setting_text(template: str | None, default: str) -> str:
    """Возвращает текст из настроек или дефолт, если значение пустое."""
    stripped = (template or '').strip()
    return stripped or default
