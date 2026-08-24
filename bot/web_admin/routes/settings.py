from quart import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app, session
import json
import asyncio
import logging
from datetime import datetime, timedelta
import pytz
import os
import base64
import httpx
import tempfile

from app_config import app_conf
from web_admin.core.sqlite_hot_backup import build_backup_zip
from web_admin.core.s3_uploader import (
    S3NotConfigured,
    S3UploadError,
    upload_file_async,
)
from src.telegram_html import is_html_text_setting_key, validate_telegram_html
from src.telegram_bot_factory import make_aiogram_bot
from tg_sender import clear_bot_token_cache
import uuid
from functools import wraps

logger = logging.getLogger(__name__)

# Импортируем login_required из run.py
# Декоратор будет доступен через замыкание при вызове attach_settings_routes

# Глобальный словарь для хранения статуса задач добавления клиентов
# Формат: {server_id: {'status': 'running'|'completed'|'error', 'progress': 0-100, 'current': 0, 'total': 0, 'added': 0, 'failed': 0, 'message': ''}}
_location_add_tasks = {}


def _devices_db_path() -> str | None:
    """Путь к devices.db. None, если модуль не установлен / БД не найдена.

    Берём DEVICES_DB_PATH из devices.config, а не хардкодим путь — так бэкап
    останется в ладу с возможным переносом модуля.
    """
    try:
        from devices.config import DEVICES_DB_PATH  # lazy import: devices не везде есть
        return os.path.abspath(DEVICES_DB_PATH)
    except Exception:
        return None

# Глобальный словарь для хранения статуса задач синхронизации
# Формат: {server_id: SyncResult}
_sync_tasks = {}


async def reload_bot_settings():
    """Отправляет локальный запрос боту для перезагрузки настроек."""
    bot_api_url = 'http://127.0.0.1:8081/api/reload-settings'
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(bot_api_url)
            resp.raise_for_status()
            current_app.logger.info(f"Настройки бота успешно перезагружены через API: {resp.json()}")
    except httpx.HTTPStatusError as e:
        current_app.logger.error(f"Ошибка HTTP при перезагрузке настроек бота: {e.response.status_code} - {e.response.text}")
    except httpx.RequestError as e:
        current_app.logger.error(f"Ошибка запроса при перезагрузке настроек бота: {e}")
    except Exception as e:
        current_app.logger.error(f"Неизвестная ошибка при перезагрузке настроек бота: {e}")


