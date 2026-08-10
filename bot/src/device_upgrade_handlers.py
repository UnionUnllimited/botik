"""
Хендлеры бота для платного расширения лимита устройств.

Поток:
    [Главное меню] «📱 Расширить лимит устройств»
        → device_upgrade_start
            → Карточка с вариантами лимита (кнопки)
        → device_upgrade_select:{new_limit}
            → Предпросмотр (лимит, дата подписки, цена) + выбор провайдера
        → device_upgrade_pay:{provider}:{new_limit}
            → Создание платежа (YooKassa, Platega, Wata, YooMoney, CryptoBot, TG Stars)
            → Кнопка перехода к оплате (btn_payment_pay_link из реестра)

После успешной оплаты лимит применяется в process_successful_payment (main.py),
ветка payment_type == 'device_limit_upgrade'.
"""
from __future__ import annotations

import json
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app_config import app_conf
from button_helpers import btn
import keyboards
from src.texts import (
    txt_device_upgrade_select,
    txt_device_upgrade_confirm,
    txt_device_upgrade_payment,
    txt_device_upgrade_reason,
    device_upgrade_reason_setting_key,
)
from src.device_upgrade_service import (
    calculate_upgrade_price,
    get_available_options,
    build_payment_metadata,
    get_monthly_price_for_limit,
)

logger = logging.getLogger(__name__)


def _device_upgrade_error_text(reason: str) -> str:
    key = device_upgrade_reason_setting_key(reason or 'no_options')
    return f"❌ {txt_device_upgrade_reason(reason or 'no_options', template=app_conf.get(key))}"


def _back_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        btn('btn_back_to_main', callback_data='back_to_main')
    ]])


def _format_price(price: float, currency: str = 'RUB') -> str:
    try:
        v = float(price)
        if v.is_integer():
            return f"{int(v)} ₽" if currency == 'RUB' else f"{int(v)} {currency}"
        return f"{v:.2f} ₽" if currency == 'RUB' else f"{v:.2f} {currency}"
    except Exception:
        return f"{price} {currency}"


def _plural_devices(n: int) -> str:
    """Склонение слова 'устройство' для русского языка."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return 'устройство'
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return 'устройства'
    return 'устройств'


def _plural_days(n: int) -> str:
    """Склонение слова 'день' для русского языка."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return 'день'
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return 'дня'
    return 'дней'


def _format_gb_amount(gb: float) -> str:
    """Человекочитаемое число GB без лишних нулей."""
    try:
        v = float(gb)
    except (TypeError, ValueError):
        return ''
    if v <= 0:
        return ''
    if v == int(v):
        return str(int(v))
    return f"{v:.1f}"


def _format_monthly_subscription_block(monthly: dict) -> str:
    """Блок «Подписка далее» для Telegram (HTML)."""
    m_days = int(monthly.get('days') or 0)
    price = monthly.get('price')
    if m_days <= 0 or not price:
        return ''

    price_str = _format_price(price)
    m_traffic_gb = float(monthly.get('traffic_gb') or 0)
    gb_str = _format_gb_amount(m_traffic_gb)

    period_line = f"<b>{price_str}</b> за {m_days} {_plural_days(m_days)}"
    if gb_str:
        inner = (
            f"{period_line}\n"
            f"📊 <b>{gb_str} GB</b> трафика"
        )
    else:
        inner = period_line

    return f"📆 <b>Подписка далее</b>\n<blockquote>{inner}</blockquote>\n"


# ────────────────────────────────────────────────────────────────────────────
# Шаг 1. Карточка выбора нового лимита
# ────────────────────────────────────────────────────────────────────────────

