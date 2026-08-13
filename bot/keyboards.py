from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from app_config import app_conf
from button_helpers import btn
from button_registry import MAIN_MENU_LAYOUT_SETTING, parse_main_menu_layout
from db_helpers import get_active_tariffs, get_user


async def _resolve_main_menu_button(key: str, *, user_id, has_active_sub, sub_uuid):
    """Возвращает InlineKeyboardButton или None, если кнопку показывать не нужно."""
    if key == 'btn_website_access':
        if str(app_conf.get('show_website_button', '0')) == '1':
            return btn('btn_website_access', callback_data='website_access')
        return None

    # Каталог роутеров: товар, а не подписка. Выключается настройкой
    # catalog_enabled — вместе с ним прячутся и заказы, показывать их
    # без каталога некуда.
    if key in ('btn_catalog', 'btn_my_orders', 'btn_my_router'):
        if str(app_conf.get('catalog_enabled', '1')) != '1':
            return None
        # «Мой роутер» — только тем, у кого он есть или едет. Клиент, зашедший
        # в бота впервые, жал её и попадал на экран про роутер, которого не
        # покупал. Каталог и заказы показываем всем: там как раз покупают.
        if key == 'btn_my_router':
            if not user_id:
                return None
            from src import shop_api
            if not await shop_api.my_router_available(user_id):
                return None
        callback = {
            'btn_catalog': 'shop_catalog',
            'btn_my_orders': 'shop_orders',
            'btn_my_router': 'shop_my_router',
        }[key]
        return btn(key, callback_data=callback)

    if key == 'btn_renew_sub':
        # Продление ведёт в нашу цепочку, а не в родную: доступ роутеру выдан
        # учётке `tg{id}_{mac}`, а родное продление двигает срок у `tg{id}` —
        # подписки для приложения на телефоне. Клиент заплатил бы, а роутер
        # отключился по старой дате. Каталог выключен — работает как раньше.
        if str(app_conf.get('catalog_enabled', '1')) == '1':
            return btn('btn_renew_sub', callback_data='shop_renew')
        return btn('btn_renew_sub', callback_data='renew_choose_payment')

    if key == 'btn_traffic_renewal':
        traffic_renewal_enabled = str(app_conf.get('traffic_renewal_enabled', '0')) == '1'
        if not (traffic_renewal_enabled and has_active_sub and user_id):
            return None
        try:
            user_data = await get_user(user_id)
            if user_data:
                user_dict = dict(user_data)
                has_remnawave = (
                    user_dict.get('subscription_provider') == 'remnawave'
                    or user_dict.get('remnawave_short_uuid')
                    or user_dict.get('remnawave_username')
                )
                if has_remnawave:
                    return btn('btn_traffic_renewal', callback_data='traffic_renewal_choose_payment')
        except Exception:
            pass
        return None

    if key == 'btn_referral':
        return btn('btn_referral', callback_data='referral_program')
    if key == 'btn_support':
        return btn('btn_support', callback_data='support')
    if key == 'btn_about_service':
        return btn('btn_about_service', callback_data='about_service')

    if key == 'btn_bot_custom_url':
        custom_url = (app_conf.get('bot_custom_url') or '').strip()
        if custom_url:
            return btn('btn_bot_custom_url', url=custom_url)
        return None

    if key == 'btn_bot_channel':
        channel_link = (app_conf.get('bot_channel_link') or '').strip()
        if channel_link:
            return btn('btn_bot_channel', url=channel_link)
        return None

    return None


async def get_main_keyboard(is_trial_available: bool, has_active_sub: bool, sub_uuid: str = None, user_id: int = None):
    from loguru import logger
    logger.info(f"[KEYBOARDS MAIN] Функция вызвана: user_id={user_id}, has_active_sub={has_active_sub}, is_trial_available={is_trial_available}")

    builder = InlineKeyboardBuilder()

    layout = parse_main_menu_layout(app_conf.get(MAIN_MENU_LAYOUT_SETTING, ''))

    for row_keys in layout:
        row_buttons = []
        for key in row_keys:
            b = await _resolve_main_menu_button(key, user_id=user_id,
                has_active_sub=has_active_sub, sub_uuid=sub_uuid)
            if b is not None:
                row_buttons.append(b)
        if row_buttons:
            builder.row(*row_buttons)

    return builder.as_markup()

def get_payment_keyboard(payment_id: str, payment_url: str):
    builder = InlineKeyboardBuilder()
    builder.row(btn('btn_payment_pay_link', url=payment_url))
    builder.row(btn('btn_back_to_main', callback_data='back_to_main'))
    return builder.as_markup()

def get_back_to_main_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(btn('btn_back_to_main', callback_data='back_to_main'))
    return builder.as_markup()

def get_success_with_referral_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(btn('btn_referral_free_days', callback_data='referral_program'))
    builder.row(btn('btn_back_to_main', callback_data='back_to_main'))
    return builder.as_markup()

def get_about_service_keyboard():
    """Клавиатура страницы «О сервисе».
    Содержит ссылки на пользовательское соглашение и политику конфиденциальности.
    """
    builder = InlineKeyboardBuilder()

    user_agreement_link = (app_conf.get('web_user_agreement_link') or '').strip()
    privacy_policy_link = (app_conf.get('web_privacy_policy_link') or '').strip()
    if user_agreement_link:
        builder.row(btn('btn_user_agreement', url=user_agreement_link))
    if privacy_policy_link:
        builder.row(btn('btn_privacy_policy', url=privacy_policy_link))

    builder.row(btn('btn_back_to_main', callback_data='back_to_main'))
    return builder.as_markup()


def get_cryptobot_payment_keyboard(payment_url: str, invoice_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(btn('btn_payment_pay_link', url=payment_url))
    builder.row(btn('btn_back_to_main', callback_data='back_to_main'))
    return builder.as_markup()

def get_yoomoney_payment_keyboard(payment_url: str, payment_id: str):
    builder = InlineKeyboardBuilder()
    builder.row(btn('btn_payment_pay_link', url=payment_url))
    builder.row(btn('btn_back_to_main', callback_data='back_to_main'))
    return builder.as_markup()

def get_captcha_keyboard(correct_answer: str, wrong_answers: list):
    """
    Создает клавиатуру с вариантами ответов для защиты от ботов.
    correct_answer: правильный ответ
    wrong_answers: список неправильных ответов
    """
    builder = InlineKeyboardBuilder()

    all_answers = [correct_answer] + wrong_answers
    import random
    random.shuffle(all_answers)

    for answer in all_answers:
        builder.row(InlineKeyboardButton(
            text=answer,
            callback_data=f"captcha_answer_{answer}"
        ))

    return builder.as_markup()