def attach_settings_routes(admin_bp_instance, query_db_func, execute_db_func):
    logger.info("[SETTINGS_ROUTES] Регистрация маршрутов для settings (Remnawave-only)")
    
    @admin_bp_instance.route('/settings/general', methods=['GET', 'POST'])
    async def settings_general():
        # Импортируем асинхронные обертки
        from web_admin.async_db import async_execute_db, async_query_db
        
        if request.method == 'POST':
            form = await request.form
            toggle_button_keys = ['show_payment_yookassa', 'show_payment_tgstar', 'show_payment_cryptobot',
                                 'show_payment_yoomoney', 'show_payment_promo_code', 'show_payment_manual',
                                 'show_payment_platega', 'show_payment_wata', 'show_partner_program_button',
                                 'yookassa_sbp_only', 'remnawave_show_traffic_stub',
                                 'bot_rate_limit_enabled', 'bot_maintenance_enabled', 'show_website_button', 'admin_ip_whitelist_enabled']
            
            # Обработка метода защиты при регистрации (select вместо чекбоксов)
            current_app.logger.info(f"[SETTINGS] Форма содержит ключи: {list(form.keys())}")
            if 'registration_protection_method' in form:
                protection_method = form.get('registration_protection_method', '').strip()
                current_app.logger.info(f"[SETTINGS] Сохранение метода защиты: '{protection_method}'")
                
                if protection_method:
                    # Проверяем существование записей и создаем их, если нужно
                    bot_protection_exists = await async_query_db("SELECT 1 FROM settings WHERE key = 'bot_protection_enabled'", (), one=True)
                    channel_subscription_exists = await async_query_db("SELECT 1 FROM settings WHERE key = 'channel_subscription_enabled'", (), one=True)
                    
                    if not bot_protection_exists:
                        await async_execute_db("INSERT INTO settings (key, value, description) VALUES (?, ?, ?)", 
                                              ('bot_protection_enabled', '0', 'Включить защиту от ботов при регистрации (0/1)'))
                    if not channel_subscription_exists:
                        await async_execute_db("INSERT INTO settings (key, value, description) VALUES (?, ?, ?)", 
                                              ('channel_subscription_enabled', '0', 'Включить обязательную подписку на канал при регистрации (0/1)'))
                    
                    # Сначала отключаем оба метода, потом включаем нужный
                    await async_execute_db("UPDATE settings SET value = ? WHERE key = ?", ('0', 'bot_protection_enabled'))
                    await async_execute_db("UPDATE settings SET value = ? WHERE key = ?", ('0', 'channel_subscription_enabled'))
                    
                    if protection_method == 'bot_protection':
                        await async_execute_db("UPDATE settings SET value = ? WHERE key = ?", ('1', 'bot_protection_enabled'))
                        current_app.logger.info("[SETTINGS] Установлена защита от ботов")
                    elif protection_method == 'channel_subscription':
                        await async_execute_db("UPDATE settings SET value = ? WHERE key = ?", ('1', 'channel_subscription_enabled'))
                        current_app.logger.info("[SETTINGS] Установлена подписка на канал")
                    elif protection_method == 'none':
                        current_app.logger.info("[SETTINGS] Защита отключена")
                    
                    # Проверяем результат
                    bot_check = await async_query_db("SELECT value FROM settings WHERE key = 'bot_protection_enabled'", (), one=True)
                    channel_check = await async_query_db("SELECT value FROM settings WHERE key = 'channel_subscription_enabled'", (), one=True)
                    current_app.logger.info(f"[SETTINGS] После сохранения: bot_protection={bot_check['value'] if bot_check else 'N/A'}, channel_subscription={channel_check['value'] if channel_check else 'N/A'}")
                else:
                    current_app.logger.warning("[SETTINGS] Значение 'registration_protection_method' пустое!")
            else:
                current_app.logger.warning("[SETTINGS] Ключ 'registration_protection_method' не найден в форме!")

            # Обработка настроек модератора (только для админа)
            if session.get('admin_role') == 'admin':
                import json as _json
                mod_pwd = form.get('moderator_web_password', '').strip()
                mod_sections = [k.replace('moderator_section_', '') for k in form.keys() if k.startswith('moderator_section_')]
                # Пароль: обновляем только если введён (пустое поле = не менять)
                if 'moderator_web_password' in form:
                    await async_execute_db(
                        "INSERT OR REPLACE INTO settings (key, value, description) VALUES (?, ?, ?)",
                        ('moderator_web_password', mod_pwd, 'Пароль для входа модератора в веб-админку')
                    )
                await async_execute_db(
                    "INSERT OR REPLACE INTO settings (key, value, description) VALUES (?, ?, ?)",
                    ('moderator_sections', _json.dumps(mod_sections), 'Разделы, видимые модератору (JSON)')
                )

            # ── Валидация admin_ip_whitelist + anti-lockout ──────────────────
            # Если пользователь хочет включить whitelist, но его текущий IP не
            # входит в список — отказываем (иначе он сам себя отрежет от админки).
            try:
                from web_admin.core.ip_whitelist import (
                    parse_cidr_list, resolve_client_ip,
                    is_ip_allowed, is_loopback,
                )
                _wl_raw = (form.get('admin_ip_whitelist') or '').strip()
                _wl_enabled_form = ('admin_ip_whitelist_enabled' in form)
                _wl_nets, _wl_errors = parse_cidr_list(_wl_raw)
                if _wl_errors:
                    await flash(
                        'IP whitelist: некорректные значения, исправьте: '
                        + ', '.join(_wl_errors[:5]),
                        'danger',
                    )
                    return redirect(url_for('admin.settings_general'))
                if _wl_enabled_form:
                    _client_ip = resolve_client_ip(request)
                    if (not is_loopback(_client_ip)) and (not is_ip_allowed(_client_ip, _wl_nets)):
                        await flash(
                            'IP whitelist не включён: ваш текущий IP '
                            f'{_client_ip} не входит в список. Добавьте его в правила, '
                            'затем включите тумблер.',
                            'danger',
                        )
                        return redirect(url_for('admin.settings_general'))
            except Exception as _wl_save_e:
                current_app.logger.warning(f"[WHITELIST] save validation error: {_wl_save_e}")

            for key, value in form.items():
                # Приманки автозаполнения из формы основных настроек (не ключи settings)
                if key.startswith('gen_honey_'):
                    continue
                if key == 'telegram_proxy_url':
                    value = (value or '').strip()
                # admin_ip_whitelist — нормализуем перед сохранением.
                if key == 'admin_ip_whitelist':
                    value = (value or '').replace(';', ',').replace('\n', ',')
                    parts = [p.strip() for p in value.split(',') if p and p.strip()]
                    value = ', '.join(parts)
                # Пропускаем скрытые поля защиты - они обрабатываются отдельно выше
                if key in ['bot_protection_enabled', 'channel_subscription_enabled', 'registration_protection_method']:
                    continue
                # Пропускаем настройки модератора — обработаны выше
                if key == 'moderator_web_password' or key.startswith('moderator_section_'):
                    continue
                # Legacy: больше не редактируется в UI
                if key == 'subscription_provider':
                    continue
                if key in (
                    'cleanup_disabled_clients_enabled',
                    'server_health_check_enabled',
                    'server_health_check_interval_minutes',
                ) or key.startswith('cleanup_'):
                    continue
                if key not in toggle_button_keys:
                    # Защита от случайного удаления токена бота
                    if key == 'bot_token':
                        # Если значение пустое или состоит только из пробелов, пропускаем обновление
                        if not value or not value.strip():
                            current_app.logger.warning("[SETTINGS] Попытка сохранить пустой токен бота - обновление пропущено")
                            continue
                        clear_bot_token_cache()

                    if key == 'wata_access_token':
                        if not value or not str(value).strip():
                            current_app.logger.warning(
                                "[SETTINGS] Пустой wata_access_token — обновление пропущено (оставлен прежний токен)"
                            )
                            continue
                    
                    # При изменении пароля админа сбрасываем секретный ключ сессий
                    if key == 'admin_web_password':
                        # Получаем старое значение пароля
                        old_password_row = await async_query_db("SELECT value FROM settings WHERE key = ?", (key,))
                        old_password = old_password_row[0]['value'] if old_password_row and old_password_row[0] else None
                        
                        # Если пароль изменился, генерируем новый секретный ключ
                        if old_password != value:
                            new_secret_key = os.urandom(32)
                            secret_key_b64 = base64.b64encode(new_secret_key).decode('utf-8')
                            await async_execute_db("UPDATE settings SET value = ? WHERE key = ?", (secret_key_b64, 'web_admin_secret_key'))
                            current_app.logger.info("[SETTINGS] Пароль админа изменен, секретный ключ сессий сброшен")
                    
                    await async_execute_db("UPDATE settings SET value = ? WHERE key = ?", (value, key))

            for key in toggle_button_keys:
                if key in form:
                    await async_execute_db("UPDATE settings SET value = ? WHERE key = ?", ('1', key))
                else:
                    await async_execute_db("UPDATE settings SET value = ? WHERE key = ?", ('0', key))

            # Перезагружаем кэш настроек после сохранения
            try:
                await app_conf.load_settings()
            except Exception as e:
                current_app.logger.error(f"Ошибка перезагрузки кэша настроек: {e}")

            # Сбрасываем кэш IP-whitelist, чтобы изменения применились мгновенно
            try:
                from web_admin.core.ip_whitelist import invalidate_cache as _wl_invalidate
                _wl_invalidate()
            except Exception:
                pass

            # Проверяем сохраненные значения защиты после всех операций
            if 'registration_protection_method' in form:
                bot_check_final = await async_query_db("SELECT value FROM settings WHERE key = 'bot_protection_enabled'", (), one=True)
                channel_check_final = await async_query_db("SELECT value FROM settings WHERE key = 'channel_subscription_enabled'", (), one=True)
                current_app.logger.info(f"[SETTINGS] Финальная проверка после сохранения: bot_protection={bot_check_final['value'] if bot_check_final else 'N/A'}, channel_subscription={channel_check_final['value'] if channel_check_final else 'N/A'}")
            
            # Отправляем команду боту на перезагрузку настроек (только локально)
            try:
                bot_api_url = 'http://127.0.0.1:8081/api/reload-settings'
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.post(bot_api_url)
                    if response.status_code == 200:
                        current_app.logger.info("Настройки бота успешно перезагружены через API")
                    else:
                        current_app.logger.warning(f"Не удалось перезагрузить настройки бота: HTTP {response.status_code}")
            except Exception as e:
                current_app.logger.warning(f"Ошибка при вызове API перезагрузки настроек бота: {e}")

            await flash('Основные настройки успешно обновлены!', 'success')
            r = redirect(url_for('admin.settings_general') + '#saved')
            r.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            return r

        settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")

        try:
            exists = await async_query_db("SELECT 1 FROM settings WHERE key = 'trial_limit_ip'", (), one=True)
            if not exists:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    ('trial_limit_ip', '1', 'Лимит устройств при пробном периоде (0 = без лимита)')
                )
                settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception:
            pass

        try:
            exists_proxy = await async_query_db("SELECT 1 FROM settings WHERE key = 'telegram_proxy_url'", (), one=True)
            if not exists_proxy:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    (
                        'telegram_proxy_url',
                        '',
                        'Прокси для исходящих запросов бота и админки к api.telegram.org (HTTP/HTTPS/SOCKS). Пример: socks5://127.0.0.1:1080 или http://user:pass@host:8080. Пусто — без прокси.',
                    ),
                )
                settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception:
            pass

        # default_subscription_mode убран - все пользователи создаются на всех серверах

        # Автоинициализация ссылки на канал бота (для динамической кнопки "Наш канал")
        try:
            exists_channel = await async_query_db("SELECT 1 FROM settings WHERE key = 'bot_channel_link'", (), one=True)
            if not exists_channel:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    ('bot_channel_link', '', 'Ссылка на канал бота (показывает кнопку \"Наш канал\" в главном меню). Текст кнопки редактируется ниже.')
                )
                settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception:
            pass

        
        # Автоинициализация настроек модератора
        try:
            for mkey, mval, mdesc in [
                ('moderator_web_password', '', 'Пароль для входа модератора в веб-админку'),
                ('moderator_sections', '["dashboard","users","payments"]', 'Разделы, видимые модератору (JSON)'),
            ]:
                exists_m = await async_query_db("SELECT 1 FROM settings WHERE key = ?", (mkey,), one=True)
                if not exists_m:
                    await async_execute_db(
                        "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                        (mkey, mval, mdesc)
                    )
        except Exception:
            pass

        # Автоинициализация кастомного URL (для динамической кнопки "КастомURL")
        try:
            exists_custom_url = await async_query_db("SELECT 1 FROM settings WHERE key = 'bot_custom_url'", (), one=True)
            if not exists_custom_url:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    ('bot_custom_url', '', 'Кастомный URL (показывает кнопку \"КастомURL\" в главном меню). Текст кнопки редактируется ниже.')
                )
                settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception:
            pass

        # Автодобавление недостающих ключей для методов оплаты (включая Platega)
        try:
            ensure = [
                ('show_payment_manual', '0', 'Показывать метод оплаты Переводом (0/1)'),
                ('manual_transfer_link_30', '', 'Ссылка для оплаты перевода на 30 дней'),
                ('manual_transfer_link_60', '', 'Ссылка для оплаты перевода на 60 дней'),
                ('manual_transfer_link_90', '', 'Ссылка для оплаты перевода на 90 дней'),
                ('show_payment_platega', '0', 'Показывать метод оплаты Platega (СБП/QR)'),
                ('platega_merchant_id', '', 'Platega: MerchantId'),
                ('platega_api_secret', '', 'Platega: API Secret'),
                ('show_payment_wata', '0', 'Показывать метод оплаты Wata (карты / СБП по терминалу токена)'),
                ('wata_access_token', '', 'Wata: Access Token (Bearer JWT из ЛК терминала; см. ENV WATA_ACCESS_TOKEN)'),
                ('wata_terminal_public_id', '', 'Wata: Terminal Public ID (UUID терминала; виджет, см. ENV WATA_TERMINAL_PUBLIC_ID)'),
                ('platega_enabled_methods', '[2]', '⚠️ DEPRECATED: больше не используется. С переходом на Platega v2 endpoint метод оплаты выбирает плательщик на странице Platega. Настройка оставлена для обратной совместимости и будет удалена в будущих версиях.'),
                ('btn_partner_program', '🤝 Партнёрская программа', 'Текст кнопки Партнёрская программа'),
                ('show_partner_program_button', '1', 'Показывать кнопку Партнёрская программа (0/1)'),

                ('web_user_agreement_link', '', 'Ссылка на Пользовательское соглашение (кнопка в О сервисе). Текст кнопки редактируется ниже.'),
                ('web_privacy_policy_link', '', 'Ссылка на Политику конфиденциальности (кнопка в О сервисе). Текст кнопки редактируется ниже.'),
                ('support_link', '', 'Ссылка на поддержку. Текст кнопки редактируется ниже.'),
                ('support_custom_link', '', 'Кастомная ссылка поддержки (отображается над основной кнопкой поддержки). Текст кнопки редактируется ниже.'),
                ('btn_support_link', '💬 Перейти в поддержку', 'Текст кнопки перехода в поддержку (для ссылки выше)'),
                ('btn_support_custom_link', '📞 Кастомная поддержка', 'Текст кнопки кастомной ссылки поддержки (для ссылки выше)'),
                ('sub_page_url', '', 'URL страницы подписки (для формирования ссылок на подписку).'),
            ]
            existing_keys_all = [r['key'] for r in settings_rows]
            for k, v, d in ensure:
                if k not in existing_keys_all:
                    await async_execute_db("INSERT INTO settings (key, value, description) VALUES (?, ?, ?)", (k, v, d))
            # перечитать после возможных вставок
            settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception:
            pass

        # Автоинициализация токена 2ip.ru для обогащения IP-адресов
        try:
            exists_2ip = await async_query_db("SELECT 1 FROM settings WHERE key = 'web_2ip_token'", (), one=True)
            if not exists_2ip:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    ('web_2ip_token', '', 'Токен API 2ip.ru для обогащения сведений об IP (опционально)')
                )
                settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception:
            pass

        # Автоинициализация настройки YooKassa: только СБП
        try:
            exists_sbp = await async_query_db("SELECT 1 FROM settings WHERE key = 'yookassa_sbp_only'", (), one=True)
            if not exists_sbp:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    ('yookassa_sbp_only', '0', 'YooKassa: принимать только СБП (выкл/вкл)')
                )
                settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception:
            pass

        # Автоинициализация кнопки «Доступ к сайту»
        try:
            exists_swb = await async_query_db("SELECT 1 FROM settings WHERE key = 'show_website_button'", (), one=True)
            if not exists_swb:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    ('show_website_button', '0', 'Показывать кнопку «Доступ к сайту» в главном меню бота (0/1)')
                )
                settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception:
            pass

        # Автоинициализация настроек партнёрской программы
        try:
            existing_keys_all = [r['key'] for r in settings_rows]
            partner_defaults = [
                ('partner_percent_rub', '10', 'Процент партнёра в RUB от успешных платежей приглашённых (0-100)'),
                ('partner_min_withdraw_rub', '500', 'Минимальная сумма вывода партнёрского баланса в ₽ (кнопка в боте)'),
                ('bot_username', '', 'Имя Telegram-бота без @ для формирования ссылки партнёра'),
            ]
            for k, v, d in partner_defaults:
                if k not in existing_keys_all:
                    await async_execute_db("INSERT INTO settings (key, value, description) VALUES (?, ?, ?)", (k, v, d))
            # перечитать после возможных вставок
            settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception:
            pass
        
        # Автоинициализация настроек Remnawave
        try:
            existing_keys_all = [r['key'] for r in settings_rows]
            remnawave_keys = [
                ('remnawave_base_url', '', 'Remnawave: Base URL (например: https://rem.tunnel.ru)'),
                ('remnawave_api_token', '', 'Remnawave: API Token (JWT токен)'),
                ('remnawave_default_internal_squad_uuid', '', 'Remnawave: UUID внутреннего сквада по умолчанию'),
                ('remnawave_default_traffic_limit_gb', '0', 'Remnawave: Лимит трафика по умолчанию в ГБ (0 = безлимит)'),
                ('remnawave_enabled', '0', 'Включить Remnawave (0/1)'),
                ('remnawave_auto_traffic_renewal_enabled', '1', 'Автоматически продлевать трафик при продлении подписки Remnawave (0/1)'),
                ('traffic_renewal_enabled', '1', 'Включить платное продление трафика для Remnawave (0/1)'),
                ('remnawave_webhook_enabled', '0', 'Принимать вебхуки от Remnawave'),
                ('remnawave_health_check_enabled', '1', 'Проверять доступность панели Remnawave и уведомлять администратора (0/1)'),
                ('remnawave_webhook_secret', '', 'Remnawave: Секретный ключ для вебхуков (используется для проверки подписи)'),
                ('remnawave_traffic_exhausted_squad_uuid', '', 'Remnawave: UUID squad для смены LIMIT на NO LIMIT(если пусто, squad не меняется)'),
            ]
            for k, v, d in remnawave_keys:
                if k not in existing_keys_all:
                    await async_execute_db("INSERT INTO settings (key, value, description) VALUES (?, ?, ?)", (k, v, d))
            # перечитать после возможных вставок
            settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception:
            pass
        
        # Автоинициализация настройки бесплатного продления
        try:
            existing_keys_all = [r['key'] for r in settings_rows]
            if 'free_renewal_days' not in existing_keys_all:
                await async_execute_db("INSERT INTO settings (key, value, description) VALUES (?, ?, ?)", ('free_renewal_days', '3', 'Количество дней для бесплатного продления подписки через кнопку "Продлить подписку бесплатно" в новостях'))
            # перечитать после возможной вставки
            settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception:
            pass
        
        # Автоинициализация ключей IP-whitelist веб-админки
        try:
            existing_keys_all = [r['key'] for r in settings_rows]
            ipw_keys = [
                ('admin_ip_whitelist_enabled', '0',
                 'Включить ограничение доступа в веб-админку по IP-адресам (0/1). '
                 'При включении: запросы извне белого списка получают 404. '
                 'Loopback (127.0.0.1, ::1) разрешён всегда. Сброс через SQLite: '
                 "UPDATE settings SET value='0' WHERE key='admin_ip_whitelist_enabled'"),
                ('admin_ip_whitelist', '',
                 'Список разрешённых IP/CIDR через запятую '
                 '(например: 1.2.3.4, 5.6.7.0/24, 2001:db8::/32). '
                 'Применяется только если admin_ip_whitelist_enabled=1.'),
            ]
            for k, v, d in ipw_keys:
                if k not in existing_keys_all:
                    await async_execute_db(
                        "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                        (k, v, d),
                    )
            settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception:
            pass

        # Автоинициализация настроек rate limiting
        try:
            existing_keys_all = [r['key'] for r in settings_rows]
            rate_limit_keys = [
                ('bot_rate_limit_enabled', '1', 'Включить ограничение частоты запросов (rate limiting) для защиты от спама'),
                ('bot_rate_limit_message_max', '10', 'Максимальное количество сообщений от одного пользователя за период (rate limiting)'),
                ('bot_rate_limit_message_window', '60', 'Период времени в секундах для ограничения сообщений (rate limiting)'),
                ('bot_rate_limit_callback_max', '30', 'Максимальное количество нажатий кнопок от одного пользователя за период (rate limiting)'),
                ('bot_rate_limit_callback_window', '60', 'Период времени в секундах для ограничения нажатий кнопок (rate limiting)'),
                ('bot_rate_limit_message_text', '⏳ Слишком много сообщений. Пожалуйста, подождите немного перед следующим запросом.', 'Текст сообщения при превышении лимита запросов'),
            ]
            for k, v, d in rate_limit_keys:
                if k not in existing_keys_all:
                    await async_execute_db("INSERT INTO settings (key, value, description) VALUES (?, ?, ?)", (k, v, d))
            # перечитать после возможных вставок
            settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception:
            pass

        # Автоинициализация сервисного режима бота
        try:
            existing_keys_all = [r['key'] for r in settings_rows]
            maintenance_keys = [
                ('bot_maintenance_enabled', '0', 'Сервисный режим: бот отвечает заглушкой на сообщения и кнопки (0/1)'),
                ('bot_maintenance_message', 'К сожалению, бот находится на технических работах. Попробуйте позже.', 'Текст заглушки сервисного режима'),
            ]
            for k, v, d in maintenance_keys:
                if k not in existing_keys_all:
                    await async_execute_db("INSERT INTO settings (key, value, description) VALUES (?, ?, ?)", (k, v, d))
            settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception:
            pass

        # Автоинициализация настроек платежей (для существующих БД)
        try:
            existing_keys_all = [r['key'] for r in settings_rows]
            payment_extra_keys = [
                ('tgstar_rub_per_star', '2.0',
                 'Курс TG Stars: сколько ₽ за 1 ⭐. Используется для конвертации '
                 'рублёвых тарифов в звёзды при создании инвойса. Пример: при 2.0 — '
                 'тариф 1000 ₽ → 500 ⭐.'),
            ]
            for k, v, d in payment_extra_keys:
                if k not in existing_keys_all:
                    await async_execute_db("INSERT INTO settings (key, value, description) VALUES (?, ?, ?)", (k, v, d))
            settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception:
            pass

        grouped_settings = {
            "🤖 Настройки бота": [],
            "🔗 Ссылки": [],
            "💳 Методы оплаты": [],
            "👥 Реферальная система": [],
            "💠 Партнёрская программа": [],
            "⚙️ Системные настройки": [],
            "🔒 Безопасность": [],
        }

        for setting in settings_rows:
            # Только UI: ключ остаётся в БД и в app_conf
            if setting['key'] == 'platega_enabled_methods':
                continue
            # IP-whitelist рендерится в отдельной карточке внутри «🔒 Безопасность»
            # (см. шаблон settings_general.html, блок ipw-card) — не дублируем.
            if setting['key'] in ('admin_ip_whitelist', 'admin_ip_whitelist_enabled'):
                continue
            # Legacy: провайдер подписки (X-UI/Remnawave) — проект только Remnawave
            if setting['key'] == 'subscription_provider':
                continue
            # Legacy 3X-UI: фоновые задачи удалены из планировщика
            if setting['key'] in (
                'cleanup_disabled_clients_enabled',
                'server_health_check_enabled',
                'server_health_check_interval_minutes',
            ) or setting['key'].startswith('cleanup_'):
                continue
            if (setting['key'].startswith('show_payment_') or
                setting['key'].startswith('yookassa_') or
                (setting['key'].startswith('tgstar_') and setting['key'] != 'tgstar_provider_token') or
                setting['key'].startswith('cryptobot_') or
                setting['key'].startswith('yoomoney_') or
                setting['key'].startswith('platega_') or
                setting['key'].startswith('wata_') or
                setting['key'].startswith('manual_')):
                grouped_settings["💳 Методы оплаты"].append(setting)
            elif (setting['key'].startswith('ref_') or setting['key'].startswith('referral_')):
                grouped_settings["👥 Реферальная система"].append(setting)
            elif setting['key'] in ('partner_percent_rub', 'partner_min_withdraw_rub', 'bot_username', 'show_partner_program_button'):
                grouped_settings["💠 Партнёрская программа"].append(setting)
            elif setting['key'] in ('bot_channel_link', 'bot_custom_url', 'web_privacy_policy_link',
                                    'web_user_agreement_link', 'support_link', 'support_custom_link'):
                # Только URL; подписи кнопок — в /settings/buttons
                grouped_settings["🔗 Ссылки"].append(setting)
            elif (setting['key'].startswith('bot_rate_limit') or
                  setting['key'] == 'bot_protection_enabled'):
                # Тексты защиты от ботов — в /settings/texts → «Защита от ботов»
                grouped_settings["🔒 Безопасность"].append(setting)
            elif setting['key'].startswith('bot_maintenance'):
                grouped_settings["⚙️ Системные настройки"].append(setting)
            elif (setting['key'].startswith('bot_') or setting['key'] in (
                    'project_name','trial_days','trial_limit_ip','free_renewal_days','admin_ids','connect_page_url','sub_page_url') or
                  (setting['key'].startswith('show_') and not setting['key'].startswith('show_payment_'))):
                if setting['key'] not in ('bot_protection_text', 'bot_protection_success_text', 'bot_protection_wrong_text',
                                         'show_change_server_button', 'show_multi_server_button',
                                         'bot_channel_link', 'bot_custom_url', 'bot_protection_enabled',
                                         'bot_rate_limit_enabled', 'bot_rate_limit_message_max', 'bot_rate_limit_message_window',
                                         'bot_rate_limit_callback_max', 'bot_rate_limit_callback_window', 'bot_rate_limit_message_text'):
                    grouped_settings["🤖 Настройки бота"].append(setting)
            elif (setting['key'].startswith('remnawave_') or
                  setting['key'] == 'traffic_renewal_enabled'):
                pass  # раздел «Панели → Remnawave → Настройки»
            elif ((setting['key'].startswith('admin_') and setting['key'] not in ['admin_text_promo_code_created','admin_text_promo_codes_menu','admin_ids']) or
                  (setting['key'].startswith('web_') and setting['key'] not in ('web_page_link','web_admin_port')) or
                  setting['key'] in ('email_domain', 'telegram_proxy_url')):
                grouped_settings["⚙️ Системные настройки"].append(setting)

        def sort_payment_settings(settings_list):
            order = {
                'show_payment_yookassa': 1,
                'show_payment_tgstar': 2,
                'tgstar_rub_per_star': 3,
                'show_payment_cryptobot': 4,
                'show_payment_yoomoney': 5,
                'show_payment_promo_code': 6,
                'show_payment_platega': 7,
                'show_payment_wata': 8,
                'yookassa_shop_id': 9,
                'yookassa_secret_key': 10,
                'cryptobot_token': 11,
                'yoomoney_token': 12,
                'yoomoney_account': 13,
                'yoomoney_notification_secret': 14,
                'platega_merchant_id': 15,
                'platega_api_secret': 16,
                'wata_access_token': 17,
                'wata_terminal_public_id': 18,
            }
            return sorted(settings_list, key=lambda s: order.get(s['key'], 999))

        def sort_links_settings(settings_list):
            """Порядок полей только с URL (кнопки — в /settings/buttons)."""
            order = {
                'bot_channel_link': 1,
                'bot_custom_url': 2,
                'web_user_agreement_link': 3,
                'web_privacy_policy_link': 4,
                'support_link': 5,
                'support_custom_link': 6,
            }
            return sorted(settings_list, key=lambda s: order.get(s['key'], 999))

        def sort_security_settings(settings_list):
            """Сортирует настройки безопасности (без текстов капчи — они в /settings/texts)."""
            order = {
                'bot_protection_enabled': 1,
                'bot_rate_limit_enabled': 2,
                'bot_rate_limit_message_max': 3,
                'bot_rate_limit_message_window': 4,
                'bot_rate_limit_callback_max': 5,
                'bot_rate_limit_callback_window': 6,
                'bot_rate_limit_message_text': 7,
            }
            return sorted(settings_list, key=lambda s: order.get(s['key'], 999))

        def sort_bot_settings(settings_list):
            """Сортирует настройки бота, чтобы trial_days и free_renewal_days были рядом"""
            order = {
                'project_name': 1,
                'trial_days': 2,
                'free_renewal_days': 3,
                'trial_limit_ip': 4,
                'show_website_button': 5,
                'connect_page_url': 6,
                'sub_page_url': 7,
            }
            # Сортируем: сначала по порядку из словаря, потом по алфавиту
            return sorted(settings_list, key=lambda s: (order.get(s['key'], 999), s['key']))

        def sort_system_settings(settings_list):
            order = {
                'bot_maintenance_enabled': 0,
                'bot_maintenance_message': 1,
            }
            return sorted(settings_list, key=lambda s: (order.get(s['key'], 999), s['key']))

        grouped_settings["💳 Методы оплаты"] = sort_payment_settings(grouped_settings["💳 Методы оплаты"])
        grouped_settings["🔗 Ссылки"] = sort_links_settings(grouped_settings["🔗 Ссылки"])
        grouped_settings["🔒 Безопасность"] = sort_security_settings(grouped_settings["🔒 Безопасность"])
        grouped_settings["🤖 Настройки бота"] = sort_bot_settings(grouped_settings["🤖 Настройки бота"])
        grouped_settings["⚙️ Системные настройки"] = sort_system_settings(grouped_settings["⚙️ Системные настройки"])

        # ── Карточки платёжных провайдеров для settings_general.html ─────────
        # Шаблон рисует группы (иконка + master-тумблер + поля) вместо плоского
        # списка. master-тумблер вынесен в head карточки (включить/выключить
        # провайдер), остальные ключи — в body. Иконки лежат в static/play/.
        # ВАЖНО: ключ структуры называется 'fields', а не 'keys', иначе в Jinja
        # `grp.keys` подставит метод dict.keys (а не наш массив).
        # webhook_path: совпадает с тем, что main.py выводит при старте бота
        # (см. main.py: «logger.info(... YooKassa: {connect_url}/yookassa/webhook ...)»).
        # У Remnawave своя страница «Панели → Remnawave», поэтому здесь его нет.
        _PAY_GROUPS_DEF = [
            {'id': 'yookassa',  'name': 'YooKassa',       'icon': 'play/yookassa.svg',
             'sub': 'Карты · СБП',                'master': 'show_payment_yookassa',
             'fields': ['yookassa_sbp_only', 'yookassa_shop_id', 'yookassa_secret_key'],
             'webhook_path': '/yookassa/webhook'},
            {'id': 'tgstar',    'name': 'Telegram Stars', 'icon': 'play/stars.svg',
             'sub': 'Внутренняя валюта Telegram', 'master': 'show_payment_tgstar',
             'fields': ['tgstar_rub_per_star'],
             'webhook_path': ''},  # вебхук не нужен — оплата через Bot API
            {'id': 'cryptobot', 'name': 'CryptoBot',      'icon': 'play/cryptobot.svg',
             'sub': 'Криптовалюты через @CryptoBot', 'master': 'show_payment_cryptobot',
             'fields': ['cryptobot_token'],
             'webhook_path': '/cryptobot/'},
            {'id': 'yoomoney',  'name': 'YooMoney',       'icon': 'play/yoomoney.svg',
             'sub': 'Кошелёк ЮMoney',             'master': 'show_payment_yoomoney',
             'fields': ['yoomoney_token', 'yoomoney_account', 'yoomoney_notification_secret'],
             'webhook_path': '/yoomoney/'},
            {'id': 'platega',   'name': 'Platega',        'icon': 'play/platega.svg',
             'sub': 'СБП / QR через Platega',     'master': 'show_payment_platega',
             'fields': ['platega_merchant_id', 'platega_api_secret'],
             'webhook_path': '/platega/callback'},
            {'id': 'wata',      'name': 'Wata',           'icon': 'play/wata.svg',
             'sub': 'Карты / СБП через Wata',     'master': 'show_payment_wata',
             'fields': ['wata_access_token', 'wata_terminal_public_id'],
             'webhook_path': '/wata/webhook'},
        ]
        _PAY_OTHER_KEYS = [
            'show_payment_promo_code',
            'show_payment_manual',
            'manual_transfer_link_30',
            'manual_transfer_link_60',
            'manual_transfer_link_90',
        ]
        # connect_page_url из настроек (то же значение, что main.py использует
        # для вывода вебхуков при старте). Нормализуем: убираем хвостовой '/'.
        _connect_url = ''
        try:
            _connect_url = next(
                (s['value'] for s in settings_rows if s['key'] == 'connect_page_url'),
                '',
            ) or ''
        except Exception:
            _connect_url = ''
        _connect_url = (_connect_url or '').strip().rstrip('/')

        _pay_settings_map = {s['key']: s for s in grouped_settings["💳 Методы оплаты"]}
        payment_groups = []
        _known_pay_keys = set()
        for grp in _PAY_GROUPS_DEF:
            master = _pay_settings_map.get(grp['master'])
            field_settings = [
                _pay_settings_map[k] for k in grp['fields'] if k in _pay_settings_map
            ]
            if master is None and not field_settings:
                continue
            webhook_path = grp.get('webhook_path') or ''
            webhook_url = (_connect_url + webhook_path) if (_connect_url and webhook_path) else ''
            payment_groups.append({
                'id': grp['id'],
                'name': grp['name'],
                'icon': grp['icon'],
                'sub': grp['sub'],
                'master': master,
                'fields': field_settings,
                'webhook_path': webhook_path,
                'webhook_url': webhook_url,
            })
            _known_pay_keys.add(grp['master'])
            _known_pay_keys.update(grp['fields'])
        payment_other = [
            _pay_settings_map[k] for k in _PAY_OTHER_KEYS if k in _pay_settings_map
        ]
        _known_pay_keys.update(_PAY_OTHER_KEYS)
        _known_pay_keys.add('payment_methods_order')
        payment_leftover = [
            s for s in grouped_settings["💳 Методы оплаты"]
            if s['key'] not in _known_pay_keys
        ]

        import json as _json
        from payment_methods import (
            parse_payment_order, PAYMENT_METHOD_LABELS, SETTINGS_ORDER_KEY,
            DEFAULT_BOT_ORDER,
        )
        _pm_order_row = await async_query_db(
            "SELECT value FROM settings WHERE key = ?", (SETTINGS_ORDER_KEY,), one=True,
        )
        _pm_order_raw = (_pm_order_row.get('value') if _pm_order_row else '') or ''
        bot_payment_order = parse_payment_order(_pm_order_raw, 'bot')
        _settings_cur = {r['key']: (r['value'] or '') for r in (settings_rows or [])}
        _icon_by_id = {g['id']: g['icon'] for g in _PAY_GROUPS_DEF}
        from button_registry import BUTTON_REGISTRY_MAP, style_meta_keys
        from payment_methods import PAYMENT_METHODS, is_payment_method_available
        payment_order_meta = {}
        for mid in bot_payment_order:
            pm = PAYMENT_METHODS.get(mid)
            if not pm:
                continue
            btn_key = pm['btn_key']
            sk, _ = style_meta_keys(btn_key)
            reg = BUTTON_REGISTRY_MAP.get(btn_key, {})
            payment_order_meta[mid] = {
                'id': mid,
                'label': PAYMENT_METHOD_LABELS.get(mid, mid),
                'text': _settings_cur.get(btn_key, reg.get('default_text', '')),
                'style': (_settings_cur.get(sk, '') or '').strip().lower(),
                'enabled': is_payment_method_available(mid, conf=_settings_cur),
                'bot_only': 'site' not in pm.get('scopes', ()),
                'icon': _icon_by_id.get(mid, ''),
            }
        payment_order_items = [
            {'id': m, 'label': PAYMENT_METHOD_LABELS.get(m, m)} for m in bot_payment_order
        ]
        payment_methods_order_json = _json.dumps(bot_payment_order, ensure_ascii=False)
        default_payment_methods_order_json = _json.dumps(DEFAULT_BOT_ORDER, ensure_ascii=False)
        _order_index = {m: i for i, m in enumerate(bot_payment_order)}
        payment_groups.sort(key=lambda g: _order_index.get(g['id'], 999))

        # ── Контекст IP whitelist для UI ─────────────────────────────────
        registration_protection_method = 'none'
        try:
            bot_protection_row = await async_query_db("SELECT value FROM settings WHERE key = 'bot_protection_enabled'", (), one=True)
            channel_subscription_row = await async_query_db("SELECT value FROM settings WHERE key = 'channel_subscription_enabled'", (), one=True)
            
            bot_protection_value = bot_protection_row['value'] if bot_protection_row else '0'
            channel_subscription_value = channel_subscription_row['value'] if channel_subscription_row else '0'
            
            current_app.logger.info(f"[SETTINGS] Текущие значения: bot_protection={bot_protection_value}, channel_subscription={channel_subscription_value}")
            
            # Если оба включены - это ошибка, исправляем (приоритет у bot_protection)
            if bot_protection_value == '1' and channel_subscription_value == '1':
                current_app.logger.warning("[SETTINGS] Обнаружено, что оба метода защиты включены одновременно! Отключаем channel_subscription.")
                await async_execute_db("UPDATE settings SET value = ? WHERE key = ?", ('0', 'channel_subscription_enabled'))
                channel_subscription_value = '0'
            
            if bot_protection_value == '1':
                registration_protection_method = 'bot_protection'
            elif channel_subscription_value == '1':
                registration_protection_method = 'channel_subscription'
            else:
                registration_protection_method = 'none'
            
            current_app.logger.info(f"[SETTINGS] Определен метод защиты: {registration_protection_method}")
        except Exception as e:
            current_app.logger.error(f"Ошибка определения метода защиты: {e}")
            registration_protection_method = 'none'
        
        # Получаем значения настроек для подписки на канал
        channel_subscription_username = ''
        channel_subscription_username_desc = 'Username или ID канала для обязательной подписки'
        channel_subscription_message = ''
        channel_subscription_message_desc = 'Текст сообщения при запросе подписки на канал'
        
        try:
            # Инициализируем настройки, если их нет
            username_exists = await async_query_db("SELECT 1 FROM settings WHERE key = 'channel_subscription_username'", (), one=True)
            if not username_exists:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    ('channel_subscription_username', '', 'Username или ID канала для обязательной подписки (например: @my_channel или -1001234567890)')
                )
            
            message_exists = await async_query_db("SELECT 1 FROM settings WHERE key = 'channel_subscription_message'", (), one=True)
            if not message_exists:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    ('channel_subscription_message', 'Для получения пробного периода необходимо подписаться на наш канал.', 'Текст сообщения при запросе подписки на канал')
                )
            
            # Получаем значения
            username_row = await async_query_db("SELECT value, description FROM settings WHERE key = 'channel_subscription_username'", (), one=True)
            if username_row:
                channel_subscription_username = username_row.get('value', '')
                channel_subscription_username_desc = username_row.get('description', 'Username или ID канала для обязательной подписки')
            
            message_row = await async_query_db("SELECT value, description FROM settings WHERE key = 'channel_subscription_message'", (), one=True)
            if message_row:
                channel_subscription_message = message_row.get('value', '')
                channel_subscription_message_desc = message_row.get('description', 'Текст сообщения при запросе подписки на канал')
            
            # Обновляем список настроек после возможной вставки
            settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        except Exception as e:
            current_app.logger.error(f"Ошибка получения настроек подписки на канал: {e}")

        # Настройки модератора (для карточки, видимой только админу)
        moderator_web_password = ''
        moderator_sections = []
        try:
            mp_row = await async_query_db("SELECT value FROM settings WHERE key = 'moderator_web_password'", (), one=True)
            ms_row = await async_query_db("SELECT value FROM settings WHERE key = 'moderator_sections'", (), one=True)
            if mp_row and mp_row.get('value'):
                moderator_web_password = mp_row['value']
            if ms_row and ms_row.get('value'):
                import json as _json
                try:
                    moderator_sections = _json.loads(ms_row['value'])
                    if not isinstance(moderator_sections, list):
                        moderator_sections = []
                except Exception:
                    moderator_sections = []
        except Exception:
            pass

        # ── Контекст IP whitelist для UI ─────────────────────────────────
        # current_client_ip показывается в карточке (можно «Добавить мой IP»).
        ipw_client_ip = ''
        ipw_raw = ''
        ipw_enabled = False
        try:
            from web_admin.core.ip_whitelist import (
                resolve_client_ip, parse_cidr_list,
            )
            _ip = resolve_client_ip(request)
            ipw_client_ip = str(_ip) if _ip is not None else ''
            ipw_enabled_row = await async_query_db(
                "SELECT value FROM settings WHERE key = 'admin_ip_whitelist_enabled'", (), one=True,
            )
            ipw_value_row = await async_query_db(
                "SELECT value FROM settings WHERE key = 'admin_ip_whitelist'", (), one=True,
            )
            ipw_enabled = bool(ipw_enabled_row and (ipw_enabled_row.get('value') or '').strip() == '1')
            ipw_raw = (ipw_value_row.get('value') if ipw_value_row else '') or ''
            ipw_nets, _ = parse_cidr_list(ipw_raw)
            ipw_rules = [str(n) for n in ipw_nets]
        except Exception as _wl_ctx_e:
            current_app.logger.warning(f"[WHITELIST] ctx load error: {_wl_ctx_e}")
            ipw_rules = []

        return await render_template('settings_general.html',
                                   grouped_settings=grouped_settings,
                                   payment_groups=payment_groups,
                                   payment_other=payment_other,
                                   payment_leftover=payment_leftover,
                                   payment_order_items=payment_order_items,
                                   payment_order_meta=payment_order_meta,
                                   payment_methods_order=bot_payment_order,
                                   default_payment_methods_order=DEFAULT_BOT_ORDER,
                                   payment_methods_order_json=payment_methods_order_json,
                                   default_payment_methods_order_json=default_payment_methods_order_json,
                                   payment_method_labels=PAYMENT_METHOD_LABELS,
                                   registration_protection_method=registration_protection_method,
                                   channel_subscription_username=channel_subscription_username,
                                   channel_subscription_username_desc=channel_subscription_username_desc,
                                   channel_subscription_message=channel_subscription_message,
                                   channel_subscription_message_desc=channel_subscription_message_desc,
                                   moderator_web_password=moderator_web_password,
                                   moderator_sections=moderator_sections,
                                   ipw_client_ip=ipw_client_ip,
                                   ipw_enabled=ipw_enabled,
                                   ipw_raw=ipw_raw,
                                   ipw_rules=ipw_rules)


    @admin_bp_instance.route('/settings/texts', methods=['GET', 'POST'])
    async def settings_texts():
        from web_admin.async_db import async_execute_db, async_query_db
        if request.method == 'POST':
            form = await request.form
            validation_errors: list[str] = []
            for key, value in form.items():
                if is_html_text_setting_key(key):
                    ok, msgs = validate_telegram_html(value or '')
                    if not ok:
                        validation_errors.append(f'{key}: {"; ".join(msgs)}')
            if validation_errors:
                await flash(
                    'HTML-ошибки в текстах — сохранение отменено: '
                    + ' | '.join(validation_errors[:3])
                    + ('…' if len(validation_errors) > 3 else ''),
                    'danger',
                )
                return redirect(url_for('admin.settings_texts'))
            for key, value in form.items():
                if key == 'btn_menu_my_subscription':
                    value = (value or '')[:12]
                await async_execute_db("UPDATE settings SET value = ? WHERE key = ?", (value, key))
            
            # Перезагружаем кэш настроек после сохранения
            try:
                await app_conf.load_settings()
            except Exception as e:
                current_app.logger.error(f"Ошибка перезагрузки кэша настроек: {e}")
            
            # Отправляем команду боту на перезагрузку настроек (только локально)
            try:
                bot_api_url = 'http://127.0.0.1:8081/api/reload-settings'
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.post(bot_api_url)
                    if response.status_code == 200:
                        current_app.logger.info("Настройки бота успешно перезагружены через API")
                    else:
                        current_app.logger.warning(f"Не удалось перезагрузить настройки бота: HTTP {response.status_code}")
            except Exception as e:
                current_app.logger.warning(f"Ошибка при вызове API перезагрузки настроек бота: {e}")
            
            await flash('Тексты успешно обновлены!', 'success')
            r = redirect(url_for('admin.settings_texts') + '#saved')
            r.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            return r
        # Автодобавление ключей названий кнопок оплаты и рефералок
        try:
            existing_rows = await async_query_db("SELECT key FROM settings")
            existing = {r['key'] for r in existing_rows}
            ensure_btns = [
                ('btn_payment_manual', '💸 CloudTips(СБП-Картой)', 'Текст кнопки оплаты: Перевод/CloudTips'),
                ('btn_payment_yookassa', '💳 YooKassa', 'Текст кнопки оплаты: YooKassa'),
                ('btn_payment_tgstar', '⭐️ TG Star', 'Текст кнопки оплаты: Telegram Stars'),
                ('btn_payment_cryptobot', '💎 CryptoBot', 'Текст кнопки оплаты: CryptoBot'),
                ('btn_payment_yoomoney', '💰 YooMoney', 'Текст кнопки оплаты: YooMoney'),
                ('btn_payment_platega', '🏦 Platega (СБП)', 'Текст кнопки оплаты: Platega (СБП)'),
                ('btn_payment_wata', '💳 Wata', 'Текст кнопки оплаты: Wata'),
                ('btn_referral_share', '📤 Поделиться', 'Текст кнопки: Поделиться (рефералы)'),
                ('btn_referral_free_days', '🎁 Бесплатные дни', 'Текст кнопки: Бесплатные дни (рефералы)'),
                ('btn_activate_code', '🎟️ Оплатить кодом', 'Текст кнопки: Оплатить кодом'),
                ('btn_renew_30', 'Продлить на 30 дней', 'Текст кнопки продления на 30 дней (CloudTips)'),
                ('btn_renew_60', 'Продлить на 60 дней', 'Текст кнопки продления на 60 дней (CloudTips)'),
                ('btn_renew_90', 'Продлить на 90 дней', 'Текст кнопки продления на 90 дней (CloudTips)'),
                ('btn_bot_channel', '📣 Наш канал', 'Текст кнопки: Наш канал (ссылка задаётся в bot_channel_link)'),
                ('btn_bot_custom_url', '🔗 КастомURL', 'Текст кнопки: КастомURL (ссылка задаётся в bot_custom_url)'),
                ('btn_menu_my_subscription', 'Моя подписка', 'Текст кнопки меню Mini App (≤12 символов)'),
                # Новые названия кнопок раздела "О сервисе"
                ('btn_user_agreement', '📄 Пользовательское соглашение', 'Текст кнопки: Пользовательское соглашение (для ссылки выше)'),
                ('btn_privacy_policy', '🔒 Политика конфиденциальности', 'Текст кнопки: Политика конфиденциальности (для ссылки выше)'),
                ('btn_support_link', '💬 Перейти в поддержку', 'Текст кнопки перехода в поддержку (для ссылки выше)'),
                ('btn_support_custom_link', '📞 Кастомная поддержка', 'Текст кнопки кастомной ссылки поддержки (для ссылки выше)')
            ]
            for k, v, d in ensure_btns:
                if k not in existing:
                    await async_execute_db("INSERT INTO settings (key, value, description) VALUES (?, ?, ?)", (k, v, d))
            # Текст для шаринга реферальной программы
            if 'text_referral_share' not in existing:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    ('text_referral_share', 'Присоединяйся!', 'Текст для кнопки Поделиться в реферальной программе (можно использовать {ref_link})')
                )
            # Текст поддержки
            if 'text_support' not in existing:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    ('text_support', '💬 <b>Поддержка</b>\n\nДля того, чтобы мы быстро вас нашли, скопируйте ваш ID заранее и отправьте в поддержку с проблемой, которая у вас случилась.\n\n📋 <b>Ваш ID для копирования:</b>\n\n<blockquote>{user_id}</blockquote>\n\n👇 Нажмите на текст выше, чтобы скопировать ваш ID, затем перейдите в поддержку.', 'Текст страницы поддержки. Используйте {user_id} для отображения ID пользователя')
                )
            # Текст об окончании трафика Remnawave
            if 'text_remnawave_traffic_exhausted' not in existing:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    ('text_remnawave_traffic_exhausted', '⚠️ <b>Трафик закончился</b>\n\n📊 Использовано: {used_gb:.2f} GB из {limit_gb:.2f} GB\n\nБезлимитные серверы все равно доступны.\n\nДля продолжения пользования:\n• Докупите GB\n• Продлите подписку\n\nДля продления вернитесь на главную.', 'Текст сообщения об окончании трафика Remnawave. Переменные: {used_gb} - использовано GB, {limit_gb} - лимит GB')
                )
            if 'text_subscription_expiring' not in existing:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    (
                        'text_subscription_expiring',
                        '⏰ Ваша подписка заканчивается завтра! Не забудьте продлить, чтобы не потерять доступ.',
                        'Напоминание о скором завершении подписки (авто-уведомление в Telegram)',
                    ),
                )
            if 'text_subscription_expired' not in existing:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    (
                        'text_subscription_expired',
                        '😔 Ваша подписка истекла. Чтобы возобновить доступ, пожалуйста, продлите ее.',
                        'Текст, который получит пользователь, когда его подписка истечёт (авто-уведомление)',
                    ),
                )
            if 'text_subscription_revoke' not in existing:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    (
                        'text_subscription_revoke',
                        '⚠️ <b>Данные для подключения вашей подписки были сброшены</b>\n\n'
                        'Это могло произойти из-за:\n'
                        '• Нарушения правил сервиса\n'
                        '• Вашего запроса на сброс\n\n'
                        '🔑 <b>Новые данные для подключения готовы</b>\n'
                        '📱 Лимит устройств и срок подписки не изменились\n'
                        '📊 Использованный трафик сохранён\n\n'
                        '⚠️ <b>Важно:</b> После добавления новой подписки обязательно удалите старую '
                        'подписку из вашего клиента.\n\n'
                        'Нажмите кнопку ниже, чтобы получить новые данные для подключения.',
                        'Сообщение пользователю после «Сбросить подписку» (кнопка «Подключиться» добавляется автоматически)',
                    ),
                )
            if 'text_partner_program' not in existing:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    (
                        'text_partner_program',
                        '🤝 <b>Партнёрская программа</b>\n\n'
                        '<blockquote>'
                        '💰 <b>Баланс:</b> {balance_str} ₽\n'
                        '📊 <b>Процент отчислений:</b> {percent}%\n'
                        '👥 <b>Приглашено пользователей:</b> {ref_count}\n'
                        '💳 <b>Оплат от приглашённых:</b> {pay_count}\n'
                        '</blockquote>\n'
                        '{link_line}\n'
                        'Каждый раз когда ваш реферал оплачивает подписку — вы получаете '
                        '<b>{percent}%</b> от суммы на баланс.\n\n'
                        '<i>Минимальная сумма вывода: {min_withdraw} ₽</i>',
                        'Главный экран партнёрской программы. Переменные: {balance_str}, {percent}, {ref_count}, {pay_count}, {link_line}, {min_withdraw}',
                    ),
                )
            if 'text_partner_withdraw' not in existing:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    (
                        'text_partner_withdraw',
                        '💸 <b>Запрос на вывод средств</b>\n\n'
                        '💰 Ваш текущий баланс: <b>{balance_str} ₽</b>\n\n'
                        'Для вывода средств обратитесь в поддержку, указав:\n'
                        '• Ваш Telegram ID: <code>{user_id}</code>\n'
                        '• Желаемую сумму вывода\n'
                        '• Реквизиты для перевода',
                        'Экран «Запросить вывод» в партнёрской программе. Переменные: {balance_str}, {user_id}',
                    ),
                )
            if 'text_website_cabinet_no_email' not in existing:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    (
                        'text_website_cabinet_no_email',
                        '🌐 <b>Личный кабинет</b>\n\n'
                        '<blockquote>⚠️ <b>Необходима привязка Email</b>\n'
                        'Для доступа к сайту и сохранения вашей подписки, пожалуйста, '
                        'привяжите адрес электронной почты.</blockquote>\n\n'
                        '<i>Альтернативный доступ к подписке если Telegram не работает.</i>',
                        'Личный кабинет: email не привязан (кнопка «Привязать email» добавляется автоматически)',
                    ),
                )
            if 'text_website_cabinet_active' not in existing:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    (
                        'text_website_cabinet_active',
                        '🌐 <b>Личный кабинет</b>\n\n'
                        '<b>В кабинете вы можете:</b>\n'
                        '<i>• 💳 Оплатить или продлить подписку\n'
                        '• 🔑 Получить ключ подключения\n'
                        '• 📊 Следить за трафиком и сроком\n'
                        '• 📱 Управлять устройствами</i>\n\n'
                        'Нажмите кнопку ниже — вы войдёте автоматически.\n'
                        '<i>Ссылка действует 10 минут.</i>',
                        'Личный кабинет: email привязан, подписка активна (кнопка «Открыть кабинет» добавляется автоматически)',
                    ),
                )
            if 'text_website_cabinet_expired' not in existing:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    (
                        'text_website_cabinet_expired',
                        '🌐 <b>Личный кабинет</b>\n\n'
                        '❌ У вас закончилась подписка — продлите для полного доступа к личному кабинету.\n\n'
                        'Нажмите кнопку ниже — вы войдёте автоматически.\n'
                        '<i>Ссылка действует 10 минут.</i>',
                        'Личный кабинет: подписка истекла (кнопки «Открыть кабинет» и «Продлить» добавляются автоматически)',
                    ),
                )
            if 'text_traffic_renewal_payment' not in existing:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    (
                        'text_traffic_renewal_payment',
                        '💳 <b>Оплата продления трафика</b>\n\n'
                        '➕ <b>Будет добавлено:</b> {traffic_gb} GB\n'
                        '💰 <b>Стоимость:</b> {price} {currency}\n\n'
                        'Нажмите кнопку оплаты ниже, чтобы перейти к оплате.\n\n'
                        '<blockquote>⏰ Ваш успешный платеж будет обработан до 5 минут</blockquote>',
                        'Докупка трафика: единый экран оплаты для всех провайдеров (кнопка оплаты добавляется автоматически). Переменные: {traffic_gb}, {price}, {currency}',
                    ),
                )
            if 'text_traffic_renewal_select' not in existing:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    (
                        'text_traffic_renewal_select',
                        '📈 <b>Докупить гигабайты</b>{traffic_info}\n\n'
                        '💳 <b>Выберите тариф:</b>',
                        'Докупка трафика: выбор тарифа. Переменная {traffic_info} — блок текущего трафика (может быть пустым)',
                    ),
                )
            if 'text_traffic_renewal_confirm' not in existing:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    (
                        'text_traffic_renewal_confirm',
                        '📈 <b>Докупить гигабайты</b>\n\n'
                        '📦 <b>Выбранный тариф:</b> {tariff_name}\n'
                        '➕ <b>Будет добавлено:</b> {tariff_gb} GB\n'
                        '📊 <b>Новый лимит:</b> {new_traffic_limit_gb} GB\n\n'
                        '💰 <b>Стоимость:</b> {price} ₽\n\n'
                        '💳 <b>Выберите способ оплаты:</b>',
                        'Докупка трафика: после выбора тарифа. Переменные: {tariff_name}, {tariff_gb}, {new_traffic_limit_gb}, {price}',
                    ),
                )
        except Exception:
            pass
        settings_rows = await async_query_db("SELECT key, value, description FROM settings ORDER BY key")
        # Группы только под ТЕКСТЫ (text_*) и тексты «защиты от ботов».
        # Тексты кнопок (btn_*) теперь живут в /settings/buttons и здесь не показываются.
        grouped_settings = {
            "👋 Приветствие и подписка": [],
            "🛒 Каталог, заказы и роутер": [],
            "🔔 Уведомления": [],
            "💳 Оплата и продление": [],
            "🎟️ Промокоды": [],
            "👥 Реферальная программа": [],
            "💠 Партнёрская программа": [],
            "📘 О сервисе и инструкции": [],
            "💬 Поддержка": [],
            "🌐 Личный кабинет": [],
            "📈 Докупка трафика": [],
            "🤖 Защита от ботов": [],
        }
        # Экраны каталога числились редактируемыми с самого их появления,
        # но ни под один фильтр ниже не подпадали и на страницу не попадали.
        from src.shop_texts import CATALOG_TEXTS
        catalog_text_order = {key: i for i, (key, _, _) in enumerate(CATALOG_TEXTS)}
        notification_text_keys = (
            'text_subscription_expiring',
            'text_subscription_expired',
            'text_remnawave_traffic_exhausted',
            'text_subscription_revoke',
        )
        notification_text_order = {k: i for i, k in enumerate(notification_text_keys)}
        partner_text_keys = (
            'text_partner_program',
            'text_partner_withdraw',
        )
        partner_text_order = {k: i for i, k in enumerate(partner_text_keys)}
        cabinet_text_keys = (
            'text_website_cabinet_no_email',
            'text_website_cabinet_active',
            'text_website_cabinet_expired',
        )
        cabinet_text_order = {k: i for i, k in enumerate(cabinet_text_keys)}
        traffic_renewal_text_keys = (
            'text_traffic_renewal_select',
            'text_traffic_renewal_confirm',
            'text_traffic_renewal_payment',
        )
        traffic_renewal_text_order = {k: i for i, k in enumerate(traffic_renewal_text_keys)}
        for setting in settings_rows:
            key = setting['key']
            if key.startswith('btn_'):
                continue
            if key.startswith('bot_protection_') and key.endswith('_text'):
                grouped_settings["🤖 Защита от ботов"].append(setting)
                continue
            if not key.startswith('text_'):
                continue
            if key in catalog_text_order:
                grouped_settings["🛒 Каталог, заказы и роутер"].append(setting)
            elif key in notification_text_keys:
                grouped_settings["🔔 Уведомления"].append(setting)
            elif (key.startswith('text_welcome') or key.startswith('text_sub') or key.startswith('text_no_active')
                    or key == 'text_subscription_expired_main'):
                grouped_settings["👋 Приветствие и подписка"].append(setting)
            elif key.startswith('text_payment') and key != 'text_payment_prompt':
                grouped_settings["💳 Оплата и продление"].append(setting)
            elif key.startswith('text_promo'):
                grouped_settings["🎟️ Промокоды"].append(setting)
            elif key in ('text_referral_program', 'text_ref_bonus_on_join',
                         'text_ref_bonus_on_payment', 'text_referral_share'):
                grouped_settings["👥 Реферальная программа"].append(setting)
            elif key in partner_text_keys:
                grouped_settings["💠 Партнёрская программа"].append(setting)
            elif key in cabinet_text_keys:
                grouped_settings["🌐 Личный кабинет"].append(setting)
            elif key in traffic_renewal_text_keys:
                grouped_settings["📈 Докупка трафика"].append(setting)
            elif key in ('text_support',):
                grouped_settings["💬 Поддержка"].append(setting)
            elif key.startswith('text_about'):
                grouped_settings["📘 О сервисе и инструкции"].append(setting)
        grouped_settings["🛒 Каталог, заказы и роутер"].sort(
            key=lambda s: catalog_text_order.get(s['key'], 99)
        )
        grouped_settings["🔔 Уведомления"].sort(
            key=lambda s: notification_text_order.get(s['key'], 99)
        )
        grouped_settings["💠 Партнёрская программа"].sort(
            key=lambda s: partner_text_order.get(s['key'], 99)
        )
        grouped_settings["🌐 Личный кабинет"].sort(
            key=lambda s: cabinet_text_order.get(s['key'], 99)
        )
        grouped_settings["📈 Докупка трафика"].sort(
            key=lambda s: traffic_renewal_text_order.get(s['key'], 99)
        )
        from src.text_setting_vars import all_text_setting_variables_for_admin
        text_setting_variables = all_text_setting_variables_for_admin()
        return await render_template(
            'settings_texts.html',
            grouped_settings=grouped_settings,
            text_setting_variables=text_setting_variables,
        )

    @admin_bp_instance.route('/settings/texts/update', methods=['POST'])
    async def settings_texts_update():
        from web_admin.async_db import async_execute_db
        try:
            if request.is_json:
                payload = await request.get_json(force=True)
                key = (payload.get('key') or '').strip()
                value = payload.get('value') or ''
            else:
                form = await request.form
                key = (form.get('key') or '').strip()
                value = form.get('value') or ''
            if not key:
                return jsonify({'ok': False, 'error': 'empty_key'}), 400
            # Защита от случайного удаления токена бота
            if key == 'bot_token':
                # Если значение пустое или состоит только из пробелов, пропускаем обновление
                if not value or not value.strip():
                    current_app.logger.warning("[SETTINGS] Попытка сохранить пустой токен бота через texts/update - обновление пропущено")
                    return jsonify({'ok': False, 'error': 'empty_bot_token'}), 400
                clear_bot_token_cache()
            if key == 'btn_menu_my_subscription':
                value = (value or '')[:12]
            if is_html_text_setting_key(key):
                ok, msgs = validate_telegram_html(value or '')
                if not ok:
                    return jsonify({
                        'ok': False,
                        'error': 'validation_failed',
                        'messages': msgs,
                    }), 400
            await async_execute_db("UPDATE settings SET value = ? WHERE key = ?", (value, key))
            
            # Перезагружаем кэш настроек после сохранения
            try:
                await app_conf.load_settings()
            except Exception as e:
                current_app.logger.error(f"Ошибка перезагрузки кэша настроек: {e}")
            
            # Отправляем команду боту на перезагрузку настроек (только локально)
            try:
                bot_api_url = 'http://127.0.0.1:8081/api/reload-settings'
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.post(bot_api_url)
                    if response.status_code == 200:
                        current_app.logger.info("Настройки бота успешно перезагружены через API")
                    else:
                        current_app.logger.warning(f"Не удалось перезагрузить настройки бота: HTTP {response.status_code}")
            except Exception as e:
                current_app.logger.warning(f"Ошибка при вызове API перезагрузки настроек бота: {e}")
            
            return jsonify({'ok': True})
        except Exception as e:
            current_app.logger.error(f"settings_texts_update error: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

    # ─── Настройки веб-кабинета (website) ───────────────────────────────────

    @admin_bp_instance.route('/settings/website-cabinet', methods=['GET'])
    async def settings_website_cabinet():
        from web_admin.run import current_user
        if not current_user.is_admin:
            return '', 403
        from web_admin.core.website_cabinet_config import load_config, POPULAR_DOMAIN_PRESETS
        cfg = await load_config()
        return await render_template(
            'settings_website_cabinet.html',
            cfg=cfg,
            domain_presets=POPULAR_DOMAIN_PRESETS,
        )

    @admin_bp_instance.route('/settings/website-cabinet/save', methods=['POST'])
    async def settings_website_cabinet_save():
        from web_admin.run import current_user
        if not current_user.is_admin:
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
        from web_admin.core.website_cabinet_config import save_config
        try:
            payload = await request.get_json(force=True) or {}
        except Exception as e:
            return jsonify({'ok': False, 'error': f'bad_json: {e}'}), 400
        try:
            cfg_in = payload.get('cfg') if isinstance(payload.get('cfg'), dict) else payload
            cfg = await save_config(cfg_in)
            return jsonify({'ok': True, 'cfg': cfg})
        except Exception as e:
            current_app.logger.error(f"settings_website_cabinet_save error: {e}", exc_info=True)
            return jsonify({'ok': False, 'error': str(e)}), 500

    # ─── Стилизация inline-кнопок (text/style/icon/kind из реестра) ─────────

    @admin_bp_instance.route('/settings/buttons', methods=['GET'])
    async def settings_buttons():
        """Страница управления текстом, стилями, premium-эмодзи и режимом
        открытия (url/webapp) inline-кнопок бота.

        В settings хранятся:
          - `<key>`             — текст кнопки;
          - `<key>__style`      — стиль ('' | primary | success | danger);
          - `<key>__icon`       — icon_custom_emoji_id (Premium custom emoji);
          - `<key>__kind`       — режим открытия (url|webapp), только для kind-aware кнопок;
          - `group_<gid>_enabled` — тумблер всей группы (опционально, см. GROUP_TOGGLE_KEYS).
        """
        from web_admin.run import current_user
        if not current_user.is_admin:
            return '', 403
        from web_admin.async_db import async_query_db, async_execute_db
        from button_registry import (
            BUTTON_REGISTRY, BUTTON_GROUPS, ALLOWED_STYLES, ALLOWED_KINDS,
            KIND_AWARE_KEYS, GROUP_TOGGLE_KEYS,
            style_meta_keys, kind_key, group_enabled_key, is_kind_aware,
            enabled_key, has_per_button_toggle,
            MAIN_MENU_LAYOUT_SETTING, parse_main_menu_layout,
            MAIN_MENU_LAYOUT_KEYS, DEFAULT_MAIN_MENU_LAYOUT,
        )

        # Подтянем все нужные ключи разом
        keys: list[str] = []
        for b in BUTTON_REGISTRY:
            keys.append(b['key'])
            sk, ik = style_meta_keys(b['key'])
            keys.extend([sk, ik])
            if is_kind_aware(b['key']):
                keys.append(kind_key(b['key']))
            if has_per_button_toggle(b['key']):
                keys.append(enabled_key(b['key']))
        # Тумблеры групп
        for gk in GROUP_TOGGLE_KEYS.values():
            keys.append(gk)
        placeholders = ','.join(['?'] * len(keys))
        rows = await async_query_db(
            f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
            keys,
        )
        cur = {r['key']: (r['value'] or '') for r in (rows or [])}

        # Лениво сидируем недостающие ключи дефолтами реестра.
        try:
            seeded = 0
            for b in BUTTON_REGISTRY:
                key = b['key']
                sk, ik = style_meta_keys(key)
                if key not in cur:
                    await async_execute_db(
                        "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                        (key, b.get('default_text', ''), f"Текст кнопки: {b.get('label', key)}"),
                    )
                    cur[key] = b.get('default_text', '')
                    seeded += 1
                if sk not in cur:
                    await async_execute_db(
                        "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                        (sk, b.get('default_style', '') or '', f"Стиль кнопки: {b.get('label', key)}"),
                    )
                    cur[sk] = b.get('default_style', '') or ''
                    seeded += 1
                if ik not in cur:
                    await async_execute_db(
                        "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                        (ik, b.get('default_icon', '') or '', f"icon_custom_emoji_id: {b.get('label', key)}"),
                    )
                    cur[ik] = b.get('default_icon', '') or ''
                    seeded += 1
                if is_kind_aware(key):
                    kk = kind_key(key)
                    if kk not in cur:
                        await async_execute_db(
                            "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                            (kk, b.get('default_kind', '') or '',
                             f"Режим открытия (url|webapp): {b.get('label', key)}"),
                        )
                        cur[kk] = b.get('default_kind', '') or ''
                        seeded += 1
                if has_per_button_toggle(key):
                    ek = enabled_key(key)
                    if ek not in cur:
                        await async_execute_db(
                            "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                            (ek, '1', f"Видимость кнопки: {b.get('label', key)}"),
                        )
                        cur[ek] = '1'
                        seeded += 1
            for gid, gk in GROUP_TOGGLE_KEYS.items():
                if gk not in cur:
                    await async_execute_db(
                        "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                        (gk, '1', f"Группа кнопок ({gid}) включена/выключена"),
                    )
                    cur[gk] = '1'
                    seeded += 1
            if seeded:
                current_app.logger.info(f"[BUTTONS] Засидировано {seeded} ключ(ей) в settings")
                try:
                    await app_conf.load_settings()
                except Exception:
                    pass
        except Exception as e:
            current_app.logger.warning(f"[BUTTONS] Ошибка авто-сида: {e}")

        # Группируем кнопки для UI.
        groups: dict[str, dict] = {
            gid: {
                'id': gid, 'title': gtitle, 'buttons': [],
                'toggle_key': GROUP_TOGGLE_KEYS.get(gid),
                'enabled': True if not GROUP_TOGGLE_KEYS.get(gid)
                                else str(cur.get(GROUP_TOGGLE_KEYS[gid], '1')) != '0',
            }
            for gid, gtitle in BUTTON_GROUPS
        }
        for b in BUTTON_REGISTRY:
            key = b['key']
            sk, ik = style_meta_keys(key)
            kk = kind_key(key) if is_kind_aware(key) else None
            ek = enabled_key(key) if has_per_button_toggle(key) else None
            item = {
                'key': key,
                'label': b.get('label', key),
                'text': cur.get(key, b.get('default_text', '')),
                'style': (cur.get(sk, '') or '').strip().lower(),
                'icon': (cur.get(ik, '') or '').strip(),
                'kind': (cur.get(kk, '') or '').strip().lower() if kk else '',
                'default_text': b.get('default_text', ''),
                'default_style': b.get('default_style', '') or '',
                'default_icon': b.get('default_icon', '') or '',
                'default_kind': b.get('default_kind', '') or '',
                'kind_aware': bool(kk),
                'has_toggle': bool(ek),
                'enabled': True if not ek else str(cur.get(ek, '1')) != '0',
            }
            grp = groups.get(b.get('group'))
            if grp is not None:
                grp['buttons'].append(item)

        groups_list = [g for g in groups.values() if g['buttons']]

        layout_labels = {
            b['key']: b.get('label', b['key'])
            for b in BUTTON_REGISTRY if b['key'] in MAIN_MENU_LAYOUT_KEYS
        }
        _conditional_layout_keys = frozenset({
            'btn_traffic_renewal',
            'btn_bot_custom_url', 'btn_bot_channel',
            'btn_website_access',
        })
        layout_button_meta = {}
        for b in BUTTON_REGISTRY:
            key = b['key']
            if key not in MAIN_MENU_LAYOUT_KEYS:
                continue
            sk, _ = style_meta_keys(key)
            layout_button_meta[key] = {
                'label': b.get('label', key),
                'text': cur.get(key, b.get('default_text', '')),
                'style': (cur.get(sk, '') or '').strip().lower(),
                'conditional': key in _conditional_layout_keys,
            }
        layout_row = await async_query_db(
            "SELECT value FROM settings WHERE key = ?", (MAIN_MENU_LAYOUT_SETTING,), one=True,
        )
        main_menu_layout = parse_main_menu_layout(
            (layout_row.get('value') if layout_row else '') or '',
        )

        return await render_template(
            'settings_buttons.html',
            groups=groups_list,
            allowed_styles=ALLOWED_STYLES,
            allowed_kinds=ALLOWED_KINDS,
            main_menu_layout=main_menu_layout,
            layout_labels=layout_labels,
            layout_button_meta=layout_button_meta,
            default_main_menu_layout=DEFAULT_MAIN_MENU_LAYOUT,
        )

    @admin_bp_instance.route('/settings/buttons/save', methods=['POST'])
    async def settings_buttons_save():
        """Bulk-сохранение для страницы «Стиль кнопок».

        Формат JSON:
        {
          "items": [
            {"key":"btn_xxx", "text":"...", "style":"...", "icon":"...", "kind":"url|webapp"},
            ...
          ],
          "groups": {"connect": true, ...}
        }

        Все поля опциональны — пишутся только переданные ключи.
        """
        from web_admin.run import current_user
        if not current_user.is_admin:
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
        from web_admin.async_db import async_execute_db
        from button_registry import (
            BUTTON_REGISTRY_MAP, ALLOWED_STYLES, ALLOWED_KINDS,
            GROUP_TOGGLE_KEYS,
            style_meta_keys, kind_key, is_kind_aware,
            enabled_key, has_per_button_toggle,
            MAIN_MENU_LAYOUT_SETTING, parse_main_menu_layout,
        )
        import json as _json
        try:
            payload = await request.get_json(force=True, silent=True) or {}
            items = payload.get('items') or []
            grp_toggles = payload.get('groups') or {}
            layout = payload.get('layout')
            if not isinstance(items, list) or not isinstance(grp_toggles, dict):
                return jsonify({'ok': False, 'error': 'bad_payload'}), 400

            updated = 0
            if layout is not None:
                validated_layout = parse_main_menu_layout(layout)
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (
                        MAIN_MENU_LAYOUT_SETTING,
                        _json.dumps(validated_layout, ensure_ascii=False),
                        'Раскладка главного меню бота (JSON: массив рядов с ключами кнопок)',
                    ),
                )
                updated += 1
            for it in items:
                if not isinstance(it, dict):
                    continue
                key = (it.get('key') or '').strip()
                if not key or key not in BUTTON_REGISTRY_MAP:
                    continue
                label = BUTTON_REGISTRY_MAP[key].get('label', key)
                sk, ik = style_meta_keys(key)

                # text (если передан)
                if 'text' in it:
                    text = (it.get('text') or '')
                    # Защита от слишком длинного текста (Telegram лимит ~64 символа,
                    # с запасом — 96).
                    text = text[:96]
                    await async_execute_db(
                        "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, text, f"Текст кнопки: {label}"),
                    )

                # style
                if 'style' in it:
                    style = (it.get('style') or '').strip().lower()
                    if style not in ALLOWED_STYLES:
                        style = ''
                    await async_execute_db(
                        "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (sk, style, f"Стиль кнопки: {label}"),
                    )

                # icon (только цифры)
                if 'icon' in it:
                    icon = (it.get('icon') or '').strip()
                    if icon and not icon.isdigit():
                        icon = ''.join(ch for ch in icon if ch.isdigit())
                    await async_execute_db(
                        "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (ik, icon, f"icon_custom_emoji_id: {label}"),
                    )

                # kind (только для kind-aware кнопок)
                if 'kind' in it and is_kind_aware(key):
                    kind = (it.get('kind') or '').strip().lower()
                    if kind not in ALLOWED_KINDS:
                        kind = (BUTTON_REGISTRY_MAP[key].get('default_kind') or 'url')
                    await async_execute_db(
                        "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (kind_key(key), kind,
                         f"Режим открытия (url|webapp): {label}"),
                    )

                # enabled — персональный тумблер видимости
                if 'enabled' in it and has_per_button_toggle(key):
                    en_val = '1' if bool(it.get('enabled')) else '0'
                    await async_execute_db(
                        "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (enabled_key(key), en_val,
                         f"Видимость кнопки: {label}"),
                    )

                updated += 1

            # Тумблеры групп
            for gid, enabled in grp_toggles.items():
                gk = GROUP_TOGGLE_KEYS.get(gid)
                if not gk:
                    continue
                val = '1' if bool(enabled) else '0'
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (gk, val, f"Группа кнопок ({gid}) включена/выключена"),
                )

            # Перезагружаем кэш бота
            try:
                await app_conf.load_settings()
            except Exception as e:
                current_app.logger.warning(f"[BUTTONS] reload settings: {e}")
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post('http://127.0.0.1:8081/api/reload-settings')
            except Exception:
                pass

            return jsonify({'ok': True, 'updated': updated})
        except Exception as e:
            current_app.logger.error(f"settings_buttons_save error: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

    @admin_bp_instance.route('/settings/buttons/reset', methods=['POST'])
    async def settings_buttons_reset():
        """Сброс к дефолтам реестра.

        Параметры (JSON):
          {"key": "..."}     — одну кнопку, иначе все;
          {"scope": "text"}  — только подпись, не трогая стиль и эмодзи.

        Область нужна ради обновлений оформления: подписи в коде меняются,
        а цвета оператор подобрал сам, и полный сброс их сносил — из-за
        этого новые подписи не забирали вовсе.
        """
        from web_admin.run import current_user
        if not current_user.is_admin:
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
        from web_admin.async_db import async_execute_db
        from button_registry import (
            BUTTON_REGISTRY, BUTTON_REGISTRY_MAP,
            style_meta_keys, kind_key, is_kind_aware,
            enabled_key, has_per_button_toggle,
        )
        try:
            payload = await request.get_json(force=True, silent=True) or {}
            target_key = (payload.get('key') or '').strip()
            text_only = (payload.get('scope') or '').strip() == 'text'
            targets = (
                [BUTTON_REGISTRY_MAP[target_key]]
                if target_key and target_key in BUTTON_REGISTRY_MAP
                else BUTTON_REGISTRY
            )
            for b in targets:
                key = b['key']
                label = b.get('label', key)
                sk, ik = style_meta_keys(key)
                # Текст кнопки тоже возвращаем к дефолту
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, b.get('default_text', '') or '',
                     f"Текст кнопки: {label}"),
                )
                if text_only:
                    continue
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (sk, b.get('default_style', '') or '',
                     f"Стиль кнопки: {label}"),
                )
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (ik, b.get('default_icon', '') or '',
                     f"icon_custom_emoji_id: {label}"),
                )
                if is_kind_aware(key):
                    await async_execute_db(
                        "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (kind_key(key), b.get('default_kind', '') or '',
                         f"Режим открытия (url|webapp): {label}"),
                    )
                if has_per_button_toggle(key):
                    await async_execute_db(
                        "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (enabled_key(key), '1', f"Видимость кнопки: {label}"),
                    )
            try:
                await app_conf.load_settings()
            except Exception:
                pass
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post('http://127.0.0.1:8081/api/reload-settings')
            except Exception:
                pass
            return jsonify({'ok': True, 'reset': len(targets)})
        except Exception as e:
            current_app.logger.error(f"settings_buttons_reset error: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500


    # ── 3X-UI удалён: legacy-маршруты → Remnawave ───────────────────────────

    def _gone_3xui_json():
        return jsonify({'success': False, 'error': '3X-UI удалён из проекта'}), 410

    @admin_bp_instance.route('/settings/inbound-templates', methods=['GET', 'POST'])
    async def settings_inbound_templates():
        await flash('Раздел 3X-UI удалён. Управляйте нодами в Remnawave.', 'info')
        return redirect(url_for('admin.remnawave_dashboard'))

    @admin_bp_instance.route('/api/inbound-templates/generate-keys', methods=['POST'])
    async def api_generate_reality_keys():
        return _gone_3xui_json()

    @admin_bp_instance.route('/settings/servers', methods=['GET', 'POST'])
    async def settings_servers():
        return redirect(url_for('admin.remnawave_dashboard'))

    @admin_bp_instance.route('/settings/xray_json', methods=['GET'])
    async def settings_xray_json():
        return redirect(url_for('admin.remnawave_settings'))

    @admin_bp_instance.route('/settings/xray_json/save', methods=['POST'])
    async def settings_xray_json_save():
        return redirect(url_for('admin.remnawave_settings'))

    @admin_bp_instance.route('/api/xray_json/generate_from_servers', methods=['GET'])
    async def xray_json_generate_from_servers():
        return _gone_3xui_json()

    @admin_bp_instance.route('/settings/servers/<int:server_id>/xray_restart_panel', methods=['POST'])
    async def settings_server_xray_restart_panel(server_id: int):
        return redirect(url_for('admin.remnawave_dashboard'))

    @admin_bp_instance.route('/settings/servers/<int:server_id>/delete_disabled', methods=['POST'])
    async def settings_server_delete_disabled(server_id: int):
        return redirect(url_for('admin.remnawave_dashboard'))

    @admin_bp_instance.route('/settings/servers/<int:server_id>/archive', methods=['POST'])
    async def settings_server_archive(server_id: int):
        return redirect(url_for('admin.remnawave_dashboard'))

    @admin_bp_instance.route('/settings/servers/archive/<int:server_id>/restore', methods=['POST'])
    async def settings_server_restore(server_id: int):
        return redirect(url_for('admin.remnawave_dashboard'))

    @admin_bp_instance.route('/settings/servers/archive/<int:server_id>/delete', methods=['POST'])
    async def settings_server_archive_delete(server_id: int):
        return redirect(url_for('admin.remnawave_dashboard'))

    @admin_bp_instance.route('/settings/servers/archive/edit/<int:server_id>', methods=['GET', 'POST'])
    async def edit_archive_server(server_id: int):
        return redirect(url_for('admin.remnawave_dashboard'))

    @admin_bp_instance.route('/api/servers/archive/check_alive', methods=['POST', 'GET'])
    async def api_archive_check_alive():
        return _gone_3xui_json()

    @admin_bp_instance.route('/settings/servers/edit/<int:server_id>', methods=['GET', 'POST'])
    async def edit_server(server_id):
        return redirect(url_for('admin.remnawave_dashboard'))

    @admin_bp_instance.route('/settings/servers/add', methods=['GET', 'POST'])
    async def add_server():
        return redirect(url_for('admin.remnawave_dashboard'))

    @admin_bp_instance.route('/api/servers/location-add-status/<int:server_id>', methods=['GET'])
    async def api_location_add_status(server_id):
        return _gone_3xui_json()

    @admin_bp_instance.route('/api/servers/<int:server_id>/sync', methods=['POST'])
    async def sync_server(server_id: int):
        return _gone_3xui_json()

    @admin_bp_instance.route('/api/servers/sync-status/<int:server_id>', methods=['GET'])
    async def api_sync_status(server_id: int):
        return _gone_3xui_json()

    @admin_bp_instance.route('/settings/servers/delete/<int:server_id>', methods=['POST'])
    async def delete_server(server_id):
        return redirect(url_for('admin.remnawave_dashboard'))

    @admin_bp_instance.route('/settings/servers/move_up/<int:server_id>', methods=['POST'])
    async def move_server_up(server_id):
        return redirect(url_for('admin.remnawave_dashboard'))

    @admin_bp_instance.route('/settings/servers/move_down/<int:server_id>', methods=['POST'])
    async def move_server_down(server_id):
        return redirect(url_for('admin.remnawave_dashboard'))

    @admin_bp_instance.route('/api/servers/reorder', methods=['POST'])
    async def api_servers_reorder():
        return _gone_3xui_json()

    @admin_bp_instance.route('/api/servers/<int:server_id>/key_template', methods=['POST'])
    async def api_server_key_template_update(server_id: int):
        return _gone_3xui_json()

    # ── Бэкап БД ─────────────────────────────────────────────────────────────

    def _backup_env_path(db_path: str) -> str | None:
        db_dir = os.path.dirname(os.path.abspath(db_path))
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for candidate in (os.path.join(db_dir, '.env'), os.path.join(project_root, '.env')):
            if os.path.isfile(candidate):
                return candidate
        return None

    def _file_size(path: str | None) -> int | None:
        if not path or not os.path.isfile(path):
            return None
        try:
            return os.path.getsize(path)
        except OSError:
            return None

    @admin_bp_instance.route('/settings/backup', methods=['GET', 'POST'])
    async def settings_backup():
        from web_admin.run import current_user
        if not current_user.is_admin:
            return '', 403
        from web_admin.async_db import async_query_db, async_execute_db

        row = await async_query_db("SELECT * FROM backup_settings LIMIT 1", (), one=True)
        if not row:
            await async_execute_db(
                "INSERT INTO backup_settings (admin_telegram_id, interval_hours, enabled) VALUES (?, ?, ?)",
                ('', 3, 0),
            )
            row = await async_query_db("SELECT * FROM backup_settings LIMIT 1", (), one=True)

        if request.method == 'POST':
            form = await request.form
            enabled = 1 if form.get('enabled') else 0
            s3_enabled = 1 if form.get('s3_enabled') else 0
            try:
                interval_hours = max(1, min(168, int(form.get('interval_hours') or 3)))
            except (TypeError, ValueError):
                interval_hours = 3
            admin_telegram_id = (form.get('admin_telegram_id') or '').strip()
            s3_endpoint = (form.get('s3_endpoint') or '').strip()
            s3_region = (form.get('s3_region') or '').strip()
            s3_bucket = (form.get('s3_bucket') or '').strip()
            s3_access_key = (form.get('s3_access_key') or '').strip()
            s3_prefix = (form.get('s3_prefix') or '').strip()
            s3_secret_key = (form.get('s3_secret_key') or '').strip()

            if s3_secret_key:
                await async_execute_db(
                    "UPDATE backup_settings SET admin_telegram_id=?, interval_hours=?, enabled=?, "
                    "s3_enabled=?, s3_endpoint=?, s3_region=?, s3_bucket=?, s3_access_key=?, "
                    "s3_secret_key=?, s3_prefix=? WHERE id=?",
                    (
                        admin_telegram_id, interval_hours, enabled,
                        s3_enabled, s3_endpoint, s3_region, s3_bucket,
                        s3_access_key, s3_secret_key, s3_prefix, row['id'],
                    ),
                )
            else:
                await async_execute_db(
                    "UPDATE backup_settings SET admin_telegram_id=?, interval_hours=?, enabled=?, "
                    "s3_enabled=?, s3_endpoint=?, s3_region=?, s3_bucket=?, s3_access_key=?, "
                    "s3_prefix=? WHERE id=?",
                    (
                        admin_telegram_id, interval_hours, enabled,
                        s3_enabled, s3_endpoint, s3_region, s3_bucket,
                        s3_access_key, s3_prefix, row['id'],
                    ),
                )
            await flash('Настройки бэкапа сохранены.', 'success')
            return redirect(url_for('admin.settings_backup'))

        db_path = current_app.config.get('DATABASE_PATH', '')
        env_path = _backup_env_path(db_path) if db_path else None
        dev_db = _devices_db_path()
        devices_db_present = bool(dev_db and os.path.isfile(dev_db))
        return await render_template(
            'settings_backup.html',
            backup=row,
            db_path=db_path,
            db_size=_file_size(db_path),
            env_present=bool(env_path),
            env_path=env_path,
            env_size=_file_size(env_path),
            devices_db_present=devices_db_present,
            devices_db_size=_file_size(dev_db) if devices_db_present else None,
        )

    @admin_bp_instance.route('/manual_backup', methods=['POST'])
    async def manual_backup():
        from web_admin.run import current_user
        if not current_user.is_admin:
            return '', 403
        from web_admin.async_db import async_query_db, async_execute_db
        from aiogram.types import FSInputFile

        row = await async_query_db("SELECT * FROM backup_settings LIMIT 1", (), one=True)
        if not row:
            await flash('Настройки бэкапа не найдены.', 'danger')
            return redirect(url_for('admin.settings_backup'))

        admin_id = (row.get('admin_telegram_id') or '').strip()
        s3_enabled = int(row.get('s3_enabled') or 0)
        if s3_enabled:
            admin_id = ''
        if not admin_id and not s3_enabled:
            await flash('Укажите Telegram ID или включите S3.', 'warning')
            return redirect(url_for('admin.settings_backup'))

        db_path = current_app.config.get('DATABASE_PATH', '')
        if not db_path or not os.path.isfile(db_path):
            await flash('Файл БД не найден.', 'danger')
            return redirect(url_for('admin.settings_backup'))

        bot_token = None
        if admin_id:
            bot_token_row = await async_query_db(
                "SELECT value FROM settings WHERE key = 'bot_token'", (), one=True
            )
            bot_token = bot_token_row['value'] if bot_token_row else None
            if not bot_token and not s3_enabled:
                await flash('Bot token не настроен.', 'danger')
                return redirect(url_for('admin.settings_backup'))

        env_path = _backup_env_path(db_path)
        extra_dbs: list[str] = []
        if s3_enabled:
            dev_db = _devices_db_path()
            if dev_db and os.path.isfile(dev_db):
                extra_dbs.append(dev_db)

        temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
        temp_zip_path = temp_zip.name
        temp_zip.close()

        moscow_tz = pytz.timezone('Europe/Moscow')
        now = datetime.now(moscow_tz)

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: build_backup_zip(
                    db_path,
                    temp_zip_path,
                    env_path=env_path,
                    extra_db_paths=extra_dbs,
                ),
            )

            timestamp = now.strftime('%Y-%m-%d_%H-%M-%S')
            zip_filename = f'backup_{timestamp}.zip'
            included = ['БД']
            if extra_dbs:
                included.append('devices.db')
            if env_path:
                included.append('.env')

            results: list[str] = []
            errors: list[str] = []

            if admin_id and bot_token:
                try:
                    proxy_url = (app_conf.get('telegram_proxy_url') or '').strip() or None
                    bot = make_aiogram_bot(bot_token, proxy_url)
                    try:
                        await bot.send_document(
                            int(admin_id),
                            FSInputFile(temp_zip_path, filename=zip_filename),
                            caption=(
                                f'Ручной бэкап ({", ".join(included)})\n'
                                f'Дата: {now.strftime("%Y-%m-%d %H:%M:%S")} МСК'
                            ),
                        )
                        results.append('Telegram')
                    finally:
                        try:
                            await bot.session.close()
                        except Exception:
                            pass
                except Exception as e:
                    logger.error(f"[BACKUP] manual Telegram failed: {e}", exc_info=True)
                    errors.append(f'Telegram: {e}')

            if s3_enabled:
                try:
                    s3_key = await upload_file_async(
                        temp_zip_path,
                        endpoint_url=(row.get('s3_endpoint') or '').strip() or None,
                        region_name=(row.get('s3_region') or '').strip() or None,
                        bucket=(row.get('s3_bucket') or '').strip(),
                        access_key=(row.get('s3_access_key') or '').strip(),
                        secret_key=(row.get('s3_secret_key') or '').strip(),
                        prefix=(row.get('s3_prefix') or '').strip(),
                        filename=zip_filename,
                    )
                    results.append(f'S3 ({s3_key})')
                except S3NotConfigured as e:
                    errors.append(f'S3: {e}')
                except S3UploadError as e:
                    errors.append(f'S3: {e}')
                except Exception as e:
                    logger.error(f"[BACKUP] manual S3 failed: {e}", exc_info=True)
                    errors.append(f'S3: {e}')

            if results:
                await async_execute_db(
                    "UPDATE backup_settings SET last_backup=? WHERE id=?",
                    (now.strftime('%Y-%m-%d %H:%M:%S'), row['id']),
                )
                msg = f'Бэкап отправлен: {", ".join(results)}.'
                if errors:
                    msg += f' Ошибки: {"; ".join(errors)}'
                await flash(msg, 'success' if not errors else 'warning')
            else:
                await flash(f'Бэкап не отправлен. Ошибки: {"; ".join(errors)}', 'danger')
        except Exception as e:
            logger.error(f"[BACKUP] manual_backup error: {e}", exc_info=True)
            await flash(f'Ошибка бэкапа: {e}', 'danger')
        finally:
            try:
                os.unlink(temp_zip_path)
            except Exception:
                pass

        return redirect(url_for('admin.settings_backup'))

    # ── Доп возможности (подписка / помощь клиенту) ─────────────────────────

    # Ссылки на магазины приложений вырезаны вместе с группой «Подключение»:
    # роутеру клиент на телефон не нужен, ставить нечего.
    _SUBSCRIPTION_APP_LINK_KEYS = ()
    _SUBSCRIPTION_EXTRA_KEYS = ('sub_extra_links_active', 'sub_vless_bac')
    _SUBSCRIPTION_HELP_KEYS = ('help_photo_file_id', 'help_photo_local', 'help_text', 'help_buttons')

    @admin_bp_instance.route('/settings/subscription', methods=['GET', 'POST'])
    async def settings_subscription():
        from web_admin.run import current_user
        if not current_user.is_admin:
            return '', 403
        from web_admin.async_db import async_query_db, async_execute_db
        from web_admin.routes.news import _build_preview_url
        from web_admin.core.help_config import (
            DEFAULT_HELP_TEXT, build_buttons_json, get_effective_buttons,
        )

        all_keys = (
            _SUBSCRIPTION_APP_LINK_KEYS + _SUBSCRIPTION_EXTRA_KEYS + _SUBSCRIPTION_HELP_KEYS
        )
        placeholders = ','.join('?' for _ in all_keys)
        rows = await async_query_db(
            f"SELECT key, value, description FROM settings WHERE key IN ({placeholders})",
            all_keys,
        )
        by_key = {r['key']: r for r in (rows or [])}

        if request.method == 'POST':
            form = await request.form
            for key in _SUBSCRIPTION_APP_LINK_KEYS:
                if key in form:
                    val = (form.get(key) or '').strip()
                    if key in by_key:
                        await async_execute_db(
                            "UPDATE settings SET value = ? WHERE key = ?", (val, key)
                        )
                    else:
                        await async_execute_db(
                            "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                            (key, val, key),
                        )

            extra_links = [
                (link or '').strip()
                for link in form.getlist('sub_extra_links_active[]')
                if (link or '').strip()
            ]
            extra_json = json.dumps(extra_links, ensure_ascii=False)
            if 'sub_extra_links_active' in by_key:
                await async_execute_db(
                    "UPDATE settings SET value = ? WHERE key = ?", (extra_json, 'sub_extra_links_active')
                )
            else:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    ('sub_extra_links_active', extra_json, 'Доп. ссылки в подписке (JSON)'),
                )

            sub_vless_bac = (form.get('sub_vless_bac') or '').strip()
            if 'sub_vless_bac' in by_key:
                await async_execute_db(
                    "UPDATE settings SET value = ? WHERE key = ?", (sub_vless_bac, 'sub_vless_bac')
                )
            else:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                    ('sub_vless_bac', sub_vless_bac, 'Резервная VLESS-ссылка'),
                )

            help_photo_file_id = (form.get('help_photo_file_id') or '').strip()
            await async_execute_db(
                "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ('help_photo_file_id', help_photo_file_id, 'Telegram file_id фото-инструкции'),
            )
            if not help_photo_file_id:
                await async_execute_db(
                    "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ('help_photo_local', '', 'Локальный путь фото для превью'),
                )

            help_text = (form.get('help_text') or '').strip()
            await async_execute_db(
                "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ('help_text', help_text, 'Текст инструкции «Помощь клиенту»'),
            )

            help_buttons_json = build_buttons_json(lambda name: form.get(name))
            await async_execute_db(
                "INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ('help_buttons', help_buttons_json, 'JSON кнопок «Помощь клиенту»'),
            )

            await app_conf.load_settings()
            await reload_bot_settings()
            await flash('Настройки сохранены.', 'success')
            return redirect(url_for('admin.settings_subscription'))

        def _setting_dict(key: str) -> dict:
            row = by_key.get(key)
            return {
                'key': key,
                'value': (row['value'] if row else '') or '',
                'description': (row['description'] if row else key) or key,
            }

        grouped_settings = {
            'Доп возможности': [_setting_dict(k) for k in _SUBSCRIPTION_EXTRA_KEYS],
        }

        raw_extra = by_key.get('sub_extra_links_active', {}).get('value') or ''
        extra_links_list: list[str] = []
        if raw_extra:
            try:
                parsed = json.loads(raw_extra)
                if isinstance(parsed, list):
                    extra_links_list = [str(x) for x in parsed if x]
            except (ValueError, TypeError):
                pass

        help_photo_file_id = (by_key.get('help_photo_file_id', {}).get('value') or '').strip()
        help_photo_local = (by_key.get('help_photo_local', {}).get('value') or '').strip()
        help_text_value = (by_key.get('help_text', {}).get('value') or '').strip() or DEFAULT_HELP_TEXT
        help_buttons = get_effective_buttons(by_key.get('help_buttons', {}).get('value'))

        return await render_template(
            'settings_subscription.html',
            grouped_settings=grouped_settings,
            extra_links_list=extra_links_list,
            help_photo_file_id=help_photo_file_id,
            help_photo_preview_url=_build_preview_url(help_photo_local),
            help_text_value=help_text_value,
            help_buttons=help_buttons,
        )

