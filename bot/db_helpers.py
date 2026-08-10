import aiosqlite
import asyncio
from datetime import datetime, timedelta, timezone
import json
from typing import Optional, List, Dict, Any
from loguru import logger
from contextlib import asynccontextmanager

import os

from src.texts import TXT_PAYMENT_GRANT_FAILED, TXT_PAYMENT_TRAFFIC_GRANT_FAILED

from config import DATABASE_NAME, migrate_remnawave_db_if_needed

_RW_SYNC_BATCH_SIZE = 2000


def utc_now_iso() -> str:
    """ISO-8601 UTC с tzinfo (+00:00) — единый формат created_at для бота и сайта."""
    return datetime.now(timezone.utc).isoformat()
_RW_SYNC_STAGGER_SEC = 0.05
_rw_sync_lock = asyncio.Lock()
_rw_sync_pending = False

# СЛОВАРЬ С НАСТРОЙКАМИ И ТЕКСТАМИ ПО УМОЛЧАНИЮ
_DEFAULT_SETTINGS = {}

# Единая функция подключения к БД с правильными настройками WAL
@asynccontextmanager
async def get_db_connection_safe():
    """
    Безопасное подключение к БД с единообразными настройками WAL.
    Используется для предотвращения конфликтов и потери данных.
    КРИТИЧЕСКИ ВАЖНО: Всегда устанавливаем WAL для безопасной параллельной работы.
    """
    conn = await aiosqlite.connect(DATABASE_NAME, timeout=30)
    try:
        # КРИТИЧЕСКИ ВАЖНО: Всегда устанавливаем WAL режим для безопасной параллельной работы
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.execute("PRAGMA busy_timeout=30000;")  # Унифицированный таймаут 30 сек
        await conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
    finally:
        await conn.close()