async def cmd_device_upgrade_start(query: CallbackQuery):
    user_id = query.from_user.id
    info = await get_available_options(user_id)

    if not info['allowed']:
        await query.message.edit_text(
            _device_upgrade_error_text(info.get('reason') or 'no_options'),
            reply_markup=_back_kbd(),
        )
        await query.answer()
        return

    builder = InlineKeyboardBuilder()
    # Кнопки выбора нового лимита (по 3 в ряд — компактно для длинных списков).
    per_row = 3
    row = []
    for opt in info['options']:
        text = f"📱 {opt['new_limit']} • {_format_price(opt['price'])}"
        row.append(InlineKeyboardButton(
            text=text,
            callback_data=f"device_upgrade_select:{opt['new_limit']}",
        ))
        if len(row) == per_row:
            builder.row(*row)
            row = []
    if row:
        builder.row(*row)
    builder.row(btn('btn_back_to_main', callback_data='back_to_main'))

    cur_limit = info['old_limit']
    end_date = info.get('subscription_end_date') or '—'
    days_left = int(info.get('days_left', 0))
    cur_limit_str = (
        f"{cur_limit} {_plural_devices(cur_limit)}" if cur_limit > 0 else "∞"
    )
    text = txt_device_upgrade_select(
        current_limit=cur_limit_str,
        subscription_end_date=end_date,
        days_left=days_left,
        days_left_word=_plural_days(days_left),
        template=app_conf.get('text_device_upgrade_select'),
    )
    await query.message.edit_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode='HTML',
        disable_web_page_preview=True,
    )
    await query.answer()


# ────────────────────────────────────────────────────────────────────────────
# Шаг 2. Предпросмотр и выбор способа оплаты
# ────────────────────────────────────────────────────────────────────────────

async def cmd_device_upgrade_select(query: CallbackQuery):
    user_id = query.from_user.id
    try:
        new_limit = int(query.data.split(':', 1)[1])
    except (ValueError, IndexError):
        await query.answer('Некорректный лимит.', show_alert=True)
        return

    calc = await calculate_upgrade_price(user_id, new_limit)
    if not calc['allowed']:
        await query.message.edit_text(
            _device_upgrade_error_text(calc.get('reason') or 'no_options'),
            reply_markup=_back_kbd(),
        )
        await query.answer()
        return

    # Доступные провайдеры
    show_yookassa = str(app_conf.get('show_payment_yookassa', '1')) == '1' \
        and bool(app_conf.get('yookassa_shop_id')) and bool(app_conf.get('yookassa_secret_key'))
    show_platega = str(app_conf.get('show_payment_platega', '1')) == '1' \
        and bool(app_conf.get('platega_merchant_id')) and bool(app_conf.get('platega_api_secret'))
    show_cryptobot = str(app_conf.get('show_payment_cryptobot', '1')) == '1' \
        and bool(app_conf.get('cryptobot_token'))
    # TG Stars (XTR) — provider_token НЕ требуется (с осени 2024 Telegram
    # принимает Stars напрямую), поэтому проверяем только флаг показа.
    show_tgstar = str(app_conf.get('show_payment_tgstar', '1')) == '1'
    # Wata: достаточно access_token; terminal_public_id опционален.
    show_wata = str(app_conf.get('show_payment_wata', '1')) == '1' \
        and bool(app_conf.get('wata_access_token'))
    show_yoomoney = str(app_conf.get('show_payment_yoomoney', '1')) == '1' \
        and bool(app_conf.get('yoomoney_account'))

    builder = InlineKeyboardBuilder()
    from payment_methods import build_bot_payment_keyboard_rows

    def _upgrade_cb(mid: str):
        return f"device_upgrade_pay:{mid}:{new_limit}"

    pay_rows = build_bot_payment_keyboard_rows(
        _upgrade_cb, exclude=frozenset({'promo', 'manual'}),
    )
    if not pay_rows:
        await query.message.edit_text(
            _device_upgrade_error_text('no_payment_methods'),
            reply_markup=_back_kbd(),
        )
        await query.answer()
        return

    for row in pay_rows:
        builder.row(*row)

    builder.row(btn('btn_back', callback_data='device_upgrade_start'))

    days_left = int(calc.get('days_left') or 0)
    new_limit_word = _plural_devices(calc['new_limit'])

    # «Подписка далее»: стоимость продления подписки с новым лимитом — ищем
    # минимальный «месячный» тариф (24-35 дней) только среди ВКЛЮЧЁННЫХ RUB-методов
    # оплаты. Показываем строку, только если тариф найден.
    enabled_rub_methods = set()
    if show_yookassa:
        enabled_rub_methods.add('yookassa')
    if show_platega:
        enabled_rub_methods.add('platega')
    if show_wata:
        enabled_rub_methods.add('wata')
    if show_yoomoney:
        enabled_rub_methods.add('yoomoney')

    monthly_line = ''
    try:
        monthly = await get_monthly_price_for_limit(calc['new_limit'], enabled_rub_methods)
    except Exception as e:
        monthly = None
        logger.debug(f"[DEVICE_UPGRADE] monthly tariff lookup failed: {e}")
    if monthly and monthly.get('price'):
        monthly_line = _format_monthly_subscription_block(monthly)

    text = txt_device_upgrade_confirm(
        old_limit=calc['old_limit'],
        new_limit=calc['new_limit'],
        new_limit_word=new_limit_word,
        subscription_end_date=calc['subscription_end_date'],
        price=_format_price(calc['price']),
        days_left=days_left,
        days_left_word=_plural_days(days_left),
        monthly_line=monthly_line,
        template=app_conf.get('text_device_upgrade_confirm'),
    )
    await query.message.edit_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode='HTML',
        disable_web_page_preview=True,
    )
    await query.answer()


