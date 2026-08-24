"""Справочник переменных для настраиваемых текстов бота (settings text_*).

Используется в веб-админке при редактировании текстов.
"""
from __future__ import annotations

from typing import Any

# name — имя в шаблоне {name} или {name:.2f}; label — подпись в UI
TEXT_SETTING_VARIABLES: dict[str, list[dict[str, str]]] = {
    # 👋 Приветствие и подписка
    "text_welcome_message": [
        {"name": "user_name", "label": "Имя пользователя"},
        {"name": "project_name", "label": "Название проекта"},
    ],
    "text_subscription_info": [
        {"name": "status", "label": "Статус подписки (например «активна»)"},
        {"name": "expiry_date", "label": "Дата окончания подписки"},
        {"name": "limit_ip", "label": "Лимит устройств или «Без лимита»"},
        {"name": "traffic", "label": "Строка трафика Remnawave или «—»"},
        {"name": "sub_link", "label": "Ссылка на страницу подписки"},
    ],
    "text_subscription_expired_main": [],
    "text_trial_success": [
        {"name": "days", "label": "Длительность пробного периода (дней)"},
        {"name": "expiry_date", "label": "Дата окончания пробного периода"},
    ],
    # 💳 Оплата
    "text_payment_success": [
        {"name": "days", "label": "Оплаченный срок (дней)"},
        {"name": "expiry_date", "label": "Новая дата окончания подписки"},
    ],
    "text_payment_grant_failed": [],
    "text_payment_traffic_grant_failed": [],
    "text_payment_checking": [],
    "text_payment_not_found": [],
    "text_payment_pending": [],
    "text_payment_canceled_or_failed": [],
    # 🎟️ Промокоды
    "text_promo_code_prompt": [],
    "text_promo_code_invalid": [],
    "text_promo_code_already_used": [],
    "text_promo_code_success": [
        {"name": "code", "label": "Код промокода"},
        {"name": "days", "label": "Добавлено дней"},
        {"name": "expiry_date", "label": "Новая дата окончания"},
    ],
    # 👥 Реферальная программа
    "text_referral_program": [
        {"name": "ref_link", "label": "Реферальная ссылка Telegram"},
        {"name": "ref_link_url", "label": "Реферальная ссылка сайта (если настроен website_url)"},
        {"name": "used_invites", "label": "Приглашений сегодня"},
        {"name": "remaining_invites", "label": "Осталось приглашений сегодня"},
        {"name": "max_per_day", "label": "Лимит приглашений в день"},
        {"name": "join_days", "label": "Бонус за первое подключение (дней)"},
        {"name": "payment_days", "label": "Бонус за первый платёж (дней)"},
    ],
    "text_ref_bonus_on_join": [
        {"name": "days", "label": "Начислено дней бонуса"},
    ],
    "text_ref_bonus_on_payment": [
        {"name": "days", "label": "Начислено дней бонуса"},
    ],
    "text_referral_share": [
        {"name": "ref_link", "label": "Реферальная ссылка (в текст «Поделиться» не подставляется — ссылка передаётся отдельно)"},
    ],
    # 💠 Партнёрская программа
    "text_partner_program": [
        {"name": "balance_str", "label": "Баланс партнёра (₽)"},
        {"name": "percent", "label": "Процент отчислений"},
        {"name": "ref_count", "label": "Число приглашённых"},
        {"name": "pay_count", "label": "Число оплат приглашённых"},
        {"name": "link_line", "label": "Блок со ссылками (Telegram / сайт), формируется ботом"},
        {"name": "min_withdraw", "label": "Минимальная сумма вывода (₽)"},
    ],
    "text_partner_withdraw": [
        {"name": "balance_str", "label": "Текущий баланс (₽)"},
        {"name": "user_id", "label": "Telegram ID пользователя"},
    ],
    # 📘 О сервисе
    "text_about_service": [
        {"name": "project_name", "label": "Название проекта"},
    ],
    # 💬 Поддержка
    "text_support": [
        {"name": "user_id", "label": "Telegram ID пользователя"},
    ],
    # 🔔 Уведомления
    "text_subscription_expiring": [],
    "text_subscription_expired": [],
    "text_remnawave_traffic_exhausted": [
        {"name": "used_gb", "label": "Использовано GB (можно {used_gb:.2f})"},
        {"name": "limit_gb", "label": "Лимит GB (можно {limit_gb:.2f})"},
    ],
    "text_subscription_revoke": [],
    # 🌐 Личный кабинет
    "text_website_cabinet_no_email": [],
    "text_website_cabinet_active": [],
    "text_website_cabinet_expired": [],
    # 📈 Докупка трафика
    "text_traffic_renewal_select": [
        {"name": "traffic_info", "label": "Блок текущего трафика (может быть пустым)"},
    ],
    "text_traffic_renewal_confirm": [
        {"name": "tariff_name", "label": "Название тарифа"},
        {"name": "tariff_gb", "label": "GB к добавлению"},
        {"name": "new_traffic_limit_gb", "label": "Новый лимит GB"},
        {"name": "price", "label": "Стоимость (₽)"},
    ],
    "text_traffic_renewal_payment": [
        {"name": "traffic_gb", "label": "GB к добавлению"},
        {"name": "price", "label": "Стоимость"},
        {"name": "currency", "label": "Валюта (₽, XTR, …)"},
    ],
    # 🤖 Защита от ботов
    "bot_protection_text": [
        {"name": "question", "label": "Текст задачи капчи"},
    ],
    "bot_protection_wrong_text": [
        {"name": "question", "label": "Текст новой задачи капчи"},
    ],
    "bot_protection_success_text": [],
    # 🛒 Каталог, заказы и роутер
    "text_catalog_intro": [],
    "text_catalog_empty": [],
    "text_catalog_unavailable": [],
    "text_order_ask_plan": [],
    "text_order_no_plans": [],
    "text_order_ask_name": [],
    "text_order_ask_phone": [],
    "text_order_ask_city": [],
    "text_order_ask_speed": [],
    "text_order_delivery_later": [],
    "text_order_ask_where": [],
    "text_order_ask_pvz": [],
    "text_order_ask_address": [],
    "text_order_ask_promo": [],
    "text_order_promo_prompt": [],
    "text_order_cancelled": [],
    "text_my_router_none": [],
    "text_my_router_waiting": [],
    "text_my_router_not_activated": [],
    "text_my_router_why_no_sub": [],
    "text_orders_empty": [],
    "text_orders_intro": [],
    "text_order_pay_hint": [],
    "text_order_pay_delivery": [
        {"name": "price", "label": "Стоимость доставки (например «450 ₽»)"},
    ],
    "text_order_pay_later": [],
    "text_renew_active": [
        {"name": "date", "label": "Дата, до которой оплачена подписка"},
    ],
    "text_renew_pending": [],
    "text_renew_selected": [
        {"name": "plan", "label": "Название выбранного срока"},
    ],
    # Прочие частые ключи
    "text_error_general": [],
    "text_error_creating_user": [],
}


def get_text_setting_variables(key: str) -> list[dict[str, str]] | None:
    """Возвращает список переменных для ключа или None, если ключ не описан."""
    if key in TEXT_SETTING_VARIABLES:
        return TEXT_SETTING_VARIABLES[key]
    return None


def variables_hint_for_description(key: str) -> str | None:
    """Краткая строка для поля description в БД (не перезаписывает существующие)."""
    vars_ = get_text_setting_variables(key)
    if vars_ is None:
        return None
    if not vars_:
        return "Статичный текст — переменные не используются."
    names = ", ".join(f"{{{v['name']}}}" for v in vars_)
    return f"Переменные: {names}"


def all_text_setting_variables_for_admin() -> dict[str, Any]:
    """Словарь для шаблона админки: key → [{name, label}, …] или []."""
    return dict(TEXT_SETTING_VARIABLES)