async def init_db():
    async with get_db_connection_safe() as db:
        try:
            # Настройки уже установлены в get_db_connection_safe
            pass
        except Exception:
            pass
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                real_username TEXT,
                xui_client_uuid TEXT,
                xui_client_email TEXT,
                subscription_end_date TEXT,
                is_trial_used INTEGER DEFAULT 0,
                current_server_id INTEGER,
                subscription_mode TEXT DEFAULT 'one',
                notified_expiring INTEGER DEFAULT 0,
                notified_expired INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                limit_ip INTEGER DEFAULT 0,
                invited_by INTEGER DEFAULT NULL,
                invited_by_method TEXT DEFAULT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Миграция: добавляем поле real_username, если его еще нет
        try:
            await db.execute('ALTER TABLE users ADD COLUMN real_username TEXT')
        except aiosqlite.OperationalError:
            pass  # Поле уже существует
        try:
            await db.execute("ALTER TABLE users ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP")
        except aiosqlite.OperationalError: pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN subscription_mode TEXT DEFAULT 'one'")
        except aiosqlite.OperationalError: pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN invited_by_method TEXT DEFAULT NULL")
        except aiosqlite.OperationalError: pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN user_tag TEXT DEFAULT NULL")
        except aiosqlite.OperationalError: pass
        # Partner program columns (migrations)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN partner_balance_rub REAL DEFAULT 0")
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN partner_ref_code TEXT")
        except aiosqlite.OperationalError:
            pass
        # Remnawave integration: используем существующие поля xui_client_uuid и xui_client_email
        # subscription_provider — legacy-колонка, больше не используется в логике
        try:
            await db.execute("ALTER TABLE users ADD COLUMN subscription_provider TEXT DEFAULT 'xui'")
        except aiosqlite.OperationalError:
            pass
        # Отдельные поля для Remnawave: username (без домена) и short_uuid
        try:
            await db.execute("ALTER TABLE users ADD COLUMN remnawave_username TEXT")
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN remnawave_short_uuid TEXT")
        except aiosqlite.OperationalError:
            pass
        # Индивидуальная настройка показа кнопки партнёрской программы
        try:
            await db.execute("ALTER TABLE users ADD COLUMN show_partner_program_button TEXT DEFAULT NULL")
        except aiosqlite.OperationalError:
            pass
        # Индивидуальный процент партнёра
        try:
            await db.execute("ALTER TABLE users ADD COLUMN partner_percent_rub REAL DEFAULT NULL")
        except aiosqlite.OperationalError:
            pass
        # Флаг получения бесплатного продления
        try:
            await db.execute("ALTER TABLE users ADD COLUMN free_renewal_used INTEGER DEFAULT 0")
        except aiosqlite.OperationalError:
            pass
        # Веб-регистрация
        try:
            await db.execute("ALTER TABLE users ADD COLUMN email TEXT")
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute("ALTER TABLE web_auth_tokens ADD COLUMN attempts INTEGER DEFAULT 0")
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute("ALTER TABLE web_auth_tokens ADD COLUMN ref_cookie TEXT DEFAULT ''")
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN registration_type TEXT DEFAULT 'telegram'")
        except aiosqlite.OperationalError:
            pass
        # Remnawave: трафик и онлайн (синхронизируются из remnawave.db)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN total_bytes INTEGER DEFAULT 0")
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN total_total_bytes INTEGER DEFAULT 0")
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN online_at TEXT")
        except aiosqlite.OperationalError:
            pass
        # Ускоряет get_user_by_uuid / get_users_by_subscription_uuids при большой таблице users
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_xui_client_uuid ON users(xui_client_uuid) "
            "WHERE xui_client_uuid IS NOT NULL AND xui_client_uuid != ''"
        )
        await db.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY, telegram_id INTEGER, amount REAL, currency TEXT,
                status TEXT DEFAULT 'pending', created_at TEXT, metadata_json TEXT,
                FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
            )
        ''')
        # Миграция: флаг отправленного PWA push-уведомления (для polling-задачи в админке).
        # Один раз отправили → больше не повторяем.
        try:
            await db.execute("ALTER TABLE payments ADD COLUMN pwa_notified INTEGER DEFAULT 0")
        except aiosqlite.OperationalError:
            pass
        # Подписки устройств на PWA push-уведомления (админ/модератор).
        # Хранятся НАВСЕГДА — до явного отписания пользователем или 410 Gone от push-сервиса.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS pwa_push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_user_id TEXT NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh_key TEXT NOT NULL,
                auth_key TEXT NOT NULL,
                user_agent TEXT,
                events_mask INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT,
                last_error TEXT,
                failed_count INTEGER DEFAULT 0
            )
        ''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_pwa_push_admin ON pwa_push_subscriptions(admin_user_id)')
        # История партнёрских начислений
        await db.execute('''
            CREATE TABLE IF NOT EXISTS partner_accruals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                partner_id INTEGER NOT NULL,
                payer_id INTEGER NOT NULL,
                payment_id TEXT,
                amount REAL,
                currency TEXT,
                percent INTEGER,
                bonus REAL,
                comment TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY, is_active INTEGER DEFAULT 1,
                activated_by_telegram_id INTEGER, activated_at TEXT, created_at TEXT,
                days INTEGER DEFAULT 30,
                max_uses INTEGER NOT NULL DEFAULT 1,
                used_count INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (activated_by_telegram_id) REFERENCES users (telegram_id)
            )
        ''')
        # Таблица фиксации активаций промокодов: один пользователь не может активировать один и тот же код более одного раза
        await db.execute('''
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                code TEXT,
                telegram_id INTEGER,
                used_at TEXT,
                PRIMARY KEY (code, telegram_id)
            )
        ''')
        # Миграции для старых баз: добавление недостающих колонок
        try:
            await db.execute("ALTER TABLE promo_codes ADD COLUMN days INTEGER DEFAULT 30")
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute("ALTER TABLE promo_codes ADD COLUMN max_uses INTEGER NOT NULL DEFAULT 1")
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute("ALTER TABLE promo_codes ADD COLUMN used_count INTEGER NOT NULL DEFAULT 0")
        except aiosqlite.OperationalError:
            pass
        await db.execute('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, description TEXT)
        ''')

        # Шаблоны рассылок. В коде поставщика эту таблицу не создаёт никто —
        # есть только ALTER TABLE в web_admin/routes/news.py, то есть она
        # предполагалась уже существующей в поставляемой базе. На базе,
        # созданной с нуля, страница клиентов падала с «no such table».
        await db.execute('''
            CREATE TABLE IF NOT EXISTS news_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                body TEXT,
                custom_btn_text TEXT,
                custom_btn_url TEXT,
                media_kind TEXT,
                media_file_id TEXT,
                media_local_path TEXT,
                media_meta_json TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        ''')
        # Magic-link токены для веб-авторизации
        await db.execute('''
            CREATE TABLE IF NOT EXISTS web_auth_tokens (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT NOT NULL,
                token      TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                used       INTEGER DEFAULT 0,
                attempts   INTEGER DEFAULT 0,
                ref_cookie TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Таблица для логирования ошибок восстановления клиентов
        await db.execute('''
            CREATE TABLE IF NOT EXISTS client_recreation_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                client_uuid TEXT,
                server_id INTEGER,
                server_name TEXT,
                error_type TEXT,
                error_message TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                recovery_status TEXT,
                recovered_at TEXT,
                FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
            )
        ''')
        # Добавляем поля recovery_status и recovered_at, если их еще нет (для существующих БД)
        try:
            await db.execute('ALTER TABLE client_recreation_errors ADD COLUMN recovery_status TEXT')
        except Exception:
            pass  # Поле уже существует
        try:
            await db.execute('ALTER TABLE client_recreation_errors ADD COLUMN recovered_at TEXT')
        except Exception:
            pass  # Поле уже существует
        await db.execute('''
            CREATE TABLE IF NOT EXISTS tariffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, days INTEGER NOT NULL,
                price REAL NOT NULL, currency TEXT DEFAULT 'RUB', is_active INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 0, description TEXT, traffic_gb INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Добавляем поле traffic_gb, если его еще нет (для существующих БД)
        try:
            await db.execute("ALTER TABLE tariffs ADD COLUMN traffic_gb INTEGER DEFAULT 0")
        except Exception:
            pass  # Поле уже существует
        await db.execute('''
            CREATE TABLE IF NOT EXISTS traffic_topup_tariffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                traffic_gb INTEGER NOT NULL,
                price REAL NOT NULL,
                is_active INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 0,
                description TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS device_limit_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                old_limit INTEGER NOT NULL,
                new_limit INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'admin',
                payment_id TEXT,
                reason TEXT,
                changed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        try:
            await db.execute('CREATE INDEX IF NOT EXISTS idx_device_limit_changes_user ON device_limit_changes(telegram_id, changed_at DESC)')
        except Exception:
            pass
        try:
            await db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_device_limit_changes_payment ON device_limit_changes(payment_id) WHERE payment_id IS NOT NULL')
        except Exception:
            pass
        await db.commit()
    await populate_default_settings()
    await populate_default_tariffs()
    await init_referral_bonus_table()
    logger.info("База данных инициализирована.")

async def populate_default_settings():
    async with get_db_connection_safe() as db:
        for key, (value, description) in _DEFAULT_SETTINGS.items():
            await db.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)", (key, str(value), description))
        
        # Добавляем настройки для защиты от ботов
        bot_protection_settings = [
            ('bot_protection_enabled', '1', 'Включить защиту от ботов (0/1)'),
            ('bot_protection_text', '🤖 <b>Защита от ботов</b>\n\nДля продолжения решите простую задачу:\n\n<b>{question}</b>', 'Текст для защиты от ботов'),
            ('bot_protection_success_text', '✅ <b>Правильно!</b>\n\n⏳ Идет регистрация пробного периода, пожалуйста подождите...', 'Текст при правильном ответе'),
            ('bot_protection_wrong_text', '❌ <b>Неправильно!</b>\n\nПопробуйте еще раз:\n\n<b>{question}</b>', 'Текст при неправильном ответе'),
        ]
        
        # Добавляем настройки для пробного периода
        trial_settings = [
            ('trial_days', '3', 'Количество дней пробного периода'),
            ('trial_limit_ip', '1', 'Лимит устройств при пробном периоде (0 = без лимита)'),
            ('text_trial_success', '🎉 <b>Пробный период активирован!</b>\n\n⏱ <b>Длительность:</b> {days} дней\n\n📅 <b>Действует до:</b> {expiry_date}\n\n💡 <b>Для продления подписки используйте кнопку "Продлить подписку"</b>', 'Текст успешной активации пробного периода. Переменные: {days}, {expiry_date}'),
        ]
        
        for key, value, description in bot_protection_settings:
            await db.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)", (key, value, description))
        
        for key, value, description in trial_settings:
            await db.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)", (key, value, description))

        # Настройки фичи "Расширение лимита устройств" (платный апгрейд для клиентов).
        # Меняется ТОЛЬКО users.limit_ip — на X-UI/Remnawave новый лимит уезжает
        # при следующем продлении/обновлении подписки.
        device_upgrade_settings = [
            ('device_upgrade_enabled', '0', 'Включить продажу расширения лимита устройств клиентам (0/1)'),
            ('device_upgrade_max_limit', '20', 'Максимальный лимит, до которого клиент может расшириться через бота'),
            ('device_upgrade_min_days_left', '3', 'Минимум дней подписки, чтобы разрешить апгрейд'),
            ('device_upgrade_min_price', '30', 'Минимальный чек апгрейда (₽), цена ниже округляется вверх'),
            ('device_upgrade_price_per_slot_per_day', '5', 'Цена за 1 устройство в 1 день (₽)'),
        ]
        for key, value, description in device_upgrade_settings:
            await db.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)", (key, value, description))

        maintenance_settings = [
            ('bot_maintenance_enabled', '0', 'Сервисный режим: бот отвечает заглушкой на сообщения и кнопки (0/1)'),
            ('bot_maintenance_message', 'К сожалению, бот находится на технических работах. Попробуйте позже.', 'Текст заглушки сервисного режима'),
        ]
        for key, value, description in maintenance_settings:
            await db.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)", (key, value, description))
        
        # Добавляем настройки для управления кнопками в боте
        button_control_settings = [
            ('show_change_server_button', '1', 'Показывать кнопку "Сменить сервер" (0/1)'),
            ('show_multi_server_button', '1', 'Показывать кнопку "Включить режим Локаций" (0/1)'),
        ]
        
        # Добавляем настройки для управления методами оплаты
        payment_methods_settings = [
            ('show_payment_yookassa', '1', 'Показывать метод оплаты YooKassa'),
            ('show_payment_tgstar', '1', 'Показывать метод оплаты TG Star'),
            ('show_payment_cryptobot', '1', 'Показывать метод оплаты CryptoBot'),
            ('show_payment_yoomoney', '1', 'Показывать метод оплаты YooMoney'),
            ('show_payment_promo_code', '1', 'Показывать метод оплаты "Оплатить кодом"'),
            ('tgstar_rub_per_star', '2.0',
             'Курс TG Stars: сколько ₽ за 1 ⭐. Используется для конвертации '
             'рублёвых тарифов в звёзды при создании инвойса.'),
        ]
        
        for key, value, description in button_control_settings:
            await db.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)", (key, value, description))
        
        for key, value, description in payment_methods_settings:
            await db.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)", (key, value, description))

        import json as _json
        from payment_methods import DEFAULT_BOT_ORDER, SETTINGS_ORDER_KEY
        from button_registry import DEFAULT_MAIN_MENU_LAYOUT, MAIN_MENU_LAYOUT_SETTING
        layout_seed_settings = [
            (SETTINGS_ORDER_KEY, _json.dumps(DEFAULT_BOT_ORDER), 'Порядок платёжных методов в боте и кабинете (JSON-массив id)'),
            (MAIN_MENU_LAYOUT_SETTING, _json.dumps(DEFAULT_MAIN_MENU_LAYOUT), 'Раскладка главного меню бота (JSON: массив рядов с ключами кнопок)'),
        ]
        for key, value, description in layout_seed_settings:
            await db.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)", (key, value, description))
        
        # Добавляем настройки для реферальной системы
        referral_settings = [
            ('ref_bonus_on_join_days', '3', 'Количество дней бонуса за первое подключение приглашённого'),
            ('text_ref_bonus_on_join', '🎁 <b>Реферальный бонус!</b>\n\nВы получили {days} дней бонуса за первое подключение приглашённого друга!', 'Текст уведомления пригласившему за первое подключение. Переменные: {days}'),
        ]
        
        for key, value, description in referral_settings:
            await db.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)", (key, value, description))
        
        # Добавляем недостающие тексты
        missing_texts = [
            ('text_error_creating_user', '❌ <b>Ошибка создания пользователя</b>\n\nНе удалось создать пробный период. Попробуйте позже или обратитесь в поддержку.', 'Текст ошибки создания пользователя'),
            ('text_payment_grant_failed', TXT_PAYMENT_GRANT_FAILED, 'Оплата прошла, но подписку выдать не удалось (Remnawave недоступна и т.п.)'),
            ('text_payment_traffic_grant_failed', TXT_PAYMENT_TRAFFIC_GRANT_FAILED, 'Оплата прошла, но докупку GB в Remnawave применить не удалось'),
        ]
        
        for key, value, description in missing_texts:
            await db.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)", (key, value, description))

        # Partner program settings
        partner_settings = [
            ('partner_percent_rub', '10', 'Процент партнёра в RUB от успешных платежей приглашённых (0-100)'),
            ('bot_username', '', 'Имя Telegram-бота без @ для формирования ссылки партнёра'),
        ]
        for key, value, description in partner_settings:
            await db.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)", (key, value, description))
        
        await db.commit()

async def populate_default_tariffs():
    async with get_db_connection_safe() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM tariffs")
        if (await cursor.fetchone())[0] == 0:
            await db.execute('''
                INSERT INTO tariffs (name, days, price, currency, is_active, sort_order, description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', ("Стандартный тариф (30 дней)", 30, 79.00, 'RUB', 1, 0, "Стандартная подписка на 30 дней"))
            await db.commit()
            logger.info("Создан стандартный тариф по умолчанию.")

async def init_referral_bonus_table():
    async with get_db_connection_safe() as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS referral_payment_bonus (
                ref_user_id INTEGER, invited_user_id INTEGER, PRIMARY KEY (ref_user_id, invited_user_id)
            )
        ''')
        await db.commit()

async def load_all_settings() -> Dict[str, str]:
    async with get_db_connection_safe() as db:
        async with db.execute("SELECT key, value FROM settings") as cursor:
            return {row[0]: row[1] for row in await cursor.fetchall()}

async def get_user(telegram_id: int) -> Optional[dict]:
    """Получает данные пользователя из БД. Возвращает словарь для удобства доступа к полям."""
    async with get_db_connection_safe() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def get_user_by_uuid(uuid: str) -> Optional[Dict]:
    """
    Получает пользователя по xui_client_uuid.
    Возвращает словарь с данными пользователя или None.
    """
    async with get_db_connection_safe() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE xui_client_uuid = ?", (uuid,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None


async def get_users_by_subscription_uuids(uuids: List[str]) -> Dict[str, Dict]:
    """
    Пакетно загружает пользователей по xui_client_uuid (ключ результата — UUID строкой как в БД).
    """
    uniq = [u for u in dict.fromkeys(uuids) if u]
    if not uniq:
        return {}
    placeholders = ','.join('?' * len(uniq))
    sql = f'''
        SELECT telegram_id, username, xui_client_uuid
        FROM users
        WHERE xui_client_uuid IN ({placeholders})
    '''
    async with get_db_connection_safe() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, uniq) as cursor:
            rows = await cursor.fetchall()
    out: Dict[str, Dict] = {}
    for row in rows:
        xid = row['xui_client_uuid']
        if xid:
            out[str(xid)] = {
                'telegram_id': row['telegram_id'],
                'username': row['username'] or '',
            }
    return out


async def get_active_subscription(telegram_id: int) -> Optional[Dict]:
    user_data = await get_user(telegram_id)
    if user_data and user_data["subscription_end_date"]:
        try:
            sub_end_date = datetime.fromisoformat(user_data["subscription_end_date"])
            if sub_end_date > datetime.now(sub_end_date.tzinfo):
                result = dict(user_data)
                result['subscription_end_date'] = sub_end_date
                return result
        except (ValueError, KeyError, TypeError) as e:
            logger.error(f"Некорректный формат активной подписки для {telegram_id}: {e}")
    return None

async def get_last_subscription(telegram_id: int) -> Optional[Dict]:
    user_data = await get_user(telegram_id)
    if user_data and user_data["xui_client_uuid"]:
        try:
            result = dict(user_data)
            if user_data["subscription_end_date"]:
                result['subscription_end_date'] = datetime.fromisoformat(user_data["subscription_end_date"])
            else:
                result['subscription_end_date'] = None
            return result
        except (ValueError, KeyError, TypeError) as e:
            logger.error(f"Некорректный формат последней подписки для {telegram_id}: {e}")
    return None

async def add_user(telegram_id: int, username: str = None, real_username: str = None):
    """
    Добавляет пользователя в БД или обновляет его данные.
    
    Args:
        telegram_id: ID пользователя в Telegram
        username: Имя пользователя (first_name) - сохраняется в поле username
        real_username: Username из Telegram (@username) - сохраняется в поле real_username
    """
    created_at_str = datetime.now(timezone.utc).isoformat()
    async with get_db_connection_safe() as db:
        # При создании нового пользователя сохраняем оба значения
        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username, real_username, created_at) VALUES (?, ?, ?, ?)",
            (telegram_id, username, real_username, created_at_str)
        )
        # Обновляем username и real_username для существующих пользователей
        await db.execute(
            "UPDATE users SET username = ?, real_username = ? WHERE telegram_id = ?",
            (username, real_username, telegram_id)
        )
        await db.commit()

async def update_user_subscription(telegram_id: int, xui_client_uuid: str, xui_client_email: str,
                                   subscription_end_date: datetime, server_id: int, is_trial: bool = False, limit_ip: int = 0):
    if subscription_end_date.tzinfo is None:
        subscription_end_date = subscription_end_date.astimezone()
    end_date_str = subscription_end_date.isoformat()
    async with get_db_connection_safe() as db:
        await db.execute(
            """UPDATE users 
               SET xui_client_uuid = ?, xui_client_email = ?, subscription_end_date = ?, 
                   is_trial_used = CASE WHEN ? THEN 1 ELSE is_trial_used END,
                   current_server_id = ?, limit_ip = ?
               WHERE telegram_id = ?""",
            (xui_client_uuid, xui_client_email, end_date_str, 1 if is_trial else 0, server_id, limit_ip, telegram_id)
        )
        if is_trial:
            await db.execute("UPDATE users SET is_trial_used = 1 WHERE telegram_id = ?", (telegram_id,))
        await db.execute("UPDATE users SET notified_expiring = 0, notified_expired = 0 WHERE telegram_id = ?", (telegram_id,))
        await db.commit()
    logger.info(f"Подписка для {telegram_id} обновлена до: {end_date_str}")

async def update_user_subscription_remnawave(
    telegram_id: int,
    remnawave_user_uuid: str,
    remnawave_username: str,
    remnawave_short_uuid: str,
    subscription_end_date: datetime,
    is_trial: bool = False,
    preserve_xui_uuid: bool = False,
    migration_mode: bool = False,
    limit_ip: Optional[int] = None
):
    """
    Обновляет подписку пользователя через Remnawave.
    Сохраняет:
    - xui_client_uuid: полный UUID Remnawave (если preserve_xui_uuid=False) или сохраняет существующий UUID (если preserve_xui_uuid=True)
    - xui_client_email: email клиента (legacy-имя колонки)
    - remnawave_username: username без домена (например, tg246509711)
    - remnawave_short_uuid: short_uuid из Remnawave
    
    Args:
        migration_mode: Если True, не изменяет is_trial_used и флаги уведомлений (notified_expiring, notified_expired).
                       Используется при миграции, когда нужно только перенести данные без изменения исторической информации.
    """
    if subscription_end_date.tzinfo is None:
        subscription_end_date = subscription_end_date.astimezone()
    end_date_str = subscription_end_date.isoformat()
    async with get_db_connection_safe() as db:
        if preserve_xui_uuid:
            # Не перезаписываем xui_client_uuid, если нужно сохранить существующий UUID
            if migration_mode:
                # При миграции не изменяем is_trial_used
                if limit_ip is not None:
                    # Обновляем limit_ip только если он явно передан
                    await db.execute(
                        """UPDATE users 
                           SET remnawave_username = ?, remnawave_short_uuid = ?,
                               subscription_end_date = ?,
                               limit_ip = ?
                           WHERE telegram_id = ?""",
                        (remnawave_username, remnawave_short_uuid, end_date_str, limit_ip, telegram_id)
                    )
                else:
                    # Не обновляем limit_ip, если он не передан
                    await db.execute(
                        """UPDATE users 
                           SET remnawave_username = ?, remnawave_short_uuid = ?,
                               subscription_end_date = ?
                           WHERE telegram_id = ?""",
                        (remnawave_username, remnawave_short_uuid, end_date_str, telegram_id)
                    )
            else:
                # При обычном обновлении подписки обновляем is_trial_used
                if limit_ip is not None:
                    # Обновляем limit_ip только если он явно передан
                    await db.execute(
                        """UPDATE users 
                           SET remnawave_username = ?, remnawave_short_uuid = ?,
                               subscription_end_date = ?,
                               is_trial_used = CASE WHEN ? THEN 1 ELSE is_trial_used END,
                               limit_ip = ?
                           WHERE telegram_id = ?""",
                        (remnawave_username, remnawave_short_uuid, end_date_str, 1 if is_trial else 0, limit_ip, telegram_id)
                    )
                else:
                    # Не обновляем limit_ip, если он не передан
                    await db.execute(
                        """UPDATE users 
                           SET remnawave_username = ?, remnawave_short_uuid = ?,
                               subscription_end_date = ?,
                               is_trial_used = CASE WHEN ? THEN 1 ELSE is_trial_used END
                           WHERE telegram_id = ?""",
                        (remnawave_username, remnawave_short_uuid, end_date_str, 1 if is_trial else 0, telegram_id)
                    )
        else:
            # Перезаписываем xui_client_uuid на UUID Remnawave
            if migration_mode:
                # При миграции не изменяем is_trial_used
                if limit_ip is not None:
                    # Обновляем limit_ip только если он явно передан
                    await db.execute(
                        """UPDATE users 
                           SET xui_client_uuid = ?, remnawave_username = ?, remnawave_short_uuid = ?,
                               subscription_end_date = ?,
                               limit_ip = ?
                           WHERE telegram_id = ?""",
                        (remnawave_user_uuid, remnawave_username, remnawave_short_uuid, end_date_str, limit_ip, telegram_id)
                    )
                else:
                    # Не обновляем limit_ip, если он не передан
                    await db.execute(
                        """UPDATE users 
                           SET xui_client_uuid = ?, remnawave_username = ?, remnawave_short_uuid = ?,
                               subscription_end_date = ?
                           WHERE telegram_id = ?""",
                        (remnawave_user_uuid, remnawave_username, remnawave_short_uuid, end_date_str, telegram_id)
                    )
            else:
                # При обычном обновлении подписки обновляем is_trial_used
                if limit_ip is not None:
                    # Обновляем limit_ip только если он явно передан
                    await db.execute(
                        """UPDATE users 
                           SET xui_client_uuid = ?, remnawave_username = ?, remnawave_short_uuid = ?,
                               subscription_end_date = ?,
                               is_trial_used = CASE WHEN ? THEN 1 ELSE is_trial_used END,
                               limit_ip = ?
                           WHERE telegram_id = ?""",
                        (remnawave_user_uuid, remnawave_username, remnawave_short_uuid, end_date_str, 1 if is_trial else 0, limit_ip, telegram_id)
                    )
                else:
                    # Не обновляем limit_ip, если он не передан
                    await db.execute(
                        """UPDATE users 
                           SET xui_client_uuid = ?, remnawave_username = ?, remnawave_short_uuid = ?,
                               subscription_end_date = ?,
                               is_trial_used = CASE WHEN ? THEN 1 ELSE is_trial_used END
                           WHERE telegram_id = ?""",
                        (remnawave_user_uuid, remnawave_username, remnawave_short_uuid, end_date_str, 1 if is_trial else 0, telegram_id)
                    )
        
        # Обновляем is_trial_used только если не режим миграции и is_trial=True
        if not migration_mode and is_trial:
            await db.execute("UPDATE users SET is_trial_used = 1 WHERE telegram_id = ?", (telegram_id,))
        
        # Сбрасываем флаги уведомлений только если не режим миграции
        if not migration_mode:
            await db.execute("UPDATE users SET notified_expiring = 0, notified_expired = 0 WHERE telegram_id = ?", (telegram_id,))
        
        await db.commit()
    logger.info(f"Подписка Remnawave для {telegram_id} обновлена до: {end_date_str}")

# Функция содержала лишний код. Теперь она исправлена.
async def update_user_subscription_mode(telegram_id: int, mode: str):
    """Обновляет режим подписки пользователя ('one' или 'multi')."""
    if mode not in ['one', 'multi']:
        logger.error(f"Недопустимый режим подписки '{mode}' для пользователя {telegram_id}")
        return
    async with get_db_connection_safe() as db:
        await db.execute("UPDATE users SET subscription_mode = ? WHERE telegram_id = ?", (mode, telegram_id))
        await db.commit()

async def update_user_current_server(telegram_id: int, server_id: int):
    """Обновляет текущий сервер пользователя"""
    async with get_db_connection_safe() as db:
        await db.execute("UPDATE users SET current_server_id = ? WHERE telegram_id = ?", (server_id, telegram_id))
        await db.commit()

async def deactivate_user(telegram_id: int):
    async with get_db_connection_safe() as db:
        await db.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
        await db.commit()
    logger.warning(f"Пользователь {telegram_id} деактивирован.")


async def get_active_subscription(telegram_id: int) -> Optional[Dict]:
    """
    Получает активную подписку, используя get_user и преобразуя дату.
    """
    user_data = await get_user(telegram_id)
    if user_data and user_data["subscription_end_date"]:
        try:
            sub_end_date = datetime.fromisoformat(user_data["subscription_end_date"])
            if sub_end_date > datetime.now(sub_end_date.tzinfo):
                result = dict(user_data)
                result['subscription_end_date'] = sub_end_date
                return result
        except (ValueError, KeyError, TypeError) as e:
            logger.error(f"Некорректный формат активной подписки для {telegram_id}: {e}")
    return None

async def add_payment(payment_id: str, telegram_id: int, amount: float, currency: str, metadata_json: Optional[str] = None):
    created_at_str = datetime.now(timezone.utc).isoformat() # Используем UTC для created_at
    async with get_db_connection_safe() as db:
        await db.execute(
            "INSERT INTO payments (payment_id, telegram_id, amount, currency, created_at, status, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (payment_id, telegram_id, amount, currency, created_at_str, 'pending', metadata_json)
        )
        await db.commit()
    logger.info(f"Платеж {payment_id} для {telegram_id} создан. Метаданные: {metadata_json}")

async def get_payment(payment_id: str):
    async with get_db_connection_safe() as db:
        async with db.execute("SELECT payment_id, telegram_id, amount, currency, status, created_at, metadata_json FROM payments WHERE payment_id = ?", (payment_id,)) as cursor:
            return await cursor.fetchone()

async def update_payment_status(payment_id: str, status: str):
    async with get_db_connection_safe() as db:
        await db.execute("UPDATE payments SET status = ? WHERE payment_id = ?", (status, payment_id))
        await db.commit()
    logger.info(f"Статус платежа {payment_id} обновлен на {status}.")

async def try_mark_payment_as_processing(payment_id: str) -> bool:
    """
    Атомарно пытается пометить платеж как обрабатываемый (status = 'processing').
    Возвращает True, если платеж был успешно помечен (т.е. он еще не был обработан),
    False если платеж уже был обработан (status = 'succeeded') или обрабатывается (status = 'processing').
    
    Используется для предотвращения race condition при обработке webhook'ов.
    """
    async with get_db_connection_safe() as db:
        # Сначала проверяем текущий статус для логирования
        async with db.execute(
            "SELECT status FROM payments WHERE payment_id = ?",
            (payment_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                old_status = row[0]
            else:
                old_status = None
        
        # Пытаемся обновить статус только если он НЕ 'succeeded' и НЕ 'processing'
        cursor = await db.execute(
            "UPDATE payments SET status = 'processing' WHERE payment_id = ? AND status NOT IN ('succeeded', 'processing')",
            (payment_id,)
        )
        await db.commit()
        rows_affected = cursor.rowcount
        
        if rows_affected > 0:
            logger.info(f"Статус платежа {payment_id} изменен с '{old_status}' на 'processing' (webhook начал обработку)")
            return True
        else:
            if old_status == 'succeeded':
                logger.debug(f"Платеж {payment_id} уже обработан (status='succeeded'), повторная обработка отклонена")
            elif old_status == 'processing':
                logger.debug(f"Платеж {payment_id} уже обрабатывается (status='processing'), повторная обработка отклонена")
            else:
                logger.warning(f"Не удалось изменить статус платежа {payment_id} на 'processing' (текущий статус: {old_status})")
            return False

async def delete_xui_user_db_record(telegram_id: int):
    async with get_db_connection_safe() as db:
        await db.execute(
            """UPDATE users 
               SET xui_client_uuid = NULL, xui_client_email = NULL, subscription_end_date = NULL, current_server_id = NULL
               WHERE telegram_id = ?""",
            (telegram_id,)
        )
        await db.commit()
    logger.info(f"Запись о XUI пользователе для {telegram_id} удалена из БД (но не подписка).")

async def get_pending_payments(limit: int = 100):
    """Получает список платежей со статусом 'pending'."""
    async with get_db_connection_safe() as db:
        async with db.execute(
                "SELECT payment_id, telegram_id, amount, currency, status, created_at, metadata_json "
                "FROM payments WHERE status = 'pending' ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cursor:
            return await cursor.fetchall()

async def get_total_users_count() -> int:
    """Получить общее количество пользователей"""
    async with get_db_connection_safe() as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_trial_users_count() -> int:
    async with get_db_connection_safe() as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_trial_used = 1") as cursor:
            return (await cursor.fetchone())[0]

def parse_subscription_end_utc(end_raw: Any) -> Optional[datetime]:
    """Разбор subscription_end_date в UTC (как при показе в админке)."""
    if end_raw is None:
        return None
    s = str(end_raw).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_subscription_end_active(end_raw: Any, *, now: datetime | None = None) -> bool:
    """Подписка активна, если дата окончания строго в будущем (UTC)."""
    dt = parse_subscription_end_utc(end_raw)
    if dt is None:
        return False
    ref = now if now is not None else datetime.now(timezone.utc)
    return dt > ref


def _active_subscription_prefilter_where(*, require_uuid: bool = False, exclude_blocked: bool = True) -> str:
    """SQL-отбор кандидатов без сравнения даты (дата проверяется в Python)."""
    parts = [
        "subscription_end_date IS NOT NULL",
        "TRIM(subscription_end_date) != ''",
    ]
    if exclude_blocked:
        parts.append("COALESCE(is_blocked, 0) = 0")
    if require_uuid:
        parts.extend([
            "xui_client_uuid IS NOT NULL",
            "TRIM(COALESCE(xui_client_uuid, '')) != ''",
        ])
    return " AND ".join(parts)


def _active_subscription_where(*, require_uuid: bool = False, exclude_blocked: bool = True) -> str:
    """Устаревший SQL-only фильтр; оставлен для совместимости, не использовать для подсчёта."""
    parts = [
        "subscription_end_date IS NOT NULL",
        "TRIM(subscription_end_date) != ''",
        "datetime(subscription_end_date) > datetime('now', 'utc')",
    ]
    if exclude_blocked:
        parts.append("COALESCE(is_blocked, 0) = 0")
    if require_uuid:
        parts.extend([
            "xui_client_uuid IS NOT NULL",
            "TRIM(xui_client_uuid) != ''",
        ])
    return " AND ".join(parts)


async def _fetch_active_subscription_candidates(
    *,
    require_uuid: bool = False,
    exclude_blocked: bool = True,
) -> List[Dict]:
    where = _active_subscription_prefilter_where(
        require_uuid=require_uuid,
        exclude_blocked=exclude_blocked,
    )
    async with get_db_connection_safe() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f'''
            SELECT telegram_id, username, xui_client_uuid, subscription_end_date
            FROM users
            WHERE {where}
            ORDER BY telegram_id
        ''') as cursor:
            rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def count_active_subscription_users(
    *,
    require_uuid: bool = False,
    exclude_blocked: bool = True,
) -> int:
    """Количество пользователей с неистёкшей подпиской."""
    now = datetime.now(timezone.utc)
    rows = await _fetch_active_subscription_candidates(
        require_uuid=require_uuid,
        exclude_blocked=exclude_blocked,
    )
    return sum(
        1 for row in rows
        if is_subscription_end_active(row.get('subscription_end_date'), now=now)
    )


async def get_active_subscription_users(*, require_uuid: bool = True) -> List[Dict]:
    """Список пользователей с активной подпиской (для отчётов по VPN-обновлениям)."""
    now = datetime.now(timezone.utc)
    rows = await _fetch_active_subscription_candidates(require_uuid=require_uuid, exclude_blocked=True)
    out: List[Dict] = []
    for row in rows:
        if not is_subscription_end_active(row.get('subscription_end_date'), now=now):
            continue
        out.append({
            'uuid': str(row['xui_client_uuid']),
            'telegram_id': row['telegram_id'],
            'username': row['username'] or '',
            'subscription_end_date': row['subscription_end_date'] or '',
        })
    return out


async def aggregate_subscription_stats(*, exclude_blocked: bool = True) -> Dict[str, Any]:
    """Сводка по статусам подписок и лимитам устройств (сравнение дат в Python UTC)."""
    now = datetime.now(timezone.utc)
    in_7d = now + timedelta(days=7)
    where = ""
    if exclude_blocked:
        where = "WHERE COALESCE(is_blocked, 0) = 0"

    async with get_db_connection_safe() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f'''
            SELECT subscription_end_date, COALESCE(limit_ip, 0) AS limit_ip
            FROM users
            {where}
        ''') as cursor:
            rows = await cursor.fetchall()

    active = expired = none = expiring_7d = 0
    limits_by_active: Dict[int, int] = {}

    for row in rows:
        end_raw = row['subscription_end_date']
        if end_raw is None or not str(end_raw).strip():
            none += 1
            continue

        dt = parse_subscription_end_utc(end_raw)
        if dt is None or dt <= now:
            expired += 1
            continue

        active += 1
        if dt <= in_7d:
            expiring_7d += 1
        lim = int(row['limit_ip'] or 0)
        limits_by_active[lim] = limits_by_active.get(lim, 0) + 1

    return {
        'subs_active': active,
        'subs_expired': expired,
        'subs_none': none,
        'subs_expiring_7d': expiring_7d,
        'limits_by_active': limits_by_active,
    }


async def get_active_subscriptions_count() -> int:
    """Подсчитывает количество активных подписок (включая заблокированных)."""
    return await count_active_subscription_users(exclude_blocked=False)

async def get_total_payments_count() -> int:
    """Получить общее количество платежей"""
    async with get_db_connection_safe() as db:
        async with db.execute("SELECT COUNT(*) FROM payments") as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_successful_payments_count() -> int:
    """Получить количество успешных платежей"""
    async with get_db_connection_safe() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM payments WHERE status = 'succeeded'"
        ) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_total_payments_amount() -> float:
    """Получить общую сумму успешных платежей"""
    async with get_db_connection_safe() as db:
        async with db.execute(
            "SELECT SUM(amount) FROM payments WHERE status = 'succeeded'"
        ) as cursor:
            result = await cursor.fetchone()
            return result[0] if result and result[0] else 0.0

async def get_user_payments(user_id: int) -> List[tuple]:
    """Получить историю платежей пользователя (без служебных колонок вроде pwa_notified)."""
    async with get_db_connection_safe() as db:
        async with db.execute(
            """
            SELECT payment_id, telegram_id, amount, currency, status, created_at, metadata_json
              FROM payments
             WHERE telegram_id = ?
             ORDER BY created_at DESC
            """,
            (user_id,)
        ) as cursor:
            return await cursor.fetchall()

async def get_user_successful_payments(user_id: int) -> List[tuple]:
    """Получить только успешные платежи пользователя (фиксированный набор полей для распаковки в боте)."""
    async with get_db_connection_safe() as db:
        async with db.execute(
            """
            SELECT payment_id, telegram_id, amount, currency, status, created_at, metadata_json
              FROM payments
             WHERE telegram_id = ? AND status = 'succeeded'
             ORDER BY created_at DESC
            """,
            (user_id,)
        ) as cursor:
            return await cursor.fetchall()

async def delete_user_subscription(user_id: int) -> bool:
    """Удалить подписку пользователя из Remnawave и очистить данные в БД."""
    from remnawave_manager import remnawave_manager_instance

    try:
        async with get_db_connection_safe() as db:
            async with db.execute(
                "SELECT xui_client_uuid FROM users WHERE telegram_id = ? AND datetime(subscription_end_date) > datetime('now', 'utc')",
                (user_id,)
            ) as cursor:
                sub = await cursor.fetchone()
                if not sub:
                    return False

                client_uuid = sub[0]

                if client_uuid:
                    try:
                        await remnawave_manager_instance.delete_user(client_uuid)
                        logger.info(f"[DELETE] Пользователь {user_id} удалён из Remnawave (UUID: {client_uuid})")
                    except Exception as e:
                        logger.warning(f"[DELETE] Ошибка при удалении пользователя {user_id} из Remnawave: {e}")

                await db.execute(
                    """UPDATE users 
                       SET xui_client_uuid = NULL, xui_client_email = NULL, 
                           subscription_end_date = NULL, current_server_id = NULL,
                           remnawave_username = NULL, remnawave_short_uuid = NULL
                       WHERE telegram_id = ?""",
                    (user_id,)
                )
                await db.commit()
                return True
    except Exception as e:
        logger.error(f"Ошибка при удалении подписки пользователя {user_id}: {e}")
        return False

async def get_users_list(limit: int = 50, offset: int = 0) -> List[tuple]:
    """Получить список пользователей с пагинацией"""
    async with get_db_connection_safe() as db:
        async with db.execute(
            """SELECT telegram_id, username, subscription_end_date, is_trial_used, current_server_id 
               FROM users 
               ORDER BY telegram_id DESC 
               LIMIT ? OFFSET ?""",
            (limit, offset)
        ) as cursor:
            return await cursor.fetchall()

async def get_users_count() -> int:
    """Получить общее количество пользователей для пагинации"""
    async with get_db_connection_safe() as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_server_config(server_id: int) -> Optional[dict]:
    from app_config import app_conf
    return next((s for s in app_conf.get('xui_servers', []) if s['id'] == server_id), None)

# Новая версия
async def get_last_subscription(telegram_id: int) -> Optional[Dict]:
    """
    Получает последнюю подписку (даже истекшую), используя get_user.
    """
    user_data = await get_user(telegram_id)
    if user_data and user_data["xui_client_uuid"]:
        try:
            result = dict(user_data)
            if user_data["subscription_end_date"]:
                result['subscription_end_date'] = datetime.fromisoformat(user_data["subscription_end_date"])
            else:
                result['subscription_end_date'] = None
            return result
        except (ValueError, KeyError, TypeError) as e:
            logger.error(f"Некорректный формат последней подписки для {telegram_id}: {e}")
    return None

async def get_all_users() -> List[tuple]:
    """Получает всех пользователей из БД (не только активных)."""
    async with get_db_connection_safe() as db:
        async with db.execute("SELECT telegram_id, username, xui_client_uuid, xui_client_email, subscription_end_date, is_trial_used, current_server_id FROM users ORDER BY telegram_id") as cursor:
            return await cursor.fetchall()

async def get_active_users_for_broadcast() -> List[tuple]:
    """Получает активных пользователей с непустым UUID для рассылки.
    Активными считаются те, у кого subscription_end_date > текущего времени (UTC).
    Исключает заблокированных пользователей."""
    async with get_db_connection_safe() as db:
        async with db.execute(
            """SELECT telegram_id, username, xui_client_uuid, xui_client_email, subscription_end_date, is_trial_used, current_server_id 
               FROM users 
               WHERE xui_client_uuid IS NOT NULL 
               AND xui_client_uuid != '' 
               AND subscription_end_date IS NOT NULL 
               AND datetime(subscription_end_date) > datetime('now', 'utc')
               AND COALESCE(is_blocked, 0) = 0
               ORDER BY telegram_id"""
        ) as cursor:
            return await cursor.fetchall()

async def get_expired_users_for_broadcast() -> List[tuple]:
    """Получает пользователей с истекшей подпиской и непустым UUID для рассылки.
    Истекшими считаются те, у кого subscription_end_date <= текущего времени (UTC) или NULL.
    Исключает заблокированных пользователей."""
    async with get_db_connection_safe() as db:
        async with db.execute(
            """SELECT telegram_id, username, xui_client_uuid, xui_client_email, subscription_end_date, is_trial_used, current_server_id 
               FROM users 
               WHERE xui_client_uuid IS NOT NULL 
               AND xui_client_uuid != '' 
               AND (subscription_end_date IS NULL OR datetime(subscription_end_date) <= datetime('now', 'utc'))
               AND COALESCE(is_blocked, 0) = 0
               ORDER BY telegram_id"""
        ) as cursor:
            return await cursor.fetchall()

async def get_all_users_for_broadcast() -> List[tuple]:
    """Получает всех пользователей с непустым UUID для рассылки (исключая пустые UUID).
    Исключает заблокированных пользователей."""
    async with get_db_connection_safe() as db:
        async with db.execute(
            """SELECT telegram_id, username, xui_client_uuid, xui_client_email, subscription_end_date, is_trial_used, current_server_id 
               FROM users 
               WHERE xui_client_uuid IS NOT NULL 
               AND xui_client_uuid != '' 
               AND COALESCE(is_blocked, 0) = 0
               ORDER BY telegram_id"""
        ) as cursor:
            return await cursor.fetchall()

async def get_all_xui_users_for_restore() -> List[Dict]:
    """
    Получает всех пользователей, у которых есть UUID в X-UI, для восстановления (включая неактивных).
    Возвращает список словарей для удобства.
    """
    async with get_db_connection_safe() as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT
                telegram_id,
                xui_client_uuid,
                xui_client_email,
                subscription_end_date,
                current_server_id
            FROM
                users
            WHERE
                xui_client_uuid IS NOT NULL AND xui_client_uuid != ''
        """
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

# --- Функции для работы с промокодами (остаются без изменений) ---

async def add_promo_code(code: str) -> bool:
    created_at_str = datetime.now(timezone.utc).isoformat()
    async with get_db_connection_safe() as db:
        try:
            await db.execute(
                "INSERT INTO promo_codes (code, created_at, is_active) VALUES (?, ?, 1)",
                (code, created_at_str)
            )
            await db.commit()
            logger.info(f"Промокод {code} успешно добавлен в базу.")
            return True
        except aiosqlite.IntegrityError:
            logger.warning(f"Попытка добавить уже существующий промокод: {code}")
            return False

async def get_promo_code(code: str) -> Optional[tuple]:
    async with get_db_connection_safe() as db:
        async with db.execute("SELECT * FROM promo_codes WHERE code = ?", (code,)) as cursor:
            return await cursor.fetchone()

async def activate_promo_code(code: str, telegram_id: int):
    """Старая одноразовая активация (оставлена для совместимости где используется напрямую)."""
    activated_at_str = datetime.now(timezone.utc).isoformat()
    async with get_db_connection_safe() as db:
        await db.execute(
            "UPDATE promo_codes SET is_active = 0, activated_by_telegram_id = ?, activated_at = ? WHERE code = ?",
            (telegram_id, activated_at_str, code)
        )
        await db.commit()
        logger.info(f"Промокод {code} активирован пользователем {telegram_id}.")

async def redeem_promo_code(code: str, telegram_id: int) -> Dict:
    """Пытается активировать многоразовый промокод с лимитом.
    Возвращает {'ok': True, 'days': int} при успехе, иначе {'ok': False, 'error': 'reason'}.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with get_db_connection_safe() as db:
        await db.execute("PRAGMA foreign_keys=ON;")
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT code, is_active, days, max_uses, used_count FROM promo_codes WHERE code = ?", (code,)) as cursor:
                row = await cursor.fetchone()
            if not row:
                await db.execute("ROLLBACK")
                return {'ok': False, 'error': 'not_found'}
            _, is_active, days, max_uses, used_count = row
            if not is_active:
                await db.execute("ROLLBACK")
                return {'ok': False, 'error': 'inactive'}
            # Уже активировал этот код?
            async with db.execute("SELECT 1 FROM promo_redemptions WHERE code = ? AND telegram_id = ?", (code, telegram_id)) as cur:
                exists = await cur.fetchone()
            if exists:
                await db.execute("ROLLBACK")
                return {'ok': False, 'error': 'already_used_by_user'}
            # Есть ли свободные использования
            if max_uses and used_count >= max_uses:
                await db.execute("ROLLBACK")
                return {'ok': False, 'error': 'limit_reached'}
            # Регистрируем использование и инкрементим счётчик
            await db.execute("INSERT INTO promo_redemptions (code, telegram_id, used_at) VALUES (?, ?, ?)", (code, telegram_id, now))
            await db.execute("UPDATE promo_codes SET used_count = used_count + 1 WHERE code = ?", (code,))
            # Если достигнут лимит — деактивируем
            await db.execute("UPDATE promo_codes SET is_active = CASE WHEN max_uses > 0 AND used_count >= max_uses THEN 0 ELSE is_active END WHERE code = ?", (code,))
            await db.commit()
            return {'ok': True, 'days': int(days or 30)}
        except Exception as e:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            logger.error(f"redeem_promo_code error: {e}")
            return {'ok': False, 'error': 'exception'}

async def get_activated_promo_codes_count() -> int:
    async with get_db_connection_safe() as db:
        async with db.execute("SELECT COUNT(*) FROM promo_codes WHERE is_active = 0") as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_activated_code_for_user(user_id: int) -> Optional[str]:
    async with get_db_connection_safe() as db:
        async with db.execute(
            "SELECT code FROM promo_codes WHERE activated_by_telegram_id = ?", (user_id,)
        ) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else None

async def get_promo_codes_list(status: str, limit: int, offset: int) -> List[tuple]:
    query = "SELECT code, is_active, activated_by_telegram_id, activated_at FROM promo_codes"
    params = []
    if status == 'active': query += " WHERE is_active = 1"
    elif status == 'inactive': query += " WHERE is_active = 0"
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    async with get_db_connection_safe() as db:
        async with db.execute(query, tuple(params)) as cursor:
            return await cursor.fetchall()

async def get_promo_codes_count(status: str) -> int:
    query = "SELECT COUNT(*) FROM promo_codes"
    if status == 'active': query += " WHERE is_active = 1"
    elif status == 'inactive': query += " WHERE is_active = 0"
    async with get_db_connection_safe() as db:
        async with db.execute(query) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_users_with_expiring_subscriptions(days_before: int = 1):
    """
    Получает пользователей с подпиской, которая заканчивается через указанное количество дней.
    Учитывает часовые пояса и не отправляет повторные уведомления.
    """
    from datetime import datetime, timedelta, timezone
    import aiosqlite
    
    # Вычисляем целевую дату в UTC
    now_utc = datetime.now(timezone.utc)
    target_date_start = (now_utc + timedelta(days=days_before)).replace(hour=0, minute=0, second=0, microsecond=0)
    target_date_end = target_date_start + timedelta(days=1)
    
    async with get_db_connection_safe() as db:
        async with db.execute(
            "SELECT telegram_id, subscription_end_date FROM users WHERE subscription_end_date IS NOT NULL AND (notified_expiring IS NULL OR notified_expiring = 0) AND is_active = 1"
        ) as cursor:
            result = []
            async for row in cursor:
                user_id, sub_end_str = row
                if not sub_end_str:
                    continue
                
                try:
                    # Парсим дату окончания подписки
                    sub_end_date = datetime.fromisoformat(sub_end_str)
                    
                    # Если дата "наивная" (без таймзоны), делаем её UTC
                    if sub_end_date.tzinfo is None:
                        sub_end_date = sub_end_date.replace(tzinfo=timezone.utc)
                    
                    # Проверяем, попадает ли дата в целевой диапазон
                    if target_date_start <= sub_end_date < target_date_end:
                        result.append(user_id)
                        
                except Exception as e:
                    logger.error(f"Не удалось обработать дату окончания подписки для пользователя {user_id}: {sub_end_str}. Ошибка: {e}")
                    continue
            
            return result

async def get_users_with_expired_subscriptions():
    """
    Получает пользователей с истекшей подпиской, которым еще не отправляли уведомление.
    Сравнение дат происходит в Python для надежной обработки часовых поясов.
    """
    from datetime import datetime, timezone
    import aiosqlite
    
    now_utc = datetime.now(timezone.utc)

    async with get_db_connection_safe() as db:
        # Выбираем всех потенциальных кандидатов, проверка даты будет в коде
        async with db.execute(
            """SELECT telegram_id, subscription_end_date FROM users 
               WHERE subscription_end_date IS NOT NULL 
               AND (notified_expired IS NULL OR notified_expired = 0)
               AND is_active = 1"""
        ) as cursor:
            expired_users = []
            async for row in cursor:
                user_id, sub_end_str = row
                if not sub_end_str:
                    continue
                
                try:
                    # fromisoformat корректно парсит даты с часовым поясом
                    sub_end_date = datetime.fromisoformat(sub_end_str)
                    
                    # Если дата "наивная" (без таймзоны), делаем ее "осведомленной",
                    # предполагая, что она в локальной таймзоне сервера.
                    if sub_end_date.tzinfo is None:
                        sub_end_date = sub_end_date.astimezone()

                    # Сравнение timezone-aware datetime объектов
                    if sub_end_date < now_utc:
                        expired_users.append(user_id)
                except Exception as e:
                    logger.error(f"Не удалось обработать дату окончания подписки для пользователя {user_id}: {sub_end_str}. Ошибка: {e}")
            
            return expired_users

async def reset_notification_flags(telegram_id: int = None):
    """
    Сбрасывает флаги уведомлений для пользователя или всех пользователей.
    telegram_id: если указан, сбрасывает только для этого пользователя, иначе для всех
    """
    async with get_db_connection_safe() as db:
        if telegram_id:
            await db.execute("UPDATE users SET notified_expiring = 0, notified_expired = 0 WHERE telegram_id = ?", (telegram_id,))
            logger.info(f"Сброшены флаги уведомлений для пользователя {telegram_id}")
        else:
            await db.execute("UPDATE users SET notified_expiring = 0, notified_expired = 0")
            logger.info("Сброшены флаги уведомлений для всех пользователей")
        await db.commit()

async def async_update_setting(key: str, value: str) -> bool:
    """
    Асинхронно обновляет настройку в таблице settings.
    Если настройка не существует, создает новую запись.
    
    Args:
        key: Ключ настройки
        value: Значение настройки
        
    Returns:
        bool: True если успешно, False при ошибке
    """
    try:
        async with get_db_connection_safe() as db:
            cur = await db.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
            if cur.rowcount == 0:
                await db.execute("INSERT OR IGNORE INTO settings(key, value, description) VALUES(?, ?, ?)", (key, value, ''))
            await db.commit()
            return True
    except Exception as e:
        logger.error(f"Ошибка обновления настройки {key}: {e}")
        return False

async def async_update_xui_servers_distribution_settings(new_servers_list):
    """
    Асинхронно обновляет настройки распределения серверов в таблице settings.key='xui_servers'.
    """
    import json
    import aiosqlite
    try:
        async with aiosqlite.connect(DATABASE_NAME, timeout=30) as db:
            try:
                await db.execute("PRAGMA journal_mode=WAL;")
                await db.execute("PRAGMA synchronous=NORMAL;")
                await db.execute("PRAGMA busy_timeout=30000;")
                await db.execute("PRAGMA foreign_keys=ON;")
            except Exception:
                pass
            await db.execute(
                "UPDATE settings SET value = ? WHERE key = 'xui_servers'",
                (json.dumps(new_servers_list, indent=4),)
            )
            await db.commit()
            return True
    except Exception as e:
        logger.error(f"Ошибка обновления настроек распределения серверов: {e}")
        return False

def update_xui_servers_distribution_settings(new_servers_list):
    """
    Синхронная обёртка для совместимости с существующим кодом.
    Если event loop уже запущен — не блокируем его, возвращаем False.
    Рекомендуется вызывать async_update_xui_servers_distribution_settings в async-коде.
    """
    try:
        asyncio.get_running_loop()
        # Уже в event loop — синхронный блокирующий вызов недопустим
        logger.error("update_xui_servers_distribution_settings вызван внутри event loop. Используйте async-версию.")
        return False
    except RuntimeError:
        # Нет активного цикла — можно выполнить
        return asyncio.run(async_update_xui_servers_distribution_settings(new_servers_list))

async def get_active_clients_count_for_server(server_id: int) -> Optional[int]:
    """
    Возвращает количество активных клиентов (с действующей подпиской) для конкретного сервера.
    """
    try:
        async with get_db_connection_safe() as db:
            cursor = await db.execute(
                """SELECT COUNT(*) FROM users 
                   WHERE current_server_id = ? 
                   AND subscription_end_date IS NOT NULL 
                   AND subscription_end_date > ?""",
                (server_id, datetime.now().isoformat())
            )
            result = await cursor.fetchone()
            return result[0] if result else 0
    except Exception as e:
        logger.error(f"Ошибка при подсчёте активных клиентов для сервера {server_id}: {e}")
        return None

async def get_active_tariffs() -> List[Dict]:
    """Получает все активные тарифы, отсортированные по sort_order."""
    async with get_db_connection_safe() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tariffs WHERE is_active = 1 ORDER BY sort_order, id"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def get_tariff_by_id(tariff_id: int) -> Optional[Dict]:
    """Получает тариф по ID."""
    async with get_db_connection_safe() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tariffs WHERE id = ?", (tariff_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def get_active_traffic_topup_tariffs() -> List[Dict]:
    """Получает все активные тарифы докупки трафика, отсортированные по цене (от меньшей к большей)."""
    async with get_db_connection_safe() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM traffic_topup_tariffs WHERE is_active = 1 ORDER BY price ASC, traffic_gb ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def get_traffic_topup_tariff_by_id(tariff_id: int) -> Optional[Dict]:
    """Получает тариф докупки трафика по ID."""
    async with get_db_connection_safe() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM traffic_topup_tariffs WHERE id = ?", (tariff_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def create_traffic_topup_tariff(name: str, traffic_gb: int, price: float, 
                       description: str = '', sort_order: int = 0) -> bool:
    """Создает новый тариф докупки трафика."""
    try:
        async with get_db_connection_safe() as db:
            await db.execute('''
                INSERT INTO traffic_topup_tariffs (name, traffic_gb, price, description, sort_order, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
            ''', (name, traffic_gb, price, description, sort_order))
            await db.commit()
            return True
    except Exception as e:
        logger.error(f"Ошибка создания тарифа докупки трафика: {e}")
        return False

async def update_traffic_topup_tariff(tariff_id: int, name: str, traffic_gb: int, price: float, 
                       description: str = '', 
                       sort_order: int = 0, is_active: bool = True) -> bool:
    """Обновляет существующий тариф докупки трафика."""
    try:
        async with get_db_connection_safe() as db:
            await db.execute('''
                UPDATE traffic_topup_tariffs 
                SET name = ?, traffic_gb = ?, price = ?, description = ?, 
                    sort_order = ?, is_active = ?
                WHERE id = ?
            ''', (name, traffic_gb, price, description, sort_order, 
                  int(is_active), tariff_id))
            await db.commit()
            return True
    except Exception as e:
        logger.error(f"Ошибка обновления тарифа докупки трафика: {e}")
        return False

async def create_tariff(name: str, days: int, price: float, currency: str = 'RUB', 
                       description: str = '', sort_order: int = 0) -> bool:
    """Создает новый тариф."""
    try:
        async with get_db_connection_safe() as db:
            await db.execute('''
                INSERT INTO tariffs (name, days, price, currency, description, sort_order, is_active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            ''', (name, days, price, currency, description, sort_order))
            await db.commit()
            return True
    except Exception as e:
        logger.error(f"Ошибка создания тарифа: {e}")
        return False

async def update_tariff(tariff_id: int, name: str, days: int, price: float, 
                       currency: str = 'RUB', description: str = '', 
                       sort_order: int = 0, is_active: bool = True) -> bool:
    """Обновляет существующий тариф."""
    try:
        async with get_db_connection_safe() as db:
            await db.execute('''
                UPDATE tariffs 
                SET name = ?, days = ?, price = ?, currency = ?, description = ?, 
                    sort_order = ?, is_active = ?
                WHERE id = ?
            ''', (name, days, price, currency, description, sort_order, 
                  int(is_active), tariff_id))
            await db.commit()
            return True
    except Exception as e:
        logger.error(f"Ошибка обновления тарифа: {e}")
        return False

async def delete_tariff(tariff_id: int) -> bool:
    """Удаляет тариф."""
    try:
        async with get_db_connection_safe() as db:
            await db.execute("DELETE FROM tariffs WHERE id = ?", (tariff_id,))
            await db.commit()
            return True
    except Exception as e:
        logger.error(f"Ошибка удаления тарифа: {e}")
        return False

async def toggle_tariff_active(tariff_id: int) -> bool:
    """Переключает активность тарифа."""
    try:
        async with get_db_connection_safe() as db:
            await db.execute(
                "UPDATE tariffs SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?",
                (tariff_id,)
            )
            await db.commit()
            return True
    except Exception as e:
        logger.error(f"Ошибка переключения активности тарифа: {e}")
        return False

# --- ФУНКЦИИ ДЛЯ РАБОТЫ С invited_by ---

async def set_invited_by(telegram_id: int, invited_by: int):
    async with get_db_connection_safe() as db:
        await db.execute("UPDATE users SET invited_by = ? WHERE telegram_id = ?", (invited_by, telegram_id))
        await db.commit()

async def set_invited_by_with_method(telegram_id: int, invited_by: int, method: str):
    async with get_db_connection_safe() as db:
        await db.execute("UPDATE users SET invited_by = ?, invited_by_method = ? WHERE telegram_id = ?", (invited_by, method, telegram_id))
        await db.commit()

async def get_invited_by(telegram_id: int) -> int:
    async with get_db_connection_safe() as db:
        async with db.execute("SELECT invited_by FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] is not None else None

async def get_invited_by_method(telegram_id: int) -> str:
    async with get_db_connection_safe() as db:
        async with db.execute("SELECT invited_by_method FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] is not None else None

async def get_referrals_count_today(ref_id: int) -> int:
    """Возвращает количество рефералов, приглашенных сегодня"""
    try:
        async with get_db_connection_safe() as db:
            # Сначала проверяем, есть ли колонка created_at
            cursor = await db.execute("PRAGMA table_info(users)")
            columns = await cursor.fetchall()
            column_names = [col[1] for col in columns]
            
            if 'created_at' not in column_names:
                logger.warning("Колонка created_at не найдена в таблице users. Возвращаем 0.")
                return 0
            
            # Получаем все рефералы и проверяем дату в Python
            async with db.execute("""
                SELECT created_at FROM users 
                WHERE invited_by = ?
            """, (ref_id,)) as cursor:
                rows = await cursor.fetchall()
                
            if not rows:
                return 0
            
            # Подсчитываем рефералов за сегодня
            today = datetime.now().date()
            count = 0
            
            for row in rows:
                if row[0]:  # created_at не None
                    try:
                        # Парсим дату из ISO формата
                        created_date = datetime.fromisoformat(row[0]).date()
                        if created_date == today:
                            count += 1
                    except (ValueError, TypeError):
                        # Если не удалось распарсить дату, пропускаем
                        continue
            
            return count
    except Exception as e:
        logger.error(f"Ошибка при подсчете рефералов за сегодня для {ref_id}: {e}")
        return 0

async def can_invite_more_today(ref_id: int, max_per_day: int = 3) -> bool:
    """Проверяет, может ли пользователь пригласить еще рефералов сегодня"""
    current_count = await get_referrals_count_today(ref_id)
    return current_count < max_per_day

async def get_referrals_stats(ref_id: int, days: int = 7) -> list:
    """Возвращает статистику рефералов за последние N дней"""
    try:
        async with get_db_connection_safe() as db:
            # Сначала проверяем, есть ли колонка created_at
            cursor = await db.execute("PRAGMA table_info(users)")
            columns = await cursor.fetchall()
            column_names = [col[1] for col in columns]
            
            if 'created_at' not in column_names:
                logger.warning("Колонка created_at не найдена в таблице users. Возвращаем пустой список.")
                return []
            
            # Получаем все рефералы за последние N дней
            async with db.execute("""
                SELECT created_at FROM users 
                WHERE invited_by = ?
            """, (ref_id,)) as cursor:
                rows = await cursor.fetchall()
            
            if not rows:
                return []
            
            # Группируем по датам в Python
            from collections import defaultdict
            stats = defaultdict(int)
            
            # Вычисляем дату N дней назад
            from datetime import timedelta
            cutoff_date = datetime.now().date() - timedelta(days=days)
            
            for row in rows:
                if row[0]:  # created_at не None
                    try:
                        created_date = datetime.fromisoformat(row[0]).date()
                        if created_date >= cutoff_date:
                            stats[created_date.isoformat()] += 1
                    except (ValueError, TypeError):
                        continue
            
            # Преобразуем в список словарей
            result = [{'date': date, 'count': count} for date, count in stats.items()]
            result.sort(key=lambda x: x['date'], reverse=True)
            
            return result
    except Exception as e:
        logger.error(f"Ошибка при получении статистики рефералов для {ref_id}: {e}")
        return []

# --- Партнёрская программа: получение процента партнёра ---
async def get_partner_percent(partner_id: int) -> int:
    """
    Получает процент партнёра для начисления бонусов.
    Сначала проверяет индивидуальный процент пользователя, если NULL - возвращает глобальную настройку.
    
    Args:
        partner_id: Telegram ID партнёра
        
    Returns:
        Процент партнёра (0-100)
    """
    try:
        async with get_db_connection_safe() as db:
            # Проверяем индивидуальный процент партнёра
            cursor = await db.execute(
                "SELECT partner_percent_rub FROM users WHERE telegram_id = ?",
                (partner_id,)
            )
            row = await cursor.fetchone()
            if row and row[0] is not None:
                # Есть индивидуальный процент
                individual_percent = float(row[0])
                if 0 <= individual_percent <= 100:
                    return int(individual_percent)
                # Если процент вне диапазона, используем глобальный
                logger.warning(f"Invalid partner_percent_rub for user {partner_id}: {individual_percent}")
            
            # Используем глобальную настройку
            settings = await load_all_settings()
            return int(settings.get('partner_percent_rub') or '10')
    except Exception as e:
        logger.error(f"get_partner_percent error for {partner_id}: {e}")
        # Fallback на глобальную настройку
        try:
            settings = await load_all_settings()
            return int(settings.get('partner_percent_rub') or '10')
        except Exception:
            return 10  # Последний fallback

# --- Партнёрская программа: история начислений ---
async def log_partner_accrual(partner_id: int, payer_id: int, payment_id: str, amount: float, currency: str, percent: int, bonus: float):
    try:
        async with get_db_connection_safe() as db:
            await db.execute(
                """
                INSERT INTO partner_accruals (partner_id, payer_id, payment_id, amount, currency, percent, bonus, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (partner_id, payer_id, str(payment_id) if payment_id else None, float(amount or 0), str(currency or ''), int(percent or 0), float(bonus or 0), utc_now_iso())
            )
            await db.commit()
    except Exception as e:
        logger.error(f"log_partner_accrual error: {e}")

async def get_partner_accruals(partner_id: int, limit: int = 10):
    try:
        async with get_db_connection_safe() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, created_at, payer_id, amount, currency, percent, bonus, payment_id
                FROM partner_accruals
                WHERE partner_id = ?
                ORDER BY datetime(created_at) DESC
                LIMIT ?
                """,
                (partner_id, limit)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.error(f"get_partner_accruals error for {partner_id}: {e}")
        return []

# --- Для бонуса за первый платёж: отдельная таблица, чтобы не начислять дважды ---
async def init_referral_bonus_table():
    async with get_db_connection_safe() as db:
        # Проверяем, существует ли таблица и есть ли в ней поле bonus_type
        try:
            async with db.execute("PRAGMA table_info(referral_payment_bonus)") as cursor:
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]
                has_bonus_type = 'bonus_type' in column_names
        except Exception:
            has_bonus_type = False
        
        if not has_bonus_type:
            # Миграция: пересоздаем таблицу с новым полем bonus_type
            try:
                # Создаем временную таблицу с новой структурой
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS referral_payment_bonus_new (
                        ref_user_id INTEGER,
                        invited_user_id INTEGER,
                        bonus_type TEXT NOT NULL DEFAULT 'payment',
                        PRIMARY KEY (ref_user_id, invited_user_id, bonus_type)
                    )
                ''')
                # Копируем данные из старой таблицы, устанавливая bonus_type='payment' для существующих записей
                await db.execute("INSERT INTO referral_payment_bonus_new (ref_user_id, invited_user_id, bonus_type) SELECT ref_user_id, invited_user_id, 'payment' FROM referral_payment_bonus")
                # Удаляем старую таблицу
                await db.execute("DROP TABLE referral_payment_bonus")
                # Переименовываем новую таблицу
                await db.execute("ALTER TABLE referral_payment_bonus_new RENAME TO referral_payment_bonus")
                await db.commit()
            except Exception as e:
                # Если миграция не удалась, создаем таблицу с нуля
                await db.execute("DROP TABLE IF EXISTS referral_payment_bonus")
                await db.execute('''
                    CREATE TABLE referral_payment_bonus (
                        ref_user_id INTEGER,
                        invited_user_id INTEGER,
                        bonus_type TEXT NOT NULL DEFAULT 'payment',
                        PRIMARY KEY (ref_user_id, invited_user_id, bonus_type)
                    )
                ''')
                await db.commit()
        else:
            # Таблица уже обновлена, просто убеждаемся, что она существует
            await db.execute('''
                CREATE TABLE IF NOT EXISTS referral_payment_bonus (
                    ref_user_id INTEGER,
                    invited_user_id INTEGER,
                    bonus_type TEXT NOT NULL DEFAULT 'payment',
                    PRIMARY KEY (ref_user_id, invited_user_id, bonus_type)
                )
            ''')
            await db.commit()

async def delete_referral_bonuses_for_user(telegram_id: int) -> None:
    """Удаляет записи referral_payment_bonus, где пользователь — пригласивший или приглашённый."""
    async with get_db_connection_safe() as db:
        await db.execute(
            "DELETE FROM referral_payment_bonus WHERE ref_user_id = ? OR invited_user_id = ?",
            (telegram_id, telegram_id),
        )
        await db.commit()


async def mark_referral_payment_bonus_given(ref_user_id: int, invited_user_id: int, bonus_type: str = 'payment'):
    """
    Отмечает, что реферальный бонус был выдан.
    
    Args:
        ref_user_id: ID пользователя, который получил бонус (пригласивший)
        invited_user_id: ID пользователя, за которого выдан бонус (приглашенный)
        bonus_type: Тип бонуса - 'join' (за первое подключение) или 'payment' (за платеж)
    """
    async with get_db_connection_safe() as db:
        await db.execute("INSERT OR IGNORE INTO referral_payment_bonus (ref_user_id, invited_user_id, bonus_type) VALUES (?, ?, ?)", (ref_user_id, invited_user_id, bonus_type))
        await db.commit()

async def is_referral_payment_bonus_given(ref_user_id: int, invited_user_id: int, bonus_type: str = 'payment') -> bool:
    """
    Проверяет, был ли уже выдан реферальный бонус определенного типа.
    
    Args:
        ref_user_id: ID пользователя, который получил бонус (пригласивший)
        invited_user_id: ID пользователя, за которого выдан бонус (приглашенный)
        bonus_type: Тип бонуса - 'join' (за первое подключение) или 'payment' (за платеж)
    
    Returns:
        True, если бонус уже был выдан, False иначе
    """
    async with get_db_connection_safe() as db:
        async with db.execute("SELECT 1 FROM referral_payment_bonus WHERE ref_user_id = ? AND invited_user_id = ? AND bonus_type = ?", (ref_user_id, invited_user_id, bonus_type)) as cursor:
            row = await cursor.fetchone()
            return bool(row)

# --- Маркировка уведомлений о подписке ---
async def mark_notified_expiring(telegram_id: int) -> None:
    """Помечает, что пользователю отправлено уведомление о скором окончании (SQLite)."""
    try:
        async with get_db_connection_safe() as db:
            cursor = await db.execute("UPDATE users SET notified_expiring = 1 WHERE telegram_id = ?", (telegram_id,))
            rows_affected = cursor.rowcount
            await db.commit()
            
            # Проверяем, что изменения действительно применены
            if rows_affected > 0:
                logger.info(f"✓ Установлен флаг notified_expiring=1 для пользователя {telegram_id} (затронуто строк: {rows_affected})")
            else:
                logger.warning(f"⚠ Не удалось установить флаг notified_expiring для пользователя {telegram_id} (пользователь не найден или уже помечен?)")
                # Проверяем текущее значение для диагностики
                async with db.execute("SELECT notified_expiring FROM users WHERE telegram_id = ?", (telegram_id,)) as check_cursor:
                    row = await check_cursor.fetchone()
                    if row:
                        logger.warning(f"Текущее значение notified_expiring для пользователя {telegram_id}: {row[0]}")
                    else:
                        logger.error(f"Пользователь {telegram_id} не найден в базе данных!")
    except Exception as e:
        logger.error(f"❌ Ошибка при установке флага notified_expiring для пользователя {telegram_id}: {e}", exc_info=True)
        raise

async def mark_free_renewal_used(telegram_id: int) -> None:
    """Отмечает, что пользователь использовал бесплатное продление"""
    async with get_db_connection_safe() as db:
        await db.execute("UPDATE users SET free_renewal_used = 1 WHERE telegram_id = ?", (telegram_id,))
        await db.commit()
    logger.info(f"Пользователь {telegram_id} отметил использование бесплатного продления")

async def is_free_renewal_used(telegram_id: int) -> bool:
    """Проверяет, использовал ли пользователь бесплатное продление"""
    try:
        async with get_db_connection_safe() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT free_renewal_used FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    # Проверяем, есть ли поле free_renewal_used в результате
                    try:
                        # Row объект поддерживает доступ по индексу и по имени
                        free_renewal_used = row['free_renewal_used'] if 'free_renewal_used' in row.keys() else row[0] if len(row) > 0 else 0
                        return bool(free_renewal_used)
                    except (IndexError, KeyError, AttributeError):
                        # Если поле не существует (старая БД), возвращаем False
                        return False
    except Exception as e:
        logger.warning(f"Ошибка при проверке free_renewal_used для {telegram_id}: {e}")
        return False
    return False

async def mark_notified_expired(telegram_id: int) -> None:
    """Помечает, что пользователю отправлено уведомление об истечении (SQLite)."""
    async with get_db_connection_safe() as db:
        await db.execute("UPDATE users SET notified_expired = 1 WHERE telegram_id = ?", (telegram_id,))
        await db.commit()

# --- Получение списка рефералов пользователя ---
async def get_referrals(ref_id: int) -> List[Dict]:
    try:
        async with get_db_connection_safe() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT telegram_id, username, created_at FROM users WHERE invited_by = ? ORDER BY created_at DESC, telegram_id DESC",
                (ref_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.error(f"get_referrals error for {ref_id}: {e}")
        return []

async def log_client_recreation_error(telegram_id: int, client_uuid: str, server_id: int, server_name: str, error_type: str, error_message: str):
    """
    Логирует ошибку восстановления клиента в БД.
    
    Args:
        telegram_id: ID пользователя в Telegram
        client_uuid: UUID клиента
        server_id: ID сервера
        server_name: Название сервера
        error_type: Тип ошибки (например, 'api_error', 'recreation_failed')
        error_message: Сообщение об ошибке
    """
    try:
        async with get_db_connection_safe() as db:
            await db.execute('''
                INSERT INTO client_recreation_errors 
                (telegram_id, client_uuid, server_id, server_name, error_type, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (telegram_id, client_uuid, server_id, server_name, error_type, error_message, datetime.now().isoformat()))
            await db.commit()
    except Exception as e:
        logger.error(f"Ошибка при логировании ошибки восстановления клиента: {e}")

async def get_tariff_by_id(tariff_id: int) -> Optional[Dict]:
    """Получает тариф по ID."""
    async with aiosqlite.connect(DATABASE_NAME, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tariffs WHERE id = ?", (tariff_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


# ─── Веб-регистрация ──────────────────────────────────────────────────────────

import random as _random

def generate_web_user_id() -> int:
    """Генерирует уникальный fake telegram_id для веб-пользователя (10-99 млрд)."""
    return _random.randint(10_000_000_000, 99_999_999_999)

async def create_web_user(email: str) -> int:
    """Создаёт веб-пользователя в users и возвращает его telegram_id."""
    user_id = generate_web_user_id()
    async with aiosqlite.connect(DATABASE_NAME, timeout=30) as db:
        # Гарантируем уникальность user_id
        while True:
            async with db.execute("SELECT 1 FROM users WHERE telegram_id = ?", (user_id,)) as cur:
                if not await cur.fetchone():
                    break
            user_id = generate_web_user_id()
        now = utc_now_iso()
        await db.execute(
            "INSERT INTO users (telegram_id, username, email, registration_type, created_at) VALUES (?, ?, ?, 'site', ?)",
            (user_id, email.split('@')[0], email, now)
        )
        await db.commit()
    return user_id

async def ensure_tg_user_exists(telegram_id: int, username: str = '', first_name: str = '') -> None:
    """Создаёт пользователя по Telegram ID если его ещё нет в базе (для Telegram Login Widget)."""
    async with aiosqlite.connect(DATABASE_NAME, timeout=30) as db:
        async with db.execute("SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,)) as cur:
            if await cur.fetchone():
                return
        now = utc_now_iso()
        display = username or first_name or str(telegram_id)
        await db.execute(
            "INSERT INTO users (telegram_id, username, registration_type, created_at) VALUES (?, ?, 'telegram', ?)",
            (telegram_id, display, now)
        )
        await db.commit()

async def update_user_email(telegram_id: int, email: str) -> None:
    """Привязывает email к боту-пользователю."""
    async with get_db_connection_safe() as db:
        await db.execute(
            "UPDATE users SET email = ? WHERE telegram_id = ?",
            (email, telegram_id)
        )
        await db.commit()


async def get_setting_by_key(key: str, default: str = '') -> str:
    async with get_db_connection_safe() as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            if not row or row[0] is None:
                return default
            return str(row[0])


async def email_registered_in_db(email: str) -> bool:
    async with get_db_connection_safe() as db:
        async with db.execute(
            "SELECT 1 FROM users WHERE LOWER(email) = LOWER(?) LIMIT 1",
            (email,),
        ) as cur:
            return bool(await cur.fetchone())


async def get_web_user_by_email(email: str) -> Optional[Dict]:
    """Находит веб-пользователя по email."""
    async with aiosqlite.connect(DATABASE_NAME, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def save_web_auth_token(email: str, token: str, expires_at: str, ref_cookie: str = '') -> None:
    """Сохраняет magic-link/auth токен. ref_cookie передаётся при верификации."""
    async with aiosqlite.connect(DATABASE_NAME, timeout=30) as db:
        now = utc_now_iso()
        await db.execute(
            "INSERT INTO web_auth_tokens (email, token, expires_at, ref_cookie, created_at) VALUES (?, ?, ?, ?, ?)",
            (email, token, expires_at, ref_cookie or '', now)
        )
        await db.commit()

async def consume_web_auth_token(token: str) -> Optional[dict]:
    """
    Проверяет и помечает токен использованным.
    Возвращает {'email': ..., 'ref_cookie': ...} или None.
    """
    async with aiosqlite.connect(DATABASE_NAME, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        now = utc_now_iso()
        async with db.execute(
            "SELECT email, expires_at, used, COALESCE(ref_cookie,'') as ref_cookie FROM web_auth_tokens WHERE token = ?",
            (token,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        if row['used'] or row['expires_at'] < now:
            return None
        await db.execute("UPDATE web_auth_tokens SET used = 1 WHERE token = ?", (token,))
        await db.commit()
        return {'email': row['email'], 'ref_cookie': row['ref_cookie']}


async def peek_web_auth_token(token: str) -> Optional[dict]:
    """
    Проверяет токен БЕЗ потребления (не ставит used=1).

    Нужно для magic-link авторизации: пользователь открывает ссылку во встроенном
    браузере Telegram, авторизуется через cookie. Затем нажимает «Открыть в браузере»
    — Safari/Chrome получают тот же URL `/cabinet?m=<token>`, но без cookie.
    Раз токен не «потреблён» — внешний браузер тоже может по нему авторизоваться
    (в течение 10 минут жизни токена).

    Истёкшие/использованные через consume токены не валидны.
    Возвращает {'email': ..., 'ref_cookie': ...} или None.
    """
    async with aiosqlite.connect(DATABASE_NAME, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        now = utc_now_iso()
        async with db.execute(
            "SELECT email, expires_at, used, COALESCE(ref_cookie,'') as ref_cookie "
            "FROM web_auth_tokens WHERE token = ?",
            (token,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        if row['used'] or row['expires_at'] < now:
            return None
        return {'email': row['email'], 'ref_cookie': row['ref_cookie']}

async def check_send_attempts(email: str, max_sends: int = 3) -> bool:
    """
    Считает сколько раз за последние 15 минут запрашивали код для email.
    Возвращает True если лимит НЕ превышен (можно отправлять).
    """
    async with aiosqlite.connect(DATABASE_NAME, timeout=30) as db:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        async with db.execute(
            "SELECT COUNT(*) FROM web_auth_tokens WHERE email = ? AND created_at > ?",
            (email, cutoff)
        ) as cur:
            row = await cur.fetchone()
        total = int(row[0]) if row else 0
        return total < max_sends

async def check_code_attempts(email: str, max_attempts: int = 5) -> bool:
    """
    Считает неверные попытки ввода кода для email за последние 15 минут.
    Возвращает True если лимит НЕ превышен (можно продолжать).
    """
    async with aiosqlite.connect(DATABASE_NAME, timeout=30) as db:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        async with db.execute(
            "SELECT COALESCE(SUM(attempts), 0) FROM web_auth_tokens WHERE email = ? AND created_at > ? AND used = 0",
            (email, cutoff)
        ) as cur:
            row = await cur.fetchone()
        total = int(row[0]) if row else 0
        return total < max_attempts

async def increment_code_attempt(email: str) -> None:
    """Увеличивает счётчик неверных попыток для последнего активного токена."""
    async with aiosqlite.connect(DATABASE_NAME, timeout=30) as db:
        await db.execute(
            """UPDATE web_auth_tokens SET attempts = COALESCE(attempts, 0) + 1
               WHERE email = ? AND used = 0
               AND id = (SELECT id FROM web_auth_tokens WHERE email = ? AND used = 0
                         ORDER BY created_at DESC LIMIT 1)""",
            (email, email)
        )
        await db.commit()

async def cleanup_web_auth_tokens() -> None:
    """Удаляет истёкшие и использованные токены."""
    async with aiosqlite.connect(DATABASE_NAME, timeout=30) as db:
        now = utc_now_iso()
        await db.execute(
            "DELETE FROM web_auth_tokens WHERE used = 1 OR expires_at < ?", (now,)
        )
        await db.commit()


async def sync_remnawave_traffic_to_users() -> int:
    """Синхронизирует total_bytes, total_total_bytes и online_at из remnawave.db в users.

    Батчами с commit между батчами — короткие write-lock, основная БД не блокируется надолго.
    """
    db_path = migrate_remnawave_db_if_needed()
    if not os.path.isfile(db_path):
        return 0

    async with get_db_connection_safe() as bot_db:
        async with bot_db.execute("PRAGMA table_info(users)") as cur:
            user_cols = {row[1] for row in await cur.fetchall()}
        if not {'total_bytes', 'total_total_bytes', 'online_at'}.issubset(user_cols):
            return 0

    rw_conn = await aiosqlite.connect(db_path, timeout=10)
    try:
        await rw_conn.execute("PRAGMA busy_timeout=15000;")
        async with rw_conn.execute("PRAGMA table_info(user_traffic)") as cur:
            rw_cols = {row[1] for row in await cur.fetchall()}
        has_online = 'online_at' in rw_cols
        if has_online:
            query = (
                "SELECT telegram_id, "
                "SUM(total_bytes), SUM(total_total_bytes), MAX(online_at) "
                "FROM user_traffic WHERE telegram_id IS NOT NULL "
                "GROUP BY telegram_id"
            )
        else:
            query = (
                "SELECT telegram_id, "
                "SUM(total_bytes), SUM(total_total_bytes) "
                "FROM user_traffic WHERE telegram_id IS NOT NULL "
                "GROUP BY telegram_id"
            )
        cursor = await rw_conn.execute(query)

        total_updated = 0
        while True:
            rows = await cursor.fetchmany(_RW_SYNC_BATCH_SIZE)
            if not rows:
                break

            batch = []
            for row in rows:
                tg = row[0]
                if tg is None:
                    continue
                online_at = None
                if has_online and len(row) > 3 and row[3] is not None:
                    online_at = row[3]
                    if hasattr(online_at, 'isoformat'):
                        online_at = online_at.isoformat()
                    else:
                        online_at = str(online_at)
                batch.append((
                    int(tg),
                    int(row[1] or 0),
                    int(row[2] or 0),
                    online_at,
                ))
            if not batch:
                continue

            async with get_db_connection_safe() as bot_db:
                await bot_db.execute(
                    """
                    CREATE TEMP TABLE _rw_sync (
                        telegram_id INTEGER PRIMARY KEY,
                        total_bytes INTEGER NOT NULL DEFAULT 0,
                        total_total_bytes INTEGER NOT NULL DEFAULT 0,
                        online_at TEXT
                    )
                    """
                )
                await bot_db.executemany(
                    "INSERT INTO _rw_sync (telegram_id, total_bytes, total_total_bytes, online_at) "
                    "VALUES (?, ?, ?, ?)",
                    batch,
                )
                cur = await bot_db.execute(
                    """
                    UPDATE users SET
                        total_bytes = (
                            SELECT s.total_bytes FROM _rw_sync s
                            WHERE s.telegram_id = users.telegram_id
                        ),
                        total_total_bytes = (
                            SELECT s.total_total_bytes FROM _rw_sync s
                            WHERE s.telegram_id = users.telegram_id
                        ),
                        online_at = (
                            SELECT s.online_at FROM _rw_sync s
                            WHERE s.telegram_id = users.telegram_id
                        )
                    WHERE EXISTS (
                        SELECT 1 FROM _rw_sync s WHERE s.telegram_id = users.telegram_id
                    )
                    """
                )
                if cur.rowcount and cur.rowcount > 0:
                    total_updated += cur.rowcount
                await bot_db.execute("DROP TABLE _rw_sync")
                await bot_db.commit()

            await asyncio.sleep(_RW_SYNC_STAGGER_SEC)

        if total_updated:
            logger.info(f"sync_remnawave_traffic_to_users: обновлено {total_updated} пользователей")
        return total_updated
    finally:
        await rw_conn.close()


def schedule_sync_remnawave_traffic_to_users() -> None:
    """Запускает синхронизацию в фоне; вебхук не ждёт завершения."""
    global _rw_sync_pending
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _rw_sync_lock.locked():
        _rw_sync_pending = True
        logger.debug("sync_remnawave_traffic_to_users: уже выполняется, запланирован повтор")
        return
    loop.create_task(_run_remnawave_traffic_sync())


async def _run_remnawave_traffic_sync() -> None:
    global _rw_sync_pending
    async with _rw_sync_lock:
        while True:
            _rw_sync_pending = False
            try:
                synced = await sync_remnawave_traffic_to_users()
                logger.info(f"Remnawave traffic sync (background): обновлено {synced}")
            except Exception as e:
                logger.error(f"Remnawave traffic sync (background) failed: {e}", exc_info=True)
            if not _rw_sync_pending:
                break