# ────────────────────────────────────────────────────────────────────────────
# Шаг 3. Создание платежа
# ────────────────────────────────────────────────────────────────────────────

async def cmd_device_upgrade_pay(query: CallbackQuery, bot: Bot):
    user_id = query.from_user.id
    # Поддерживаемые форматы (v2 — единый):
    #   device_upgrade_pay:yookassa:{new_limit}
    #   device_upgrade_pay:platega:{new_limit}
    #   device_upgrade_pay:yoomoney:{new_limit}
    # Для обратной совместимости поддерживаем legacy:
    #   device_upgrade_pay:platega:{method_id}:{new_limit}
    try:
        parts = query.data.split(':')
        provider = parts[1]
        # new_limit всегда последний компонент
        new_limit = int(parts[-1])
    except (ValueError, IndexError):
        await query.answer('Некорректные данные.', show_alert=True)
        return

    # Перепроверяем расчёт перед созданием платежа (чтобы не было «фантомных» цен)
    calc = await calculate_upgrade_price(user_id, new_limit)
    if not calc['allowed']:
        await query.message.edit_text(
            _device_upgrade_error_text(calc.get('reason') or 'no_options'),
            reply_markup=_back_kbd(),
        )
        await query.answer()
        return

    upgrade_meta = build_payment_metadata(user_id, new_limit, calc)

    bot_username = (await bot.get_me()).username
    return_url = f"https://t.me/{bot_username}"

    payment_id = None
    pay_url = None

    if provider == 'yookassa':
        shop_id = app_conf.get('yookassa_shop_id')
        secret = app_conf.get('yookassa_secret_key')
        if not shop_id or not secret:
            await query.message.edit_text(
                "❌ YooKassa не настроена. Обратитесь в поддержку.",
                reply_markup=_back_kbd(),
            )
            await query.answer()
            return

        from src.pay import create_yookassa_payment_device_upgrade
        result = await create_yookassa_payment_device_upgrade(
            shop_id=str(shop_id),
            secret_key=str(secret),
            user_id=user_id,
            new_limit=new_limit,
            upgrade_meta=upgrade_meta,
            return_url=return_url,
        )
        if result:
            payment_id, pay_url = result

    elif provider == 'platega':
        merchant_id = app_conf.get('platega_merchant_id')
        api_secret = app_conf.get('platega_api_secret')
        if not merchant_id or not api_secret:
            await query.message.edit_text(
                "❌ Platega не настроена. Обратитесь в поддержку.",
                reply_markup=_back_kbd(),
            )
            await query.answer()
            return

        from src.pay import create_platega_payment_device_upgrade
        result = await create_platega_payment_device_upgrade(
            merchant_id=str(merchant_id),
            api_secret=str(api_secret),
            user_id=user_id,
            new_limit=new_limit,
            upgrade_meta=upgrade_meta,
            return_url=return_url,
        )
        if result:
            payment_id, pay_url = result

    elif provider == 'wata':
        import os as _os
        access_token = (app_conf.get('wata_access_token') or '').strip() \
            or (_os.getenv('WATA_ACCESS_TOKEN') or '').strip()
        terminal_public_id = (app_conf.get('wata_terminal_public_id') or '').strip() \
            or (_os.getenv('WATA_TERMINAL_PUBLIC_ID') or '').strip()
        if not access_token:
            await query.message.edit_text(
                "❌ Wata не настроена. Обратитесь в поддержку.",
                reply_markup=_back_kbd(),
            )
            await query.answer()
            return

        from src.pay import create_wata_payment_device_upgrade
        result = await create_wata_payment_device_upgrade(
            access_token=str(access_token),
            user_id=user_id,
            new_limit=new_limit,
            upgrade_meta=upgrade_meta,
            return_url=return_url,
            terminal_public_id=str(terminal_public_id),
        )
        if result:
            payment_id, pay_url = result

    elif provider == 'yoomoney':
        account = app_conf.get('yoomoney_account')
        if not account:
            await query.message.edit_text(
                "❌ YooMoney не настроен. Обратитесь в поддержку.",
                reply_markup=_back_kbd(),
            )
            await query.answer()
            return

        from src.pay import create_yoomoney_quickpay_device_upgrade
        yoomoney_meta = dict(upgrade_meta)
        yoomoney_meta['registration_type'] = 'bot'
        result = await create_yoomoney_quickpay_device_upgrade(
            account=str(account),
            user_id=user_id,
            new_limit=new_limit,
            upgrade_meta=yoomoney_meta,
        )
        if not result:
            await query.message.edit_text(
                "❌ Не удалось создать платёж. Попробуйте позже или обратитесь в поддержку.",
                reply_markup=_back_kbd(),
            )
            await query.answer()
            return

        payment_id, pay_url = result
        new_limit_word = _plural_devices(calc['new_limit'])
        text = txt_device_upgrade_payment(
            old_limit=calc['old_limit'],
            new_limit=calc['new_limit'],
            new_limit_word=new_limit_word,
            price=_format_price(calc['price']),
            template=app_conf.get('text_device_upgrade_payment'),
        )
        await query.message.edit_text(
            text,
            reply_markup=keyboards.get_yoomoney_payment_keyboard(pay_url, payment_id),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )
        await query.answer()
        logger.info(
            f"[DEVICE_UPGRADE] user={user_id} created YooMoney payment: "
            f"{payment_id} for limit {calc['old_limit']}->{new_limit} = {calc['price']}₽"
        )
        return

    elif provider == 'cryptobot':
        token = app_conf.get('cryptobot_token')
        if not token:
            await query.message.edit_text(
                "❌ CryptoBot не настроен. Обратитесь в поддержку.",
                reply_markup=_back_kbd(),
            )
            await query.answer()
            return

        from src.pay import create_cryptobot_invoice_device_upgrade
        result = await create_cryptobot_invoice_device_upgrade(
            token=str(token),
            user_id=user_id,
            new_limit=new_limit,
            upgrade_meta=upgrade_meta,
            return_url=return_url,
        )
        if result:
            payment_id, pay_url, payload_str = result

    elif provider == 'tgstar':
        # Для XTR provider_token не нужен — передаём пустую строку.
        provider_token = app_conf.get('tgstar_provider_token') or ''

        from src.pay import rub_to_stars
        rub_amount = float(upgrade_meta.get('price') or 0)
        stars_amount = rub_to_stars(rub_amount, app_conf)
        rub_str = int(rub_amount) if rub_amount == int(rub_amount) else f"{rub_amount:.2f}"

        # Кодируем metadata в payload — потребуется в process_tgstar_payment
        # для применения upgrade'а после успешной оплаты.
        tg_payload = json.dumps({
            'op': 'device_upgrade',
            'user_id': user_id,
            'new_limit': new_limit,
            'price': rub_amount,
        }, ensure_ascii=False)
        # Telegram-инвойс ограничивает payload 128 байтами — оборачиваем в
        # короткую строку с маркером, расшифровка идёт по содержимому.
        payload_short = f"tgstar_device_upgrade:{user_id}:{new_limit}"

        try:
            await query.message.delete()
        except Exception:
            pass

        await bot.send_invoice(
            chat_id=user_id,
            title=f"Расширение лимита до {new_limit} устр.",
            description=(
                f"Новый лимит устройств: {new_limit}\n\n"
                f"{rub_str} ₽ ≈ {stars_amount} ⭐"
            ),
            payload=payload_short,
            provider_token=str(provider_token),
            currency='XTR',
            prices=[LabeledPrice(label=f"Лимит {new_limit} устр.", amount=stars_amount)],
            start_parameter='device_upgrade',
        )
        await bot.send_message(
            chat_id=user_id,
            text=(
                "⬆️ Счёт для оплаты создан.\n\n"
                "Если передумали — нажмите кнопку ниже, чтобы вернуться."
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [btn('btn_back', callback_data='device_upgrade_start')],
            ]),
        )
        await query.answer()
        logger.info(
            f"[DEVICE_UPGRADE] user={user_id} TG Star invoice sent, "
            f"new_limit={new_limit}, {rub_str}₽ → {stars_amount}⭐"
        )
        # Возврат — пометить, что для tgstar инвойс уже отправлен, дальнейшая
        # логика с pay_url не нужна. Применение лимита — в process_tgstar_payment.
        _ = tg_payload  # для совместимости/будущих расширений
        return

    else:
        await query.answer('Неизвестный способ оплаты.', show_alert=True)
        return

    if not pay_url:
        await query.message.edit_text(
            "❌ Не удалось создать платёж. Попробуйте позже или обратитесь в поддержку.",
            reply_markup=_back_kbd(),
        )
        await query.answer()
        return

    kbd = InlineKeyboardMarkup(inline_keyboard=[
        [btn('btn_payment_pay_link', url=pay_url)],
        [btn('btn_back_to_main', callback_data='back_to_main')],
    ])
    new_limit_word = _plural_devices(calc['new_limit'])

    text = txt_device_upgrade_payment(
        old_limit=calc['old_limit'],
        new_limit=calc['new_limit'],
        new_limit_word=new_limit_word,
        price=_format_price(calc['price']),
        template=app_conf.get('text_device_upgrade_payment'),
    )

    # Пользовательское соглашение — показываем только если ссылка задана в настройках.
    agreement_link = (app_conf.get('web_user_agreement_link', '') or '').strip()
    if agreement_link:
        agreement_text = app_conf.get(
            'btn_user_agreement', '📄 Пользовательское соглашение'
        ) or '📄 Пользовательское соглашение'
        # html.escape нужен, чтобы кавычки/амперсанды в URL не сломали Telegram-парсер.
        import html as _html
        safe_url = _html.escape(agreement_link, quote=True)
        safe_text = _html.escape(agreement_text)
        text += (
            "\n\n<blockquote>"
            f"<i>💳 Расширяя лимит, вы принимаете</i> "
            f"<a href=\"{safe_url}\">{safe_text}</a>"
            "</blockquote>"
        )

    try:
        await query.message.edit_text(
            text,
            reply_markup=kbd,
            parse_mode='HTML',
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"[DEVICE_UPGRADE] не удалось отредактировать сообщение: {e}")
    await query.answer()
    logger.info(
        f"[DEVICE_UPGRADE] user={user_id} created payment via {provider}: "
        f"{payment_id} for limit {calc['old_limit']}->{new_limit} = {calc['price']}₽"
    )


# ────────────────────────────────────────────────────────────────────────────
# Регистрация
# ────────────────────────────────────────────────────────────────────────────

def register_device_upgrade_handlers(dp: Dispatcher):
    """Подключает все callback'и расширения лимита устройств."""
    dp.callback_query.register(cmd_device_upgrade_start, F.data == 'device_upgrade_start')
    dp.callback_query.register(cmd_device_upgrade_select, F.data.startswith('device_upgrade_select:'))
    dp.callback_query.register(cmd_device_upgrade_pay, F.data.startswith('device_upgrade_pay:'))
