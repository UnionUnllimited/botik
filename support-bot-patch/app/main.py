import asyncio
import os
import re

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery, ChatMemberUpdated, FSInputFile
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from dotenv import load_dotenv
from loguru import logger

from app.database import (
    init_db, add_user, is_banned, set_banned,
    create_ticket, save_topic, get_user_by_topic,
    get_user_open_ticket, get_user_topic, close_ticket_db,
    save_message,
    mark_ticket_operator_joined, is_ticket_operator_joined,
    did_ai_respond_in_ticket,
    # [v3.5] Маппинг group_msg ↔ client_msg для reply оператора
    save_tg_message_map, get_client_msg_id,
    # [v3.5] Обратный маппинг для reply клиента
    save_operator_msg_map, get_operator_group_msg_id,
    # [v3.5] Новая модель тикетов: один клиент = один вечный тикет
    get_or_create_ticket, reopen_ticket, get_last_ticket_for_user,
)
from app.keyboards_proxy import (
    main_menu, pay_menu, bot_menu, vpn_menu, community_menu, shop_menu,
    admin_quick_keyboard,
    mykey_back_keyboard, mykey_reset_confirm_keyboard,
)
# Остальные клавиатуры — статические (статьи, тикет-админ, шаблоны),
# их редактирование через админку не нужно, импортируем из оригинала
from app.keyboards import (
    article_keyboard, ticket_admin_keyboard, admin_action_panel,
    confirm_send_keyboard, ADMIN_ANSWERS,
    admin_panel_keyboard, confirm_extend_keyboard, confirm_reduce_keyboard,
    mykey_main_keyboard,
)
from app.states import SupportStates
from app.texts_proxy import T
from app import admin_panel
from app import content_cache as _content_cache


# ============================================================
#  ENV
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "0"))
ADMIN_PANEL_URL = os.getenv(
    "ADMIN_PANEL_URL",
    "https://ltefreemore.online/FixPakapassFGg_g34/users/"
)
# Список ID операторов через запятую: ADMIN_IDS=123,456
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

# Директория где админка сохраняет фото авто-ответов (QA).
# Та же что в admin_web/qa_manager.py — оба контейнера видят
# через монтированный том /app/data/qa_photos.
QA_PHOTOS_DIR = os.getenv("QA_PHOTOS_DIR", "/app/data/qa_photos")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ============================================================
#  Иконки топиков — общий модуль app.topic_icons.
#  Здесь оставлены тонкие обёртки для обратной совместимости со старым
#  кодом который использует _set_topic_icon / TOPIC_ICON_*_EMOJI.
# ============================================================

from app import topic_icons as _topic_icons

# Псевдонимы для удобства — это строковые роли, не эмодзи.
# Используются как аргументы для _set_topic_icon().
# [v3.5] Только 4 роли: tg (📱), web (💻), closed (✅), banned (🤡).
TOPIC_ICON_TG_EMOJI     = "tg"
TOPIC_ICON_WEB_EMOJI    = "web"
TOPIC_ICON_CLOSED_EMOJI = "closed"
TOPIC_ICON_BANNED_EMOJI = "banned"

# Под старыми именами (для совместимости с импортами в других файлах)
TOPIC_ICON_TG     = "tg"
TOPIC_ICON_WEB    = "web"
TOPIC_ICON_CLOSED = "closed"
TOPIC_ICON_BANNED = "banned"


def _resolve_topic_icon(role: str) -> str | None:
    """
    Принимает роль ('tg' | 'web' | 'closed' | 'banned') и возвращает
    custom_emoji_id из загруженного списка Telegram. None если не найдено.
    """
    if not role:
        return None
    _, icon_id = _topic_icons.get_icon_id(role)
    return icon_id


async def _load_topic_icons() -> None:
    """Один раз при старте бота загружает доступные эмодзи."""
    await _topic_icons.load_topic_icons(bot)


async def _set_topic_icon(topic_id: int, role: str) -> None:
    """
    Меняет иконку форум-топика. role: 'tg' | 'web' | 'closed' | 'banned'.
    """
    if not topic_id or not SUPPORT_CHAT_ID:
        return
    if role not in ("tg", "web", "closed", "banned"):
        logger.debug("_set_topic_icon: неизвестная роль {!r}", role)
        return
    await _topic_icons.set_topic_icon(bot, SUPPORT_CHAT_ID, topic_id, role)


# ============================================================
#  Имя топика для TG-клиента
# ============================================================

def _build_tg_topic_name(user_id: int, user_obj=None) -> str:
    """
    [v3.5] Имя форум-топика для TG-клиента.
    Префикс 📱 (всегда) + @username / "Имя Фамилия" / user_id.
    Telegram имя топика ограничено 128 символами.
    """
    if user_obj is not None:
        username = getattr(user_obj, "username", None)
        if username:
            return f"📱 @{username}"[:128]
        first = (getattr(user_obj, "first_name", None) or "").strip()
        last = (getattr(user_obj, "last_name", None) or "").strip()
        full = " ".join(filter(None, [first, last]))
        if full:
            return f"📱 {full}"[:128]
    return f"📱 {user_id}"


# ============================================================
#  Кэш: чтобы заголовок «Ответ поддержки» слать не каждый раз
# ============================================================

support_reply_cache: set[int] = set()


# ============================================================
#  Универсальный поиск user_id по topic_id.
#  Топик может принадлежать обычному тикету (таблица tickets)
#  ИЛИ веб-чату с сайта (таблица web_visitors). Возвращает кортеж:
#     (user_id, source) где source = 'ticket' | 'webchat' | None
# ============================================================

async def resolve_user_by_topic(topic_id: int) -> tuple[int | None, str | None]:
    """
    Ищет user_id для callback кнопок. Сначала в обычных тикетах,
    потом в веб-чатах. Возвращает (user_id, источник) или (None, None).
    """
    if not topic_id:
        return None, None

    # 1) Обычный тикет
    user_id = await get_user_by_topic(topic_id)
    if user_id:
        return user_id, "ticket"

    # 2) Веб-чат
    try:
        from app import web_chat_db
        web_visitor = await web_chat_db.get_visitor_by_topic(topic_id)
        if web_visitor and web_visitor.get("user_id"):
            return int(web_visitor["user_id"]), "webchat"
    except Exception as e:
        logger.warning("resolve_user_by_topic webchat lookup: {}", e)

    return None, None


async def _is_topic_quiet(topic_id: int | None) -> bool:
    """[v3.5] Определяет надо ли отправлять в этот топик БЕЗ звука.

    Логика:
      - Тикет закрыт (AI работает, нет оператора) → True (тихо)
      - Тикет открыт (оператор активен, эскалация) → False (со звуком)
      - Не нашли тикет → False (по умолчанию со звуком)

    Применяется ко всем сообщениям бота в группу поддержки.
    """
    if not topic_id:
        return False

    # 1) Web-visitor: проверяем visitor.status
    try:
        from app import web_chat_db
        visitor = await web_chat_db.get_visitor_by_topic(topic_id)
        if visitor:
            status = visitor.get("status", "closed")
            return status != "open"
    except Exception:
        pass

    # 2) TG-тикет: проверяем tickets.status по topic_id
    try:
        import aiosqlite
        from app.database import DB_PATH as _TDB
        async with aiosqlite.connect(_TDB) as db:
            cur = await db.execute(
                "SELECT status FROM tickets WHERE topic_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (topic_id,),
            )
            row = await cur.fetchone()
            if row:
                return row[0] != "open"
    except Exception:
        pass

    return False  # по умолчанию со звуком (безопасно)



# ============================================================
#  Ожидающие подтверждения сбросы подписки
#  Ключ: (chat_id, thread_id, operator_id) — изолируем по топику И по
#         оператору, чтобы два оператора в одном топике не путались.
#  Значение: dict(target_user_id, pin, expires_at, prompt_message_id)
# ============================================================

import time as _time

pending_revokes: dict[tuple[int, int, int], dict] = {}
REVOKE_PIN_TTL = 60  # секунд на ввод ПИНа


# ============================================================
#  Ожидание ввода количества дней (extend / reduce)
#  Ключ: тот же — (chat_id, thread_id, operator_id)
#  Значение: {action: 'extend'|'reduce', target_user_id, expires_at, prompt_message_id}
# ============================================================

pending_days_input: dict[tuple[int, int, int], dict] = {}
DAYS_INPUT_TTL = 120  # 2 минуты на ввод числа дней


# ============================================================
#  Хелпер для сохранения сообщений в БД
# ============================================================

def _extract_message_kind_and_text(message: Message) -> tuple[str, str]:
    """
    Из Telegram-сообщения определяет (kind, text_for_log).
    text_for_log — то, что покажется в админке (для медиа — placeholder).
    """
    if message.text:
        return "text", message.text
    if message.caption:
        # У медиа может быть caption — сохраняем его как основной текст
        text = message.caption
    else:
        text = ""

    if message.photo:
        return "photo", text or "[фото]"
    if message.document:
        name = message.document.file_name or "файл"
        return "document", text or f"[документ: {name}]"
    if message.voice:
        return "voice", text or "[голосовое]"
    if message.video:
        return "video", text or "[видео]"
    if message.video_note:
        return "video_note", text or "[видео-кружок]"
    if message.audio:
        return "audio", text or "[аудио]"
    if message.sticker:
        return "sticker", text or f"[стикер {message.sticker.emoji or ''}]"
    if message.animation:
        return "animation", text or "[гифка]"
    if message.contact:
        return "contact", text or f"[контакт: {message.contact.phone_number}]"
    if message.location:
        return "location", text or "[геопозиция]"

    return "other", text or "[сообщение]"


# ============================================================
#  Маппинг callback → (имя текста, куда возвращает «Назад»)
#  ВАЖНО: храним именно ИМЯ ключа (строку), а не сам T.XXX —
#  потому что T.XXX вычисляется при импорте модуля и
#  «замораживает» значение. А имя ключа — это просто строка,
#  и при чтении внизу мы вытаскиваем актуальный текст через
#  getattr(T, key) каждый раз → правки из админки подхватываются
#  без рестарта.
# ============================================================

FAQ_ARTICLES = {
    # Подписка и оплата
    "a_pay_no_page":       ("PAY_NO_PAGE", "pay"),
    "a_pay_tariffs":       ("PAY_TARIFFS", "pay"),
    "a_pay_renew":         ("PAY_RENEW", "pay"),
    "a_bot_paid_not_work": ("BOT_PAID_NOT_WORK", "pay"),
    # Покупка и доставка
    "a_shop_why":          ("SHOP_WHY_US", "shop"),
    "a_shop_how":          ("SHOP_HOW_WORKS", "shop"),
    "a_shop_buy":          ("SHOP_HOW_BUY", "shop"),
    "a_shop_delivery":     ("SHOP_DELIVERY", "shop"),
    # Роутер не работает
    "a_vpn_how":           ("ROUTER_HOW_START", "vpn"),
    "a_vpn_site":          ("ROUTER_SITE_BLOCKED", "vpn"),
    "a_vpn_not_work":      ("ROUTER_TUNNEL_DOWN", "vpn"),
    "a_vpn_no_net":        ("ROUTER_NO_INTERNET", "vpn"),
    # Бот
    "a_bot_no_sub":        ("BOT_NO_SUB", "bot"),
    "a_bot_broken":        ("BOT_BUTTONS_BROKEN", "bot"),
    "a_bot_slow":          ("BOT_SLOW", "bot"),
    "a_bot_pay_err":       ("BOT_PAY_ERROR", "bot"),
}

SECTION_MENUS = {
    "m_pay":       ("PAY_MENU", pay_menu),
    "m_shop":      ("SHOP_MENU", shop_menu),
    "m_bot":       ("BOT_MENU", bot_menu),
    "m_vpn":       ("VPN_MENU", vpn_menu),
    "m_community": ("COMMUNITY_INFO", community_menu),
}


# ============================================================
#  Утилита: безопасное редактирование сообщения
# ============================================================

async def safe_edit(callback: CallbackQuery, text: str, kb):
    """Редактирует текст сообщения. Если нельзя (например, фото) — шлёт новое."""
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)


# ============================================================
#  /start
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return

    await add_user(message.from_user.id, message.from_user.username)

    if await is_banned(message.from_user.id):
        await message.answer(T.USER_BANNED)
        return

    # [v3.5] Создаём «ghost-ticket» (status='closed') при первом контакте.
    # Это позволит оператору видеть историю общения клиента с AI ещё до
    # эскалации. При эскалации тикет переключится в 'open'.
    try:
        await get_or_create_ticket(message.from_user.id)
    except Exception as e:
        logger.warning("cmd_start: get_or_create_ticket failed: {}", e)

    await state.clear()
    await message.answer(T.WELCOME, reply_markup=main_menu())


# ============================================================
#  Главное меню (back_main)
# ============================================================

@dp.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(callback, T.WELCOME, main_menu())
    await callback.answer()


# ============================================================
#  Подменю разделов
# ============================================================

@dp.callback_query(F.data.in_(SECTION_MENUS.keys()))
async def cb_section(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text_key, kb_func = SECTION_MENUS[callback.data]
    text = getattr(T, text_key)  # читаем АКТУАЛЬНЫЙ текст из кеша
    await safe_edit(callback, text, kb_func())
    await callback.answer()


# ============================================================
#  Статьи FAQ
# ============================================================

@dp.callback_query(F.data.in_(FAQ_ARTICLES.keys()))
async def cb_article(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text_key, back_to = FAQ_ARTICLES[callback.data]
    text = getattr(T, text_key)  # читаем АКТУАЛЬНЫЙ текст из кеша
    await safe_edit(callback, text, article_keyboard(back_to))
    await callback.answer()


# ============================================================
#  Связаться с поддержкой
# ============================================================

@dp.callback_query(F.data == "contact_support")
async def cb_contact_support(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id

    if await is_banned(user_id):
        await callback.message.answer(T.USER_BANNED)
        await callback.answer()
        return

    # Если уже есть открытый тикет — просто скажем
    existing = await get_user_open_ticket(user_id)
    if existing:
        await callback.message.answer(
            "🎫 У вас уже есть открытый тикет. Просто напишите "
            "сюда сообщение — оно будет переслано поддержке."
        )
        await callback.answer()
        return

    await state.set_state(SupportStates.awaiting_ticket_message)
    await callback.message.answer(T.CONTACT_PROMPT)
    await callback.answer()


# ============================================================
#  РАЗДЕЛ «🔑 МОЙ КЛЮЧ» (личный кабинет клиента)
# ============================================================

def _safe_get(d: dict, *keys, default=None):
    """Безопасно достаёт вложенное значение d[k1][k2]... — на случай отсутствия ключей."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _extract_subscription_vars(payload: dict | None) -> tuple[dict | None, str | None]:
    """
    [v3.5] Извлекает данные клиента в виде словаря переменных для подстановки
    в шаблон MENU_MYKEY_MENU из админки. Каждая переменная — строка готовая
    к вставке.

    Возвращает (vars, sub_page_link). vars=None если клиента нет в админке.
    Поля которых у клиента нет → возвращаются как пустые строки.
    """
    if not payload or not isinstance(payload, dict):
        return None, None
    data = payload.get("data") if "data" in payload else payload
    if not isinstance(data, dict):
        return None, None
    user = data.get("user") or {}
    if not user:
        return None, None
    remna = data.get("remnawave") or {}

    from datetime import datetime, timezone
    from html import escape

    sub_link_raw = data.get("sub_page_link")
    sub_page_link: str | None = None
    if isinstance(sub_link_raw, str) and sub_link_raw.startswith(("http://", "https://")):
        sub_page_link = sub_link_raw

    # --- status / end_date / days_left ---
    status = ""
    end_date_str = ""
    days_left_str = ""
    end_date = user.get("subscription_end_date")
    if end_date:
        try:
            s = str(end_date).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(tz=timezone.utc)
            if dt < now:
                days_ago = (now - dt).days
                status = f"🔴 <b>истекла</b> {days_ago} дн. назад"
                days_left_str = "0"
            else:
                days_left = (dt - now).days
                status = f"🟢 <b>активна</b> (ещё {days_left} дн.)"
                days_left_str = str(days_left)
            end_date_str = dt.strftime("%d.%m.%Y %H:%M")
        except (ValueError, TypeError):
            status = "❔ не определён"
            end_date_str = str(end_date)
    else:
        status = "❔ не определён"

    # --- registered ---
    registered_str = ""
    created = user.get("created_at")
    if created:
        try:
            s = str(created).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            registered_str = dt.strftime("%d.%m.%Y")
        except (ValueError, TypeError):
            registered_str = str(created)

    # --- limit_ip ---
    limit_ip = user.get("limit_ip")
    limit_ip_str = "∞" if (limit_ip is None or limit_ip == 0) else str(limit_ip)

    # --- username ---
    username_str = user.get("real_username") or ""

    # --- email ---
    email_str = escape(str(user.get("email"))) if user.get("email") else ""

    # --- partner balance ---
    partner_balance_str = ""
    balance = user.get("partner_balance_rub")
    if balance is not None:
        try:
            bal_f = float(balance)
            if bal_f > 0:
                partner_balance_str = f"{bal_f:.2f}"
        except (TypeError, ValueError):
            pass

    # --- traffic ---
    traffic_lifetime_str = ""
    if isinstance(remna, dict):
        traffic = remna.get("user_traffic") or {}
        raw = traffic.get("lifetime_used_traffic_bytes")
        try:
            n = float(raw) if raw is not None else 0
            if n > 0:
                traffic_lifetime_str = f"{n / (1024 ** 3):.2f} GB"
        except (TypeError, ValueError):
            pass

    # --- blocked ---
    blocked_str = ""
    if user.get("is_blocked"):
        blocked_str = "⛔ <b>Подписка заблокирована.</b> Свяжитесь с поддержкой."

    return {
        "status": status,
        "end_date": end_date_str,
        "days_left": days_left_str,
        "registered": registered_str,
        "limit_ip": limit_ip_str,
        "username": username_str,
        "email": email_str,
        "partner_balance": partner_balance_str,
        "traffic_lifetime": traffic_lifetime_str,
        "sub_link": escape(sub_page_link) if sub_page_link else "",
        "blocked": blocked_str,
    }, sub_page_link


def _render_mykey_from_template(template: str, vars_: dict) -> str:
    """
    [v3.5] Подставляет переменные в шаблон. Если ВСЕ плейсхолдеры в строке
    оказались пустыми — удаляем строку целиком. Это убирает «висячие» строки
    типа «• Email: <code></code>» или «• Баланс: <b> ₽</b>».
    """
    import re as _re
    placeholder_re = _re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
    out_lines = []
    for raw_line in template.split("\n"):
        # Все плейсхолдеры в этой строке
        names_in_line = placeholder_re.findall(raw_line)
        if names_in_line:
            # Если строка содержит плейсхолдеры — проверяем все ли пусты
            all_empty = all(
                not (vars_.get(n) or "").strip() for n in names_in_line
            )
            if all_empty:
                continue  # удаляем строку целиком
        # Подставляем значения (пустые тоже)
        line = raw_line
        for n in names_in_line:
            line = line.replace("{" + n + "}", vars_.get(n, ""))
        out_lines.append(line)
    return "\n".join(out_lines)


def _format_my_subscription(payload: dict | None) -> tuple[str | None, str | None]:
    """
    Формирует красивый блок с инфой о подписке клиента для самого клиента.

    [v3.5] Если в админке задан шаблон MENU_MYKEY_MENU — рендерим из него
    с подстановкой переменных. Иначе — fallback на старую логику (хардкод).

    Возвращает (text, sub_page_link). text=None если клиента нет в админке.
    """
    if not payload or not isinstance(payload, dict):
        return None, None

    # [v3.5] Сначала пробуем взять шаблон из админки.
    # Если в БД есть MENU_MYKEY_MENU — используем его.
    # Иначе падаем в старую (хардкод) логику ниже.
    try:
        from app import content_cache
        tpl_value = content_cache.get_text("MENU_MYKEY_MENU", "") or ""
    except Exception:
        tpl_value = ""

    if tpl_value:
        vars_, sub_link = _extract_subscription_vars(payload)
        if vars_ is None:
            return None, None
        rendered = _render_mykey_from_template(tpl_value, vars_)
        return rendered, sub_link

    data = payload.get("data") if "data" in payload else payload
    if not isinstance(data, dict):
        return None, None

    user = data.get("user") or {}
    if not user:
        return None, None

    remna = data.get("remnawave") or {}

    # sub_page_link достаём отсюда же — для удобства возврата
    sub_link_raw = data.get("sub_page_link")
    sub_page_link: str | None = None
    if isinstance(sub_link_raw, str) and sub_link_raw.startswith(("http://", "https://")):
        sub_page_link = sub_link_raw

    from datetime import datetime, timezone

    def _bytes_to_gb(val) -> str | None:
        try:
            n = float(val)
            if n <= 0:
                return None
            return f"{n / (1024 ** 3):.2f} GB"
        except (TypeError, ValueError):
            return None

    lines: list[str] = ["📶 <b>Мой роутер</b>", ""]

    # --- Статус подписки ---
    end_date = user.get("subscription_end_date")
    if end_date:
        try:
            s = str(end_date).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(tz=timezone.utc)
            if dt < now:
                days_ago = (now - dt).days
                lines.append(f"• Статус: 🔴 <b>истекла</b> {days_ago} дн. назад")
            else:
                days_left = (dt - now).days
                lines.append(f"• Статус: 🟢 <b>активна</b> (ещё {days_left} дн.)")
            lines.append(f"• Действует до: <b>{dt.strftime('%d.%m.%Y %H:%M')}</b>")
        except (ValueError, TypeError):
            lines.append(f"• Действует до: {end_date}")
    else:
        lines.append("• Статус: ❔ не определён")

    # --- Регистрация ---
    created = user.get("created_at")
    if created:
        try:
            s = str(created).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            lines.append(f"• Зарегистрированы: {dt.strftime('%d.%m.%Y')}")
        except (ValueError, TypeError):
            lines.append(f"• Зарегистрированы: {created}")

    # Лимита устройств у роутера нет: к нему подключаются по Wi-Fi
    # сколько угодно, поэтому строку клиенту не показываем.

    # --- Real Username (TG) ---
    real_username = user.get("real_username")
    if real_username:
        lines.append(f"• Username Telegram: @{real_username}")

    # --- Email (если задан) ---
    email = user.get("email")
    if email:
        # HTML-escape от & < > " — Telegram парсер чувствительный
        from html import escape
        lines.append(f"• Email: <code>{escape(str(email))}</code>")

    # --- Баланс партнёра ---
    balance = user.get("partner_balance_rub")
    if balance is not None:
        try:
            bal_f = float(balance)
            if bal_f > 0:
                lines.append(f"• Баланс партнёра: <b>{bal_f:.2f} ₽</b>")
        except (TypeError, ValueError):
            pass

    # --- Трафик за всё время (если Remnawave) ---
    if isinstance(remna, dict):
        traffic = remna.get("user_traffic") or {}
        lifetime = _bytes_to_gb(traffic.get("lifetime_used_traffic_bytes"))
        if lifetime:
            lines.append(f"• Трафик за всё время: <b>{lifetime}</b>")

    # --- Заблокирован? ---
    if user.get("is_blocked"):
        lines.append("")
        lines.append("⛔ <b>Подписка заблокирована.</b> Свяжитесь с поддержкой.")

    # --- Ключ для копирования (если есть) ---
    if sub_page_link:
        lines.append("")
        lines.append("🔑 <b>Ваш ключ подключения</b>")
        lines.append("<i>Нажмите, чтобы скопировать. Не передавайте никому.</i>")
        from html import escape
        lines.append(f"<blockquote>{escape(sub_page_link)}</blockquote>")

    return "\n".join(lines), sub_page_link


def _format_my_router(data: dict) -> str:
    """Карточка роутера для клиента.

    Показываем только то, что человеку понятно и полезно: связь, когда
    выходил, сколько устройств, до какого числа подписка. Технику вроде
    MAC, WAN-адреса и загрузки процессора клиенту знать незачем.
    """
    lines: list[str] = ["📶 <b>Мой роутер</b>", ""]
    routers = data.get("routers") or []

    for i, r in enumerate(routers):
        if len(routers) > 1:
            lines.append(f"<b>Роутер {i + 1}</b>")

        if r.get("online"):
            lines.append("• Связь: 🟢 <b>на связи</b>")
        else:
            lines.append("• Связь: 🔴 <b>молчит</b>")
            seen = _human_dt(r.get("last_seen"))
            if seen:
                lines.append(f"• Последний отклик: {seen}")

        model = (r.get("model") or "").strip()
        if model:
            lines.append(f"• Модель: {model}")

        clients = r.get("clients_connected")
        if isinstance(clients, int) and r.get("online"):
            lines.append(f"• Устройств в сети: <b>{clients}</b>")

        until = _human_dt(r.get("subscription_until"))
        if until:
            lines.append(f"• Подписка до: <b>{until}</b>")
        else:
            status = (r.get("subscription_status") or "").strip()
            lines.append(f"• Подписка: {status or 'не оформлена'}")

        fw = (r.get("firmware") or "").strip()
        if fw:
            lines.append(f"• Прошивка: {fw}")
        lines.append("")

    if not any(r.get("online") for r in routers):
        lines.append(
            "Роутер не выходит на связь. Проверьте, что он включён в "
            "розетку, а кабель провайдера — в порту <b>WAN</b>."
        )

    return "\n".join(lines).strip()


def _human_dt(value) -> str | None:
    """ISO-дата в вид, понятный человеку. Мусор молча пропускаем."""
    # datetime в этом модуле импортируется внутри функций, а не сверху.
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return None


@dp.callback_query(F.data == "m_mykey")
async def cb_mykey(callback: CallbackQuery, state: FSMContext):
    """Раздел «Мой роутер»: состояние устройства и подписки.

    Данные берём из парка устройств, а не из старой панели: подписка
    привязана к роутеру, и «на связи или молчит» знает только парк.
    """
    await state.clear()
    user_id = callback.from_user.id

    await callback.answer("Загружаю данные…")

    from app import ai_tools
    data = await ai_tools.get_my_router(user_id)

    if data.get("error"):
        await safe_edit(callback, T.MYKEY_FETCH_FAIL, mykey_back_keyboard())
        return

    if not data.get("has_router"):
        await safe_edit(callback, T.MYKEY_NOT_REGISTERED, mykey_back_keyboard())
        return

    await safe_edit(callback, _format_my_router(data), mykey_main_keyboard(None))


@dp.callback_query(F.data == "mk_reset")
async def cb_mykey_reset(callback: CallbackQuery):
    """Показывает клиенту предупреждение про сброс и кнопку для отправки запроса оператору."""
    await safe_edit(callback, T.MYKEY_RESET_WARNING, mykey_reset_confirm_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "mk_reset_send")
async def cb_mykey_reset_send(callback: CallbackQuery):
    """
    Клиент подтвердил запрос сброса — создаём тикет оператору с пометкой
    «КЛИЕНТ ПРОСИТ СБРОС ПОДПИСКИ». Сам бот сброс НЕ делает.
    """
    user_id = callback.from_user.id
    username = callback.from_user.username or "—"

    if await is_banned(user_id):
        await safe_edit(callback, T.MYKEY_RESET_TICKET_BANNED, mykey_back_keyboard())
        await callback.answer()
        return

    # Если у клиента уже есть открытый тикет — используем его, не создаём новый.
    existing = await get_user_open_ticket(user_id)
    if existing:
        try:
            await bot.send_message(
                SUPPORT_CHAT_ID,
                f"🔁 <b>КЛИЕНТ ПРОСИТ СБРОС ПОДПИСКИ</b>\n\n"
                f"👤 ID: <code>{user_id}</code>\n"
                f"🔗 Username: @{username}\n\n"
                f"<i>Запрос отправлен через раздел «Мой ключ» → "
                f"«Сбросить подписку».</i>",
                message_thread_id=existing["topic_id"],
                reply_markup=admin_action_panel(user_id, ADMIN_PANEL_URL),
                disable_notification=True,  # [v3.5] тихо
            )
        except Exception as e:
            logger.error("mk_reset_send: не удалось дописать в существующий топик {}: {}", existing["topic_id"], e)
            await safe_edit(callback, T.MYKEY_RESET_TICKET_FAIL, mykey_back_keyboard())
            await callback.answer()
            return

        await safe_edit(callback, T.MYKEY_RESET_SENT, mykey_back_keyboard())
        await callback.answer()
        return

    # Иначе создаём новый тикет (с rate-limit-проверкой внутри create_ticket).
    ticket_id = await create_ticket(user_id)
    if ticket_id is None:
        await safe_edit(callback, T.MYKEY_RESET_TICKET_RATELIMIT, mykey_back_keyboard())
        await callback.answer()
        return

    try:
        topic_id = await get_user_topic(user_id)
        if not topic_id:
            _icon_kw = {}
            _icon_id = _resolve_topic_icon(TOPIC_ICON_TG_EMOJI)
            if _icon_id:
                _icon_kw["icon_custom_emoji_id"] = _icon_id
            topic = await bot.create_forum_topic(
                chat_id=SUPPORT_CHAT_ID,
                name=_build_tg_topic_name(user_id, callback.from_user),
                **_icon_kw,
            )
            topic_id = topic.message_thread_id
        else:
            # Топик уже был — клиент создал новый тикет в существующем.
            # Возвращаем иконку на 💬 (была ✅ от закрытия) и переоткрываем
            # если был закрыт оператором
            try:
                await bot.reopen_forum_topic(
                    chat_id=SUPPORT_CHAT_ID, message_thread_id=topic_id,
                )
            except Exception:
                pass
            await _set_topic_icon(topic_id, TOPIC_ICON_TG_EMOJI)

        await save_topic(ticket_id, topic_id)

        # Расширенный блок из админки (по возможности)
        try:
            admin_info = await admin_panel.build_ticket_info_block(user_id)
        except Exception:
            admin_info = ""

        body = (
            f"🔁 <b>КЛИЕНТ ПРОСИТ СБРОС ПОДПИСКИ</b>\n"
            f"🎫 Тикет #{ticket_id}\n"
            f"👤 ID: <code>{user_id}</code>\n"
            f"🔗 Username: @{username}\n\n"
            f"🛠 <b>Админка:</b> {ADMIN_PANEL_URL}{user_id}"
            f"{admin_info}\n\n"
            f"<i>Запрос отправлен через раздел «Мой ключ» → "
            f"«Сбросить подписку». Бот сам сброс не сделал — "
            f"уточните детали у клиента и выполните вручную "
            f"(🛠 Админка → 🔁 Сброс подписки).</i>\n\n"
            f"<i>Команды в этом топике:</i>\n"
            f"<code>/info</code> · <code>/ban</code> · <code>/unban</code> · <code>/close</code> · <code>/help</code>"
        )

        await bot.send_message(
            SUPPORT_CHAT_ID,
            body,
            message_thread_id=topic_id,
            reply_markup=ticket_admin_keyboard(user_id, ADMIN_PANEL_URL),
            disable_notification=True,  # [v3.5] тихо
        )

        logger.warning(
            "MK_RESET_REQUEST: клиент={} (@{}) создал тикет #{}",
            user_id, username, ticket_id,
        )
    except Exception as e:
        logger.exception("mk_reset_send: ошибка создания тикета для {}: {}", user_id, e)
        await safe_edit(callback, T.MYKEY_RESET_TICKET_FAIL, mykey_back_keyboard())
        await callback.answer()
        return

    await safe_edit(callback, T.MYKEY_RESET_SENT, mykey_back_keyboard())
    await callback.answer()


# ============================================================
#  СОЗДАНИЕ ТИКЕТА (пользователь прислал первое сообщение)
# ============================================================

async def _ensure_topic_exists(
    user_id: int,
    name: str | None = None,
    user_obj=None,
) -> int:
    """
    [v3.5] Гарантированно возвращает РАБОЧИЙ topic_id для клиента.

    Логика:
    1) Берём последний topic_id из БД (если был)
    2) Пытаемся переоткрыть его в Telegram
    3) Если Telegram говорит «топик не найден» (БД была очищена, оператор
       удалил тему вручную, и т.п.) — создаём новую тему
    4) Если topic_id из БД не было — сразу создаём новую тему

    user_obj — Telegram User объект (для @username / "Имя" в имени топика).

    Возвращает рабочий topic_id.
    """
    topic_id = await get_user_topic(user_id)
    topic_name = name or _build_tg_topic_name(user_id, user_obj)

    _icon_kw = {}
    _icon_id = _resolve_topic_icon(TOPIC_ICON_TG_EMOJI)
    if _icon_id:
        _icon_kw["icon_custom_emoji_id"] = _icon_id

    # Случай: в БД нет topic_id → сразу создаём новый
    if not topic_id:
        topic = await bot.create_forum_topic(
            chat_id=SUPPORT_CHAT_ID,
            name=topic_name,
            **_icon_kw,
        )
        return topic.message_thread_id

    # Случай: есть topic_id → пробуем переиспользовать
    try:
        await bot.reopen_forum_topic(
            chat_id=SUPPORT_CHAT_ID, message_thread_id=topic_id,
        )
        await _set_topic_icon(topic_id, TOPIC_ICON_TG_EMOJI)
        return topic_id
    except Exception as e:
        err = str(e)
        err_lower = err.lower()
        # [v3.5] TOPIC_NOT_MODIFIED — топик уже открыт, это НЕ ошибка.
        # Просто возвращаем существующий id, не создаём новый.
        if "TOPIC_NOT_MODIFIED" in err or "topic_not_modified" in err_lower:
            logger.debug(
                "Топик {} уже открыт (TOPIC_NOT_MODIFIED), переиспользую",
                topic_id,
            )
            try:
                await _set_topic_icon(topic_id, TOPIC_ICON_TG_EMOJI)
            except Exception:
                pass
            return topic_id

        # [v3.5] СТРОГИЙ список маркеров «топик реально удалён».
        # Раньше тут было `or "topic" in err` — оно ловило TOPIC_NOT_MODIFIED
        # и ошибочно создавало дубль (баг «два топика на одно сообщение»).
        invalid_markers = (
            "topic_deleted", "topic deleted",
            "topic_not_found", "topic not found",
            "thread not found", "thread_not_found",
            "message thread not found",
            "topic was deleted",
        )
        is_really_invalid = any(m in err_lower for m in invalid_markers)
        if is_really_invalid:
            logger.warning(
                "Топик {} реально удалён для user {} ({}). Создаю новый.",
                topic_id, user_id, e,
            )
            topic = await bot.create_forum_topic(
                chat_id=SUPPORT_CHAT_ID,
                name=topic_name,
                **_icon_kw,
            )
            return topic.message_thread_id
        # Какая-то другая ошибка — пытаемся продолжить с тем же topic_id.
        logger.warning(
            "reopen_forum_topic({}) для user {} не удался ({}), "
            "продолжаю с тем же topic_id",
            topic_id, user_id, e,
        )
        return topic_id


# [v3.5] Маркеры ошибок Telegram «топик невалиден» — оператор удалил топик
# вручную, БД содержит несуществующий topic_id, и т.п. Если send_message
# падает с таким — пересоздаём топик и шлём в новый. Без этого сообщения
# уходят в General topic группы поддержки (баг: «удалил топик → клиент пишет
# в общий чат»).
_TOPIC_INVALID_MARKERS = (
    "topic_deleted", "topic deleted",
    "topic_not_found", "topic not found",
    "thread not found", "thread_not_found",
    "message thread not found",
    "topic_closed",
)


async def _safe_send_to_topic(
    user_id: int,
    method: str,
    **kwargs,
):
    """[v3.5] Безопасная отправка в топик с fallback: если топик был удалён
    вручную в TG — пересоздаём его и шлём в новый topic_id.

    method: 'send_message' | 'send_photo' | 'send_document' | 'copy_message'
    kwargs: все параметры кроме chat_id/message_thread_id

    Также после успешного создания нового топика обновляет topic_id в БД
    через save_topic (для текущего активного тикета клиента).
    """
    topic_id = kwargs.pop("message_thread_id", None)
    if topic_id is None:
        # На всякий случай — берём из БД
        topic_id = await get_user_topic(user_id)

    send_func = getattr(bot, method, None)
    if send_func is None:
        logger.error("_safe_send_to_topic: неизвестный метод {!r}", method)
        return None

    # Первая попытка
    try:
        return await send_func(
            chat_id=SUPPORT_CHAT_ID,
            message_thread_id=topic_id,
            **kwargs,
        )
    except Exception as e:
        err = str(e).lower()
        is_topic_error = (
            any(m in err for m in _TOPIC_INVALID_MARKERS)
            or "topic" in err
        )
        if not is_topic_error:
            # Не наша ошибка — пробрасываем дальше
            raise
        logger.warning(
            "_safe_send_to_topic: topic {} невалиден для user {} ({}). "
            "Создаю новый и повторяю отправку.",
            topic_id, user_id, e,
        )

    # Создаём новый topic и сохраняем в БД активному тикету
    try:
        new_topic_id = await _ensure_topic_exists(user_id)
    except Exception as e:
        logger.exception(
            "_safe_send_to_topic: не смог создать новый топик: {}", e,
        )
        return None

    # Обновляем topic_id в активном тикете клиента
    try:
        ticket = await get_last_ticket_for_user(user_id)
        if ticket:
            await save_topic(ticket["id"], new_topic_id)
            logger.info(
                "_safe_send_to_topic: ticket #{} → topic_id {} (был {})",
                ticket["id"], new_topic_id, topic_id,
            )
    except Exception as e:
        logger.warning("_safe_send_to_topic: save_topic failed: {}", e)

    # Шлём шапку «топик восстановлен» с историей если она есть
    try:
        username_safe = "—"
        try:
            chat = await bot.get_chat(user_id)
            username_safe = chat.username or "—"
        except Exception:
            pass
        try:
            admin_info = await admin_panel.build_ticket_info_block(user_id)
        except Exception:
            admin_info = ""
        recover_header = (
            "♻️ <b>Топик пересоздан</b>\n"
            "<i>(старый topic был удалён, но история клиента сохранена в БД)</i>\n\n"
            f"👤 <b>User ID:</b> <code>{user_id}</code>\n"
            f"🔗 <b>Username:</b> @{username_safe}\n\n"
            f"🛠 <b>Админка:</b> {ADMIN_PANEL_URL}{user_id}"
            f"{admin_info}"
        )
        await bot.send_message(
            chat_id=SUPPORT_CHAT_ID,
            message_thread_id=new_topic_id,
            text=recover_header,
            parse_mode="HTML",
            reply_markup=ticket_admin_keyboard(user_id, ADMIN_PANEL_URL),
            disable_notification=True,  # [v3.5] тихо
        )
    except Exception as e:
        logger.warning("_safe_send_to_topic: recover header failed: {}", e)

    # Повторная попытка в новый topic
    try:
        return await send_func(
            chat_id=SUPPORT_CHAT_ID,
            message_thread_id=new_topic_id,
            **kwargs,
        )
    except Exception as e:
        logger.exception(
            "_safe_send_to_topic: повторная отправка тоже упала: {}", e,
        )
        return None


async def _create_support_ticket(message: Message) -> int | None:
    """[v3.5] Эскалация к оператору: get_or_create_ticket + reopen.
    Если у клиента уже есть тикет — переоткрываем его (статус → 'open').
    Если нет — создаём новый, сразу с открытым статусом для эскалации.
    Тикет/топик никогда не пересоздаётся — вся история в одном тикете.
    """
    user_id = message.from_user.id
    username = message.from_user.username or "—"

    # Получаем или создаём тикет клиента (всегда один на клиента)
    ticket = await get_or_create_ticket(user_id)
    ticket_id = ticket["id"]
    was_status = ticket.get("status", "closed")
    was_topic_id = ticket.get("topic_id")

    # [v3.5] _create_support_ticket вызывается ТОЛЬКО когда клиент только что
    # написал и тикета open у него нет → это новый клиент или возврат
    # после закрытия. В обоих случаях нужен ЗВУК — оператор должен
    # заметить клиента.
    is_first_time = was_topic_id is None
    is_reopen = (was_status == "closed" and not is_first_time)
    quiet = False  # ВСЕГДА со звуком

    # Переоткрываем (closed → open) или оставляем open
    if was_status != "open":
        await reopen_ticket(ticket_id)

    logger.info(
        "🔔 _create_support_ticket: user={} ticket_id={} was_status={!r} "
        "was_topic_id={} is_first={} is_reopen={} quiet={}",
        user_id, ticket_id, was_status, was_topic_id,
        is_first_time, is_reopen, quiet,
    )

    # [v3.5] Helper гарантирует рабочий topic_id (переоткрывает старый
    # или создаёт новый если старый удалён). user_obj даст красивое
    # имя топика (@username / "Имя Фамилия") вместо голого user_id.
    try:
        topic_id = await _ensure_topic_exists(user_id, user_obj=message.from_user)
    except Exception as e:
        logger.exception("_create_support_ticket: не смог создать топик: {}", e)
        return None

    await save_topic(ticket_id, topic_id)

    # [v3.5] Настройки бота — нужны раньше для управления уведомлением
    # «Клиент вернулся» и его звуком. Если bot_settings по какой-то
    # причине упал — продолжаем с безопасными дефолтами (всё включено).
    try:
        from app import bot_settings as _bs
        notify_returned = _bs.get_bool("notify_client_returned")
        sound_ret = _bs.get_bool("sound_client_returned")
        header_short = _bs.get_bool("ticket_header_short")
        load_admin_info = _bs.get_bool("load_admin_info_on_ticket")
        sound_new = _bs.get_bool("sound_new_ticket")
    except Exception as e:
        logger.warning("bot_settings unavailable, using defaults: {}", e)
        notify_returned = True
        sound_ret = True
        header_short = False
        load_admin_info = True
        sound_new = True

    # [v3.5] Если это переоткрытие (клиент вернулся после закрытия) —
    # шлём яркое уведомление чтобы оператор заметил.
    # Управляется notify_client_returned + sound_client_returned.
    if is_reopen and notify_returned:
        try:
            await bot.send_message(
                SUPPORT_CHAT_ID,
                "🔔 <b>Клиент вернулся в чат</b>\n"
                f"<i>Тикет #{ticket_id} был закрыт — переоткрыт.</i>",
                message_thread_id=topic_id,
                parse_mode="HTML",
                disable_notification=not sound_ret,
            )
        except Exception as e:
            logger.warning("notify return-client failed: {}", e)

    # Финальный disable_notification: тихо если был open ИЛИ звук выключен
    if was_status == "open":
        quiet = True
    elif is_reopen:
        quiet = not sound_ret
    else:
        quiet = not sound_new

    # Расширенный блок из админки (может быть пустым, если ADMIN_PASSWORD
    # не задан или админка недоступна — в этом случае шапка остаётся минимальной)
    admin_info = ""
    if load_admin_info:
        try:
            admin_info = await admin_panel.build_ticket_info_block(user_id)
        except Exception as e:
            logger.warning("Не удалось получить инфу из админки для {}: {}", user_id, e)
            admin_info = ""

    if header_short:
        # [v3.5] Короткая шапка — только ID + ссылка на админку
        header = (
            f"🎫 <b>Тикет #{ticket_id}</b> "
            f"· <code>{user_id}</code> · @{username}\n"
            f"🛠 {ADMIN_PANEL_URL}{user_id}"
            f"{admin_info}"
        )
    else:
        header = (
            f"🎫 <b>Тикет #{ticket_id}</b>\n"
            f"👤 <b>User ID:</b> <code>{user_id}</code>\n"
            f"🔗 <b>Username:</b> @{username}\n\n"
            f"🛠 <b>Админка:</b> {ADMIN_PANEL_URL}{user_id}"
            f"{admin_info}\n\n"
            f"<i>Команды в этом топике:</i>\n"
            f"<code>/info</code> — обновить инфу клиента\n"
            f"<code>/ban</code> — забанить · <code>/unban</code> — снять бан\n"
            f"<code>/close</code> — закрыть · <code>/help</code> — все команды"
        )

    # Сначала шлём шапку тикета с клавиатурой.
    # [v3.5] Если отправка падает — топик невалидный, создаём новый и
    # повторяем. Это редкий случай (например БД восстановлена из бэкапа,
    # а Telegram-темы уже нет).
    try:
        await bot.send_message(
            SUPPORT_CHAT_ID,
            header,
            message_thread_id=topic_id,
            reply_markup=ticket_admin_keyboard(user_id, ADMIN_PANEL_URL),
            disable_notification=quiet,  # [v3.5] звук при новом/переоткрытии
        )
    except Exception as e:
        err = str(e).lower()
        if any(s in err for s in (
            "topic_deleted", "topic not found", "thread not found",
            "message thread not found", "topic_closed",
        )):
            logger.warning(
                "Шапка не ушла в topic={} (невалидный), создаю новый: {}",
                topic_id, e,
            )
            # Создаём новый топик и пробуем снова
            _icon_kw = {}
            _icon_id = _resolve_topic_icon(TOPIC_ICON_TG_EMOJI)
            if _icon_id:
                _icon_kw["icon_custom_emoji_id"] = _icon_id
            new_topic = await bot.create_forum_topic(
                chat_id=SUPPORT_CHAT_ID,
                name=_build_tg_topic_name(user_id, message.from_user),
                **_icon_kw,
            )
            topic_id = new_topic.message_thread_id
            await save_topic(ticket_id, topic_id)
            await bot.send_message(
                SUPPORT_CHAT_ID,
                header,
                message_thread_id=topic_id,
                reply_markup=ticket_admin_keyboard(user_id, ADMIN_PANEL_URL),
                disable_notification=quiet,  # [v3.5] звук при новом/переоткрытии
            )
        else:
            # Другая ошибка — переброс
            raise

    # [v3.5] Если AI эскалировал — покажем оператору причину (исчерпан лимит,
    # ошибка API, штатная передача — чтобы оператор сразу понимал контекст).
    escalate_reason = _consume_ai_escalate_reason(user_id)
    if escalate_reason:
        try:
            await bot.send_message(
                SUPPORT_CHAT_ID,
                escalate_reason,
                message_thread_id=topic_id,
                disable_notification=True,  # [v3.5] тихо
            )
        except Exception as e:
            logger.warning("Не смог отправить причину эскалации: {}", e)

    # [v3.5] Переписку AI с клиентом НЕ дублируем в шапку — она уже видна
    # в этом же топике через зеркалирование "💬 Клиент:" / "🤖 AI:" по
    # ходу диалога (см. try_ai_for_user). При эскалации ghost-topic
    # превращается в реальный тикет, и оператор видит всю переписку как
    # она шла. Раньше тут вызывался format_history_for_operator() —
    # это создавало дубль.

    # Затем пересылаем само сообщение пользователя
    fwd = await bot.forward_message(
        chat_id=SUPPORT_CHAT_ID,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
        message_thread_id=topic_id,
        disable_notification=quiet,  # [v3.5] звук при новом/переоткрытии
    )
    # [v3.5] Маппинг для reply оператора: id forward'а в группе → id оригинала в чате клиента
    try:
        await save_tg_message_map(fwd.message_id, user_id, message.message_id)
    except Exception as e:
        logger.debug("save_tg_message_map (initial forward) failed: {}", e)

    # Сохраняем сообщение в БД для отображения в админке
    try:
        kind, text_for_log = _extract_message_kind_and_text(message)
        await save_message(
            ticket_id=ticket_id, user_id=user_id, topic_id=topic_id,
            direction="in", kind=kind, text=text_for_log,
        )
    except Exception as e:
        logger.warning("save_message (initial) failed: {}", e)

    return ticket_id


# ============================================================
#  AI: попытка автоматического ответа клиенту перед эскалацией
# ============================================================

async def _cleanup_ai_state_for_user(user_id: int) -> None:
    """
    [v3.5] Очищает AI-состояние для клиента после закрытия его тикета:
    - очищает историю диалога (чтобы AI начал с чистого листа в следующий раз)
    - сбрасывает флаг operator_joined у тикетов клиента
    - сбрасывает FSM state клиента (чтобы он не остался в
      awaiting_ticket_message — иначе следующее сообщение пойдёт в
      create_ticket_handler и сразу создаст новый тикет, минуя AI)

    Вызывать ПОСЛЕ close_ticket_db чтобы клиент мог снова получить ответы от AI.
    """
    try:
        from app import ai_assistant
        await ai_assistant.clear_history(str(user_id))
    except Exception as e:
        logger.warning("cleanup AI state: clear_history failed: {}", e)

    # [v3.5 fix] Сбрасываем operator_joined на тикетах клиента — иначе
    # AI навсегда замолчит после первого контакта с оператором.
    try:
        import aiosqlite
        from app.database import DB_PATH
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE tickets SET operator_joined = 0 WHERE user_id = ?",
                (user_id,),
            )
            await db.commit()
    except Exception as e:
        logger.warning("cleanup AI state: reset operator_joined failed: {}", e)

    # [v3.5] Сбрасываем FSM state клиента через storage Dispatcher'а.
    # Это критично для бага «после закрытия клиент сразу попадает в
    # create_ticket_handler и тикет дублируется».
    try:
        from aiogram.fsm.storage.base import StorageKey
        bot_id = bot.id if bot else 0
        key = StorageKey(bot_id=bot_id, chat_id=user_id, user_id=user_id)
        await dp.storage.set_state(key, None)
        await dp.storage.set_data(key, {})
    except Exception as e:
        logger.warning("cleanup AI state: FSM reset failed: {}", e)

    # Также сбрасываем сохранённую причину эскалации
    _last_ai_escalate_reason.pop(user_id, None)


# [v3.5] Запоминаем последнюю причину эскалации AI для клиента.
# Когда upstream создаёт тикет — может проверить эту причину и добавить
# в шапку уведомление вида «⚠️ AI исчерпал суточный лимит токенов».
# Очищается после прочтения или при закрытии тикета.
_last_ai_escalate_reason: dict[int, str] = {}


def _consume_ai_escalate_reason(user_id: int) -> str | None:
    """Извлекает и очищает причину последней эскалации AI."""
    return _last_ai_escalate_reason.pop(user_id, None)


def _extract_reply_context(message: Message) -> str | None:
    """[v3.5] Если клиент делает reply на конкретное сообщение в боте —
    возвращает короткое описание исходного сообщения для AI/контекста.

    Это критично для коротких уточнений: клиент пишет "А с лимитом?"
    реплаем на длинный ответ AI про тарифы. Без reply-контекста AI не
    понимает что это уточнение и отвечает невпопад.

    Возвращает строку формата:
        [Клиент отвечает на сообщение: "...(до 200 символов)..."]

    Или None если reply нет.
    """
    if not message.reply_to_message:
        return None

    rm = message.reply_to_message

    # Берём текст или caption того сообщения на которое отвечают
    orig_text = (rm.text or rm.caption or "").strip()

    # Если оригинал содержал quote (выделенный фрагмент) — приоритет ему
    quote_text = None
    try:
        if rm.quote and rm.quote.text:
            quote_text = rm.quote.text.strip()
    except AttributeError:
        # Bot API < 7.0 — нет quote
        pass

    # quote приходит на самом message (не на reply_to) в новом API
    try:
        if message.quote and message.quote.text:
            quote_text = message.quote.text.strip()
    except AttributeError:
        pass

    if quote_text:
        # Клиент выделил конкретную фразу — это самый ценный сигнал
        snippet = quote_text[:200]
        return f'[Клиент уточняет именно про эту фразу: "{snippet}"]'

    if not orig_text:
        # Нет ни текста, ни caption — возможно reply на фото без подписи
        return None

    # Сокращаем длинные сообщения чтобы не раздувать промпт
    snippet = orig_text[:200]
    if len(orig_text) > 200:
        snippet += "..."

    return f'[Клиент отвечает на сообщение: "{snippet}"]'


async def try_ai_for_user(message: Message) -> str | None:
    """
    Пробует ответить клиенту через AI-ассистента.

    Возвращает:
      - None — если AI выключен, нет ключа, ошибка, или AI эскалировал
        (тогда дальше создаётся обычный тикет / forward в группу).
      - текст ответа — если AI успешно ответил, и эскалации НЕ требуется
        (бот уже отправил этот текст клиенту, дальше ничего делать не нужно).

    Также сам отправляет ответ клиенту, чтобы upstream-код знал что делать
    дальше (создавать тикет или нет).
    """
    from admin_web import ai_settings
    from app import ai_assistant

    # 1. AI должен быть включён глобально
    if not ai_settings.is_enabled():
        return None

    # 2. У клиента должен быть текст ИЛИ фото. [v3.5]
    # Фото обрабатываем через vision-модель (gpt-4o, gpt-4o-mini).
    user_text = (message.text or message.caption or "").strip()

    # [v3.5] Если клиент делает reply на конкретное сообщение — добавляем
    # контекст. AI поймёт что это уточнение по предыдущему ответу, а не
    # новый вопрос. Без этого AI часто отвечает невпопад на короткие
    # уточнения вроде "А что с лимитом?" после длинного ответа про тарифы.
    reply_ctx = _extract_reply_context(message)
    if reply_ctx and user_text:
        user_text = f"{reply_ctx}\n\n{user_text}"
    elif reply_ctx:
        user_text = reply_ctx

    # [v3.5] Если есть фото — скачаем для передачи в AI vision
    image_data_url: str | None = None
    if message.photo:
        try:
            # Берём фото максимального размера (последнее в массиве)
            photo = message.photo[-1]
            file = await bot.get_file(photo.file_id)
            buf = await bot.download_file(file.file_path)
            import base64 as _b64
            img_bytes = buf.read() if hasattr(buf, "read") else bytes(buf)
            # Лимит ~5 MB для отправки в vision API
            if len(img_bytes) > 5 * 1024 * 1024:
                logger.info(
                    "try_ai_for_user: фото слишком большое ({} байт), AI пропускает",
                    len(img_bytes),
                )
            else:
                b64 = _b64.b64encode(img_bytes).decode("ascii")
                image_data_url = f"data:image/jpeg;base64,{b64}"
                if not user_text:
                    user_text = "Посмотри что на фото и помоги."
                logger.info(
                    "try_ai_for_user: фото подготовлено для vision ({} байт)",
                    len(img_bytes),
                )
        except Exception as e:
            logger.warning("try_ai_for_user: не смог скачать фото: {}", e)

    if not user_text and not image_data_url:
        return None
    # Слишком длинное — лучше оператору
    if len(user_text) > 2000:
        return None

    user_id = message.from_user.id

    # 3. Если оператор уже подключён к этому клиенту — AI молчит
    try:
        joined = await is_ticket_operator_joined(user_id)
        logger.info(
            "try_ai_for_user: user={} is_operator_joined={}",
            user_id, joined,
        )
        if joined:
            return None
    except Exception as e:
        logger.warning(
            "try_ai_for_user: is_operator_joined упало для user={}: {}",
            user_id, e,
        )

    # 4. Идём в AI
    try:
        await bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass

    # [v3.5] Гарантируем что у клиента есть тикет (ghost со status='closed').
    # При первом сообщении создаст пустой тикет — AI-история будет в нём,
    # оператор сможет посмотреть переписку даже до эскалации.
    # ТАКЖЕ — создаём topic в TG-группе сразу (с иконкой «🤖 AI») и
    # зеркалим туда сообщения клиента + ответы AI, чтобы операторы
    # видели переписку в реальном времени.
    ticket_data = None
    ghost_topic_id: int | None = None
    try:
        ticket_data = await get_or_create_ticket(user_id)
        ghost_topic_id = ticket_data.get("topic_id")
        if not ghost_topic_id:
            # Тикета не было или у него ещё нет topic_id — создаём topic
            try:
                ghost_topic_id = await _ensure_topic_exists(
                    user_id, user_obj=message.from_user,
                )
                await save_topic(ticket_data["id"], ghost_topic_id)
                # Шапка ghost-тикета — оператор видит «AI работает»
                try:
                    admin_info = await admin_panel.build_ticket_info_block(user_id)
                except Exception:
                    admin_info = ""
                username = message.from_user.username or "—"
                ghost_header = (
                    f"🤖 <b>AI-диалог #{ticket_data['id']}</b>\n"
                    f"👤 <b>User ID:</b> <code>{user_id}</code>\n"
                    f"🔗 <b>Username:</b> @{username}\n\n"
                    f"🛠 <b>Админка:</b> {ADMIN_PANEL_URL}{user_id}"
                    f"{admin_info}\n\n"
                    f"<i>Тикет в режиме AI (status='closed'). Если AI "
                    f"не справится — статус автоматически станет 'open' "
                    f"и придёт уведомление.</i>"
                )
                try:
                    await bot.send_message(
                        SUPPORT_CHAT_ID,
                        ghost_header,
                        message_thread_id=ghost_topic_id,
                        parse_mode="HTML",
                        reply_markup=ticket_admin_keyboard(user_id, ADMIN_PANEL_URL),
                        disable_notification=True,  # [v3.5] тихо
                    )
                except Exception as e:
                    logger.warning("ghost_topic header send failed: {}", e)
                # Закрываем topic в TG (он будет переоткрыт при эскалации)
                # — оставляем открытым, чтобы операторы видели диалог.
                # Иконка — обычная (потом меняется на «нужен оператор»)
                logger.info(
                    "Создан ghost-topic {} для AI-диалога с user {}",
                    ghost_topic_id, user_id,
                )
                # [v3.5] Закрываем topic в TG для AI-режима — оператор увидит
                # его в архиве. При эскалации reopenForumTopic переоткроет.
                try:
                    await bot.close_forum_topic(
                        chat_id=SUPPORT_CHAT_ID,
                        message_thread_id=ghost_topic_id,
                    )
                    await _set_topic_icon(ghost_topic_id, TOPIC_ICON_CLOSED_EMOJI)
                    logger.info(
                        "ghost-topic {} закрыт в TG (AI-режим)", ghost_topic_id,
                    )
                except Exception as e:
                    logger.warning("ghost_topic TG close failed: {}", e)
            except Exception as e:
                logger.warning("ghost_topic create failed: {}", e)
                ghost_topic_id = None
    except Exception as e:
        logger.warning("try_ai_for_user: get_or_create_ticket failed: {}", e)

    # [v3.5] Зеркалим сообщение клиента в ghost-topic
    if ghost_topic_id:
        try:
            # Если у сообщения есть фото — отправляем фото с подписью
            client_caption = f"💬 <b>Клиент:</b>\n{user_text[:3500]}"
            if message.photo:
                _mirror = await bot.send_photo(
                    chat_id=SUPPORT_CHAT_ID,
                    message_thread_id=ghost_topic_id,
                    photo=message.photo[-1].file_id,
                    caption=client_caption,
                    parse_mode="HTML",
                    disable_notification=True,  # [v3.5] тихо
                )
            else:
                _mirror = await bot.send_message(
                    chat_id=SUPPORT_CHAT_ID,
                    message_thread_id=ghost_topic_id,
                    text=client_caption,
                    parse_mode="HTML",
                    disable_notification=True,  # [v3.5] тихо
                )
            # [v3.5] Маппинг: зеркало в ghost-topic → оригинал клиента
            try:
                await save_tg_message_map(
                    _mirror.message_id, user_id, message.message_id,
                )
            except Exception as e:
                logger.debug("save_tg_message_map (mirror) failed: {}", e)
        except Exception as e:
            logger.warning("ghost_topic mirror client msg failed: {}", e)

    # [v3.5] Rate limit — защита от спама
    from app import security
    allowed, rl_reason = await security.check_message_rate(str(user_id))
    if not allowed:
        logger.info("try_ai_for_user: rate-limit для user {}: {}", user_id, rl_reason)
        try:
            await message.answer(
                "⏱ Вы пишете слишком часто. Подождите немного и попробуйте снова.",
            )
        except Exception:
            pass
        return ""  # «AI справился» — чтобы upstream не создавал тикет
    await security.record_message(str(user_id))

    # [v3.5] Защита от prompt injection — фильтр входящего текста
    user_text, injection_reason = await security.sanitize_user_input(user_text)
    if injection_reason:
        logger.warning(
            "try_ai_for_user: prompt-injection попытка от user {}: {}",
            user_id, injection_reason,
        )

    # tg_user_id передаём отдельно — AI использует его для вызова tools
    # (получить подписку/платежи/устройства/трафик клиента).
    # source='telegram' — AI получит канал-специфичную секцию промпта. [v3.5]
    result = await ai_assistant.ask(
        str(user_id), user_text,
        tg_user_id=user_id,
        source="telegram",
        image_data_url=image_data_url,
    )

    # Имя ассистента — настраивается в админке (/ai → блок «Имя ассистента»).
    # Клиент видит это имя как отправителя. Можно поставить «Поддержка», «Анна»,
    # чтобы клиент не догадался что говорит с ботом.
    try:
        from admin_web import ai_settings as _ais
        from html import escape as _html_escape
        assistant_name = _html_escape(_ais.get_assistant_name())
    except Exception:
        assistant_name = "Поддержка"

    if not result.ok or result.escalate:
        # [v3.5] Запоминаем причину эскалации — upstream добавит её в шапку тикета,
        # чтобы оператор видел что произошло (исчерпан лимит, ошибка API и т.п.)
        reason_text = None
        err = (result.error or "").lower()
        if err == "quota_exceeded":
            reason_text = "⚠️ AI исчерпал суточный лимит токенов клиента"
        elif err == "no api key":
            reason_text = "⚠️ AI отключён (нет API-ключа)"
        elif err == "ai disabled":
            reason_text = "⚠️ AI выключен в админке"
        elif err == "timeout":
            reason_text = "⚠️ AI не ответил вовремя (таймаут)"
        elif "vision_not_supported" in err:
            reason_text = (
                "⚠️ AI не смог посмотреть фото — текущая модель не поддерживает "
                "обработку изображений. Переключите модель на gpt-4o-mini в админке /ai."
            )
        elif "http" in err:
            reason_text = f"⚠️ AI вернул ошибку: {result.error}"
        elif result.text and result.text.strip():
            # AI эскалирует штатно (сказал [ESCALATE] с текстом)
            reason_text = "🤖 AI передаёт диалог оператору"
        if reason_text:
            _last_ai_escalate_reason[user_id] = reason_text

        # AI эскалирует. Если у него есть осмысленный текст —
        # отправим его клиенту перед тем как создавать тикет.
        if result.text and result.text.strip():
            try:
                escalate_text = (
                    f"<b>{assistant_name}:</b>\n\n"
                    f"{result.text.strip()}"
                )
                await message.answer(escalate_text, parse_mode="HTML")
                logger.info(
                    "try_ai_for_user: escalate с текстом для user={}",
                    user_id,
                )
            except Exception as e:
                logger.warning(
                    "try_ai_for_user: не смог отправить escalate-текст: {}",
                    e,
                )
        # Возвращаем None — upstream создаст тикет
        return None

    # Успех — отправляем клиенту с пометкой что это AI
    answer = (
        f"<b>{assistant_name}:</b>\n\n"
        f"{result.text}"
    )
    try:
        await message.answer(answer, parse_mode="HTML")
    except Exception as e:
        logger.warning("try_ai_for_user: не смог отправить ответ: {}", e)
        return None

    # [v3.5] Зеркалим ответ AI в ghost-topic (чтобы оператор видел переписку).
    # Префикс «🤖 AI:» отличает от ответов оператора.
    if ghost_topic_id:
        try:
            await bot.send_message(
                chat_id=SUPPORT_CHAT_ID,
                message_thread_id=ghost_topic_id,
                text=f"🤖 <b>AI:</b>\n{result.text[:3500]}",
                parse_mode="HTML",
                disable_notification=True,  # [v3.5] тихо
            )
        except Exception as e:
            logger.warning("ghost_topic mirror AI answer failed: {}", e)

    # [v3.5] Если AI указал маркер [PHOTO:qa_key] — отправим фото из QA
    # клиенту вместе с текстовым ответом.
    if result.photo_keys:
        for qa_key in result.photo_keys:
            try:
                photo_paths = await ai_assistant.get_qa_photo_paths(qa_key)
                if not photo_paths:
                    logger.info(
                        "try_ai_for_user: AI запросил [PHOTO:{}] но фото нет",
                        qa_key,
                    )
                    continue
                # Шлём каждое фото отдельным сообщением (или альбомом если >1)
                for photo_path in photo_paths[:5]:  # лимит 5 фото на ответ
                    try:
                        photo_file = FSInputFile(photo_path)
                        await bot.send_photo(
                            chat_id=message.chat.id,
                            photo=photo_file,
                        )
                    except Exception as e:
                        logger.warning(
                            "try_ai_for_user: не смог отправить фото {}: {}",
                            photo_path, e,
                        )
            except Exception as e:
                logger.warning(
                    "try_ai_for_user: ошибка обработки [PHOTO:{}]: {}",
                    qa_key, e,
                )

    return result.text


@dp.message(SupportStates.awaiting_ticket_message, F.chat.type == "private")
async def create_ticket_handler(message: Message, state: FSMContext):
    # [v3.5] ДИАГНОСТИКА: видим что клиент попал сюда (state awaiting)
    logger.warning(
        "📩 CREATE_TICKET_HANDLER user={} text={!r}",
        message.from_user.id,
        (message.text or message.caption or f"<{message.content_type}>")[:80],
    )
    if await is_banned(message.from_user.id):
        await message.answer(T.USER_BANNED)
        await state.clear()
        return

    # Пробуем AI ДО создания тикета
    ai_answer = await try_ai_for_user(message)
    if ai_answer:
        # AI справился. Тикет не создаём, но запомним что был контакт.
        await state.clear()
        return

    ticket = await _create_support_ticket(message)
    # [v3.5] state.clear() ВСЕГДА (раньше при ошибке rate-limit state
    # оставался awaiting_ticket_message — следующее сообщение клиента
    # снова попадало сюда и пыталось создавать тикет → дубликат).
    await state.clear()
    if ticket is None:
        await message.answer(T.TICKET_RATE_LIMIT)
        return

    await message.answer(T.TICKET_CREATED)


# ============================================================
#  ОПЕРАТОР → ПОЛЬЗОВАТЕЛЬ
#  (сообщения в чате поддержки внутри топика тикета)
# ============================================================

@dp.message(F.chat.id == SUPPORT_CHAT_ID)
async def support_reply(message: Message):
    # Игнорим сообщения от ботов (включая нашего собственного)
    if message.from_user and message.from_user.is_bot:
        return
    if not message.message_thread_id:
        return

    # === Проверка: это ПИН-код для подтверждения сброса подписки? ===
    text_raw = (message.text or "").strip()
    if (
        message.reply_to_message
        and len(text_raw) == 4
        and text_raw.isdigit()
    ):
        key = (message.chat.id, message.message_thread_id, message.from_user.id)
        pending = pending_revokes.get(key)
        if pending and message.reply_to_message.message_id == pending["prompt_message_id"]:
            # Это попытка подтвердить сброс. Не пересылаем клиенту!
            await _process_revoke_pin(message, key, pending, text_raw)
            return

    # === Проверка: это ввод количества дней для extend/reduce? ===
    if (
        message.reply_to_message
        and text_raw.isdigit()
        and 1 <= len(text_raw) <= 4
    ):
        key = (message.chat.id, message.message_thread_id, message.from_user.id)
        pending_d = pending_days_input.get(key)
        if pending_d and message.reply_to_message.message_id == pending_d["prompt_message_id"]:
            await _process_days_input(message, key, pending_d, int(text_raw))
            return

    # Команды модерации внутри топика
    text = text_raw
    if text.startswith("/"):
        await _handle_admin_command(message)
        return

    user_id = await get_user_by_topic(message.message_thread_id)
    if not user_id:
        # Возможно это веб-чат — проверим
        from app import web_chat_db
        web_visitor = await web_chat_db.get_visitor_by_topic(message.message_thread_id)
        if web_visitor:
            # Это сообщение оператора для веб-клиента
            visitor_id = web_visitor["visitor_id"]
            # Имя оператора берём ИЗ НАСТРОЕК ВИДЖЕТА (operator_label),
            # а не из имени TG-юзера. Это чтобы клиент видел единого «Оператора»,
            # а не разные имена админов.
            operator_name = "Оператор"
            try:
                from app import widget_settings
                _ws = await widget_settings.get_settings()
                _label = (_ws.get("operator_label") or "").strip()
                if _label:
                    operator_name = _label
            except Exception:
                pass

            # === Поддержка ФОТО от оператора ===
            # Если оператор прислал фото — скачиваем из TG, кладём в
            # data/web_uploads/<visitor_id>/, отдаём клиенту как attachment.
            attachment_url = None
            attachment_kind = None
            display_text = text_raw

            if message.photo:
                try:
                    import secrets as _secrets
                    import os as _os
                    # Берём самое большое фото
                    photo_obj = message.photo[-1]
                    # Скачиваем в data/web_uploads/<visitor_id>/
                    upload_dir = _os.path.join(
                        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                        "data", "web_uploads", visitor_id,
                    )
                    _os.makedirs(upload_dir, exist_ok=True)
                    file_id_short = _secrets.token_urlsafe(12)
                    filename = file_id_short + ".jpg"
                    file_path = _os.path.join(upload_dir, filename)

                    await bot.download(photo_obj, destination=file_path)
                    attachment_url = f"/api/chat/file/{visitor_id}/{filename}"
                    attachment_kind = "photo"
                    # Если есть caption — добавим текстом
                    if message.caption:
                        display_text = message.caption
                    else:
                        display_text = ""
                    logger.info(
                        "WEB_CHAT op_photo to {}: сохранил {}",
                        visitor_id, filename,
                    )
                except Exception as e:
                    logger.exception("WEB_CHAT op_photo download failed: {}", e)
                    display_text = "[Не удалось переслать фото — попросите оператора отправить ещё раз]"

            await web_chat_db.add_message(
                visitor_id, "out", display_text,
                sender=operator_name,
                attachment_url=attachment_url,
                attachment_kind=attachment_kind,
            )
            logger.info(
                "WEB_CHAT op_reply to {}: {!r:.50}",
                visitor_id, display_text,
            )

            # [v3.5] Помечаем что оператор вмешался → AI больше не отвечает,
            # status='open' (внутри mark_operator_joined). Также переоткрываем
            # ghost-topic в TG и меняем иконку — оператор активен.
            was_already_joined = False
            try:
                was_already_joined = await web_chat_db.is_operator_joined(visitor_id)
            except Exception:
                pass
            try:
                await web_chat_db.mark_operator_joined(visitor_id)
            except Exception as e:
                logger.warning("mark_operator_joined failed: {}", e)

            # Если оператор только что подключился (раньше был AI-режим) —
            # переоткрываем topic и меняем иконку на 💬 (открытый/активный).
            if not was_already_joined:
                # [v3.5] Проверяем что AI реально отвечал этому визитёру.
                # Если AI не работал — уведомление "AI больше не отвечает"
                # не имеет смысла, не шлём (избегаем спам).
                ai_was_active = False
                try:
                    from admin_web import ai_settings as _ais
                    _ai_name = _ais.get_assistant_name()
                    ai_was_active = await web_chat_db.did_ai_respond_to_visitor(
                        visitor_id, _ai_name,
                    )
                except Exception as e:
                    logger.debug("did_ai_respond_to_visitor failed: {}", e)

                try:
                    await bot.reopen_forum_topic(
                        chat_id=SUPPORT_CHAT_ID,
                        message_thread_id=message.message_thread_id,
                    )
                except Exception as e:
                    if "TOPIC_NOT_MODIFIED" not in str(e):
                        logger.debug("reopen ghost-topic on operator: {}", e)
                try:
                    from app import topic_icons
                    await topic_icons.set_topic_icon(
                        bot, SUPPORT_CHAT_ID,
                        message.message_thread_id, "web",
                    )
                except Exception as e:
                    logger.debug("set icon web on operator: {}", e)
                # [v3.5] Уведомление шлём ТОЛЬКО если AI действительно работал
                if ai_was_active:
                    try:
                        await bot.send_message(
                            chat_id=SUPPORT_CHAT_ID,
                            message_thread_id=message.message_thread_id,
                            text=(
                                "👤 <b>Оператор подключился</b>\n"
                                "<i>AI отключён. Статус тикета: <code>open</code>.\n"
                                "При закрытии тикета AI снова начнёт отвечать.</i>"
                            ),
                            parse_mode="HTML",
                            disable_notification=True,  # [v3.5] тихо
                        )
                    except Exception:
                        pass

            # [v3.5] Подтверждение «✅ Сообщение отправлено клиенту на сайт»
            # с панелью админ-кнопок УБРАНО — оператор только что писал, ему
            # не нужны повторные кнопки после каждого сообщения. Управление
            # тикетом доступно из шапки тикета и из админ-панели.
            return
        return

    # Первый раз в этой "сессии" — шлём заголовок «Ответ поддержки»
    if user_id not in support_reply_cache:
        try:
            await bot.send_message(user_id, T.SUPPORT_REPLY)
        except Exception as e:
            logger.warning(f"Cannot send to {user_id}: {e}")
            return
        support_reply_cache.add(user_id)

    try:
        # [v3.5] Если оператор делает reply на сообщение клиента в группе
        # (forward или зеркало) — берём оригинальный message_id клиента
        # из БД-маппинга tg_message_map. Это надёжнее чем forward_origin,
        # потому что у forward'ов от бота это поле может быть пустым.
        client_reply_to_id = None
        if message.reply_to_message:
            try:
                mapped = await get_client_msg_id(message.reply_to_message.message_id)
                if mapped:
                    mapped_user_id, mapped_msg_id = mapped
                    # Проверяем что reply относится к правильному клиенту
                    if mapped_user_id == user_id:
                        client_reply_to_id = mapped_msg_id
                        logger.info(
                            "support_reply: reply на сообщение клиента {} "
                            "(group_msg={} → client_msg={})",
                            user_id, message.reply_to_message.message_id,
                            mapped_msg_id,
                        )
            except Exception as e:
                logger.debug("support_reply: маппинг reply не найден: {}", e)

        copy_kwargs = {
            "chat_id": user_id,
            "from_chat_id": SUPPORT_CHAT_ID,
            "message_id": message.message_id,
            "disable_notification": True,  # [v3.5] тихо
        }
        if client_reply_to_id:
            # allow_sending_without_reply=True — если клиент удалил оригинал,
            # отправим без привязки, не упадём.
            copy_kwargs["reply_to_message_id"] = client_reply_to_id
            copy_kwargs["allow_sending_without_reply"] = True

        sent = await bot.copy_message(**copy_kwargs)
        # [v3.5] Сохраняем обратный маппинг: client_msg_id (то что клиент
        # увидел у себя) → operator_group_msg_id (что оператор написал в группе).
        # Если клиент сделает reply на этот ответ — найдём оригинал оператора
        # и форварднем в группу с привязкой, чтобы оператор видел reply.
        try:
            await save_operator_msg_map(
                user_id=user_id,
                client_received_msg_id=sent.message_id,
                operator_group_msg_id=message.message_id,
            )
        except Exception as e:
            logger.debug("save_operator_msg_map failed: {}", e)

        # [v3.5] Тихое подтверждение доставки оператору — reply на его
        # сообщение в группе. Чтобы оператор был уверен что клиент получил.
        # Управляется настройкой notify_delivery_ack.
        try:
            from app import bot_settings as _bs
            if _bs.get_bool("notify_delivery_ack"):
                await bot.send_message(
                    chat_id=SUPPORT_CHAT_ID,
                    message_thread_id=message.message_thread_id,
                    text="✅ <i>Доставлено</i>",
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id,
                    allow_sending_without_reply=True,
                    disable_notification=True,
                )
        except Exception as e:
            logger.debug("delivery ack failed: {}", e)
    except Exception as e:
        # [v3.5] Подтверждение НЕдоставки — оператор видит причину, чтобы
        # понять почему клиент не получил (бот заблокирован, аккаунт удалён,
        # лимит сообщений и т.п.).
        err_text = str(e)
        # Переводим типичные ошибки TG в понятный текст для оператора
        err_lc = err_text.lower()
        if "bot was blocked" in err_lc or "blocked by the user" in err_lc:
            reason = "клиент заблокировал бота"
        elif "user is deactivated" in err_lc:
            reason = "аккаунт клиента удалён"
        elif "chat not found" in err_lc:
            reason = "чат с клиентом не найден"
        elif "can't be copied" in err_lc or "cant be copied" in err_lc:
            reason = "сообщение не может быть скопировано (например голосовое из защищённого чата)"
        elif "message to copy not found" in err_lc:
            reason = "оригинальное сообщение не найдено (возможно удалено)"
        elif "too many requests" in err_lc or "flood" in err_lc:
            reason = "слишком много запросов — попробуйте через минуту"
        elif "forbidden" in err_lc:
            reason = "клиент закрыл доступ боту"
        else:
            reason = err_text[:200]

        logger.warning(f"copy_message failed to {user_id}: {e}")
        # [v3.5] Уведомление о недоставке — управляется переключателем
        # notify_undelivered в Настройках бота. Можно отключить если
        # эти сообщения мешают (например клиент часто блокирует бота).
        try:
            from app import bot_settings as _bs
            _notify_undelivered = _bs.get_bool("notify_undelivered", True)
        except Exception:
            _notify_undelivered = True
        if not _notify_undelivered:
            return
        try:
            await bot.send_message(
                chat_id=SUPPORT_CHAT_ID,
                message_thread_id=message.message_thread_id,
                text=(
                    "❌ <b>Не доставлено</b>\n"
                    f"<i>Причина: {reason}</i>"
                ),
                parse_mode="HTML",
                reply_to_message_id=message.message_id,
                allow_sending_without_reply=True,
                disable_notification=True,
            )
        except Exception as e2:
            logger.debug("delivery nack failed: {}", e2)
        return

    # [v3.5] Эскалация в TG-режиме когда оператор впервые отвечает
    # клиенту с которым AI уже общался:
    #  - проверяем was_already_joined (был ли operator_joined=1)
    #  - проверяем что AI ДЕЙСТВИТЕЛЬНО отвечал в этом тикете
    #  - если впервые И AI работал → reopen + иконка 💬 + уведомление
    # Иначе (оператор просто разговаривает без AI) — без шума.
    was_already_joined_tg = False
    try:
        was_already_joined_tg = await is_ticket_operator_joined(user_id)
    except Exception:
        pass
    # Помечаем что оператор подключился к тикету → AI больше не отвечает
    try:
        await mark_ticket_operator_joined(user_id)
    except Exception as e:
        logger.warning("mark_ticket_operator_joined failed: {}", e)

    # Первая операторская реплика — переоткрываем topic и меняем иконку,
    # но уведомление шлём ТОЛЬКО если AI реально отвечал в этом тикете.
    if not was_already_joined_tg:
        # Проверяем что AI был активен в текущем тикете
        ai_was_active = False
        try:
            _current = await get_user_open_ticket(user_id)
            if _current:
                ai_was_active = await did_ai_respond_in_ticket(_current["id"])
        except Exception as e:
            logger.debug("did_ai_respond check failed: {}", e)

        # Reopen и иконку меняем всегда (визуальная отметка что оператор активен)
        try:
            await bot.reopen_forum_topic(
                chat_id=SUPPORT_CHAT_ID,
                message_thread_id=message.message_thread_id,
            )
        except Exception as e:
            if "TOPIC_NOT_MODIFIED" not in str(e):
                logger.debug("TG reopen topic on operator: {}", e)
        try:
            from app import topic_icons
            await topic_icons.set_topic_icon(
                bot, SUPPORT_CHAT_ID,
                message.message_thread_id, "tg",
            )
        except Exception as e:
            logger.debug("TG set icon tg on operator: {}", e)

        # [v3.5] Уведомление шлём ТОЛЬКО если AI действительно работал.
        # Если оператор просто отвечает без AI — не спамим.
        # Управляется настройкой notify_operator_joined.
        if ai_was_active:
            try:
                from app import bot_settings as _bs
                if _bs.get_bool("notify_operator_joined"):
                    await bot.send_message(
                        chat_id=SUPPORT_CHAT_ID,
                        message_thread_id=message.message_thread_id,
                        text=(
                            "👤 <b>Оператор подключился</b>\n"
                            "<i>AI больше не отвечает — клиент общается с вами.</i>"
                        ),
                        parse_mode="HTML",
                        disable_notification=True,
                    )
            except Exception as e:
                logger.debug("TG operator notify: {}", e)
            logger.info(
                "TG операторская эскалация после AI: user={} topic={}",
                user_id, message.message_thread_id,
            )

    # Сохраняем ответ оператора в БД
    try:
        ticket = await get_user_open_ticket(user_id)
        kind, text_for_log = _extract_message_kind_and_text(message)
        operator_name = None
        operator_id = None
        if message.from_user:
            operator_id = message.from_user.id
            operator_name = (
                message.from_user.username
                and f"@{message.from_user.username}"
                or message.from_user.full_name
            )
        await save_message(
            ticket_id=ticket["id"] if ticket else None,
            user_id=user_id,
            topic_id=message.message_thread_id,
            direction="out", kind=kind, text=text_for_log,
            operator_id=operator_id, operator_name=operator_name,
        )
    except Exception as e:
        logger.warning("save_message (out) failed: {}", e)

    # [v3.5] Подтверждение «✅ Сообщение отправлено клиенту» с панелью
    # админ-кнопок УБРАНО — спам после каждого сообщения, не нужен.


async def _handle_admin_command(message: Message):
    """Обработка /ban /unban /close /info /help внутри топика тикета."""
    cmd = message.text.split()[0].lower().split("@")[0]
    topic_id = message.message_thread_id
    user_id, source = await resolve_user_by_topic(topic_id)

    # /help работает даже без user_id — это справка
    if cmd == "/help":
        help_text = (
            "<b>📋 Команды оператора в топике тикета:</b>\n\n"
            "<code>/info</code> — обновить и показать свежие данные клиента из админки\n"
            "<code>/ban</code> — забанить клиента, закрыть тикет\n"
            "<code>/unban</code> — снять бан с клиента\n"
            "<code>/close</code> — закрыть тикет (без бана)\n"
            "<code>/help</code> — показать эту справку\n\n"
            "<i>Команды также доступны через кнопки под шапкой тикета.</i>"
        )
        await message.reply(help_text, parse_mode="HTML")
        return

    if not user_id:
        await message.reply("❗️ Не удалось определить пользователя топика.")
        return

    if cmd == "/ban":
        # [v3.5] Команда /ban может быть отключена настройкой
        try:
            from app import bot_settings as _bs
            if _bs.get_bool("disable_command_ban"):
                await message.reply(
                    "🚫 Команда <code>/ban</code> отключена в настройках бота. "
                    "Если нужно — включите в админ-панели → ⚙️ Настройки бота."
                )
                return
        except Exception:
            pass

        await set_banned(user_id, True)
        await close_ticket_db(topic_id)
        await _cleanup_ai_state_for_user(user_id)  # [v3.5]
        support_reply_cache.discard(user_id)
        # [v3.5] Меняем иконку как в кнопке-callback'е ban_user
        try:
            await _set_topic_icon(topic_id, TOPIC_ICON_BANNED)
        except Exception as e:
            logger.debug("/ban: set_topic_icon failed: {}", e)
        await message.reply(
            f"⛔️ Пользователь <code>{user_id}</code> забанен. "
            f"Тикет закрыт."
        )
        try:
            await bot.send_message(user_id, T.USER_BANNED)
        except Exception:
            pass

    elif cmd == "/unban":
        await set_banned(user_id, False)
        # [v3.5] Возвращаем иконку open — клиент снова может писать
        try:
            await _set_topic_icon(topic_id, TOPIC_ICON_TG_EMOJI)
        except Exception as e:
            logger.debug("/unban: set_topic_icon failed: {}", e)
        await message.reply(
            f"✅ Пользователь <code>{user_id}</code> разбанен."
        )

    elif cmd == "/info":
        # [v3.5] Команда-эквивалент кнопки "🔄 Обновить инфу" — обновляет
        # данные из админки и шлёт свежую шапку в топик с админ-кнопками.
        try:
            admin_panel.get_client().invalidate(user_id)
        except Exception:
            pass
        try:
            info = await admin_panel.build_ticket_info_block(user_id)
        except Exception as e:
            logger.warning("/info: ошибка для {}: {}", user_id, e)
            info = ""

        if not info.strip():
            await message.reply(
                "⚠️ Не удалось получить данные из админки. "
                "Проверь ADMIN_PASSWORD в .env и доступность панели."
            )
            return

        await message.reply(
            f"🔄 <b>Свежие данные клиента (ID: <code>{user_id}</code>):</b>\n"
            f"{info}",
            reply_markup=admin_action_panel(user_id, ADMIN_PANEL_URL),
            parse_mode="HTML",
            disable_notification=True,
        )

    elif cmd == "/close":
        await close_ticket_db(topic_id)
        await _cleanup_ai_state_for_user(user_id)  # [v3.5]
        support_reply_cache.discard(user_id)
        await message.reply("🔒 Тикет закрыт.")
        try:
            await bot.send_message(user_id, T.TICKET_CLOSED)
            await bot.send_message(user_id, T.WELCOME, reply_markup=main_menu())
        except Exception:
            pass


# ============================================================
#  Кнопки в топике тикета
# ============================================================

@dp.callback_query(F.data.in_(["close_ticket", "close_ticket_silent"]))
async def cb_close_ticket(callback: CallbackQuery):
    """
    Закрывает тикет.
    - close_ticket — обычное: клиент получает уведомление «Тикет закрыт»
                      и приветственное меню снова.
    - close_ticket_silent — тихое: НИКАКОГО уведомления клиенту.
                             Только в группе сообщение оператору.
    """
    silent = callback.data == "close_ticket_silent"
    topic_id = callback.message.message_thread_id
    if not topic_id:
        await callback.answer("Только внутри топика", show_alert=True)
        return

    user_id, source = await resolve_user_by_topic(topic_id)

    # [v3.5] Гость без user_id — проверим есть ли visitor у этого топика
    # (анонимный веб-чат). Если есть — закрываем по visitor_id.
    guest_visitor = None
    if not user_id:
        try:
            from app import web_chat_db as _wcdb
            v = await _wcdb.get_visitor_by_topic(topic_id)
            if v:
                guest_visitor = v
                source = "webchat"
        except Exception as e:
            logger.warning("close_ticket: web lookup failed: {}", e)

    if not user_id and not guest_visitor:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    op_handle = f"@{callback.from_user.username}" if callback.from_user.username else str(callback.from_user.id)
    silent_mark = " 🤫" if silent else ""

    if source == "webchat":
        # Веб-чат — мягкое закрытие: сообщение клиенту + архив топика.
        # Сохраняем привязки visitor (user_id + topic_id) чтобы при возврате
        # клиента сработал reopen_forum_topic в send_message.
        # [v3.5] Для гостя без user_id мы уже нашли visitor выше — переиспользуем.
        if guest_visitor:
            visitor = guest_visitor
        else:
            from app import web_chat_db
            visitor = await web_chat_db.get_visitor_by_topic(topic_id)
        if visitor:
            from app import web_chat_db
            visitor_id = visitor["visitor_id"]
            # В тихом режиме сообщение клиенту НЕ шлём
            if not silent:
                await web_chat_db.add_message(
                    visitor_id, "out",
                    "Диалог закрыт оператором. Если возникнут вопросы — напишите снова.",
                    sender="Поддержка",
                )
            # [v3.5] Уведомление о закрытии в топик — управляется
            # переключателем notify_topic_closed в Настройках бота.
            try:
                from app import bot_settings as _bs
                if _bs.get_bool("notify_topic_closed", True):
                    await callback.message.answer(
                        f"🔒 Веб-чат закрыт оператором {op_handle}{silent_mark}"
                    )
            except Exception:
                await callback.message.answer(
                    f"🔒 Веб-чат закрыт оператором {op_handle}{silent_mark}"
                )
            # Меняем иконку на «закрыто»
            await _set_topic_icon(topic_id, TOPIC_ICON_CLOSED)
            try:
                await bot.close_forum_topic(
                    chat_id=SUPPORT_CHAT_ID, message_thread_id=topic_id,
                )
            except Exception as e:
                msg = str(e)
                if "TOPIC_NOT_MODIFIED" not in msg:
                    logger.warning("close_forum_topic failed: {}", e)
            # [v3.5] Сбрасываем AI-состояние визитёра, но topic_id ОСТАВЛЯЕМ —
            # модель «1 клиент = 1 вечный тикет». При возврате клиента AI
            # снова начнёт отвечать в ТОТ ЖЕ закрытый topic (бот имеет
            # право писать в закрытые топики).
            #
            # Раньше тут был detach_topic — он обнулял topic_id, что приводило
            # к созданию НОВОГО топика при следующем сообщении (история в TG
            # терялась). УБРАНО.
            try:
                await web_chat_db.reset_operator_joined(visitor_id)
                from app import ai_assistant
                await ai_assistant.clear_history(visitor_id)
            except Exception as e:
                logger.warning("webchat close: AI cleanup failed: {}", e)
        await callback.answer("Веб-чат закрыт" + (" (тихо)" if silent else ""))
        return

    # Обычный TG-тикет
    await close_ticket_db(topic_id)
    await _cleanup_ai_state_for_user(user_id)  # [v3.5]
    support_reply_cache.discard(user_id)

    # [v3.5] Уведомление о закрытии в топик — управляется
    # переключателем notify_topic_closed в Настройках бота.
    try:
        from app import bot_settings as _bs
        if _bs.get_bool("notify_topic_closed", True):
            await callback.message.answer(
                f"🔒 Тикет закрыт оператором {op_handle}{silent_mark}"
            )
    except Exception:
        await callback.message.answer(
            f"🔒 Тикет закрыт оператором {op_handle}{silent_mark}"
        )
    # Меняем иконку топика на «✅ закрыт»
    await _set_topic_icon(topic_id, TOPIC_ICON_CLOSED)

    # Шлём уведомление клиенту только если НЕ тихий режим
    if not silent:
        try:
            await bot.send_message(user_id, T.TICKET_CLOSED)
            await bot.send_message(user_id, T.WELCOME, reply_markup=main_menu())
        except Exception as e:
            # Если клиент забанил бота — это нормально, не предупреждаем громко
            msg = str(e)
            if "bot was blocked" in msg or "Forbidden" in msg:
                logger.debug("Cannot notify {} about close (заблокировал бота): {}", user_id, e)
            else:
                logger.warning("Cannot notify {} about close: {}", user_id, e)

    await callback.answer("Закрыто" + (" (тихо)" if silent else ""))


@dp.callback_query(F.data == "ban_user")
async def cb_ban_user(callback: CallbackQuery):
    topic_id = callback.message.message_thread_id
    if not topic_id:
        await callback.answer("Только внутри топика", show_alert=True)
        return

    # [v3.5] Кнопка Бан отключаема в настройках
    try:
        from app import bot_settings as _bs
        if _bs.get_bool("disable_command_ban"):
            await callback.answer(
                "Бан отключён в настройках. Включите в админ-панели → ⚙️ Настройки бота.",
                show_alert=True,
            )
            return
    except Exception:
        pass

    user_id, source = await resolve_user_by_topic(topic_id)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    await set_banned(user_id, True)
    await close_ticket_db(topic_id)
    await _cleanup_ai_state_for_user(user_id)  # [v3.5]
    support_reply_cache.discard(user_id)

    await callback.message.answer(
        f"⛔️ Пользователь <code>{user_id}</code> забанен. Тикет закрыт."
    )
    # Иконка топика → 🚫 «забанен»
    await _set_topic_icon(topic_id, TOPIC_ICON_BANNED)
    try:
        await bot.send_message(user_id, T.USER_BANNED)
    except Exception:
        pass

    await callback.answer("Забанен")


# ============================================================
#  БЫСТРЫЕ ОТВЕТЫ В ТОПИКЕ
# ============================================================

@dp.callback_query(F.data == "admin_quick")
async def cb_admin_quick(callback: CallbackQuery):
    if not callback.message.message_thread_id:
        await callback.answer("Только в топике тикета", show_alert=True)
        return
    await callback.message.answer(
        "📋 <b>Быстрые ответы</b>\nВыберите шаблон для отправки пользователю:",
        reply_markup=admin_quick_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("refresh_info:"))
async def cb_refresh_info(callback: CallbackQuery):
    """Сбрасывает кэш админки для пользователя и присылает свежий блок в топик."""
    if not callback.message.message_thread_id:
        await callback.answer("Только в топике тикета", show_alert=True)
        return

    try:
        user_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный ID", show_alert=True)
        return

    await callback.answer("Запрашиваю свежие данные…")

    admin_panel.get_client().invalidate(user_id)

    try:
        info = await admin_panel.build_ticket_info_block(user_id)
    except Exception as e:
        logger.warning("refresh_info: ошибка для {}: {}", user_id, e)
        info = ""

    if not info.strip():
        await callback.message.answer(
            "⚠️ Не удалось получить данные из админки. "
            "Проверь ADMIN_PASSWORD в .env и доступность панели."
        )
        return

    await callback.message.answer(
        f"🔄 <b>Свежие данные клиента (ID: <code>{user_id}</code>):</b>\n"
        f"{info}",
        reply_markup=admin_action_panel(user_id, ADMIN_PANEL_URL),
    )


# ============================================================
#  СБРОС ПОДПИСКИ (опасное действие, через ПИН-подтверждение)
# ============================================================

@dp.callback_query(F.data.startswith("revoke_sub:"))
async def cb_revoke_sub_request(callback: CallbackQuery):
    """
    Шаг 1: оператор нажал «Сброс подписки». Генерируем ПИН и просим
    оператора ввести его реплаем в течение 60 секунд.
    """
    if not callback.message.message_thread_id:
        await callback.answer("Только в топике тикета", show_alert=True)
        return

    if callback.message.chat.id != SUPPORT_CHAT_ID:
        await callback.answer("Команда доступна только в чате поддержки", show_alert=True)
        return

    try:
        target_user_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный ID", show_alert=True)
        return

    operator_id = callback.from_user.id
    operator_name = (
        callback.from_user.username
        and f"@{callback.from_user.username}"
        or callback.from_user.full_name
    )

    import secrets
    pin = f"{secrets.randbelow(10000):04d}"
    expires_at = _time.time() + REVOKE_PIN_TTL

    prompt = await callback.message.answer(
        f"⚠️ <b>СБРОС ПОДПИСКИ</b>\n\n"
        f"Оператор: {operator_name}\n"
        f"Клиент: <code>{target_user_id}</code>\n\n"
        f"Это действие:\n"
        f"• сгенерирует новые UUID и ключи\n"
        f"• отключит клиента со ВСЕХ устройств\n"
        f"• обнулит существующие VLESS-ссылки\n\n"
        f"Если у клиента <b>безлимитный тариф</b> — после сброса нужно "
        f"вручную в админке: сменить сквад, обнулить трафик, поставить "
        f"лимит 0 в Remnawave.\n\n"
        f"🔢 Чтобы подтвердить, ответь <b>реплаем</b> на это сообщение "
        f"кодом: <code>{pin}</code>\n"
        f"У тебя {REVOKE_PIN_TTL} секунд."
    )

    pending_revokes[(callback.message.chat.id, callback.message.message_thread_id, operator_id)] = {
        "target_user_id": target_user_id,
        "pin": pin,
        "expires_at": expires_at,
        "prompt_message_id": prompt.message_id,
    }

    await callback.answer("Введи ПИН реплаем на сообщение выше")


async def _process_revoke_pin(message: Message, key: tuple, pending: dict, pin_text: str):
    """
    Шаг 2: оператор реплайнул 4 цифрами на промпт с ПИНом.
    Если ПИН верный и не протух — делаем сброс.
    """
    # Проверяем TTL
    if _time.time() > pending["expires_at"]:
        pending_revokes.pop(key, None)
        await message.reply(
            "⏰ Время ввода ПИНа истекло. Нажми «🔁 Сброс подписки» заново."
        )
        return

    # Проверяем сам ПИН
    if pin_text != pending["pin"]:
        # ПИН неверный — даём ещё попытки в пределах TTL
        await message.reply("❌ Неверный код. Попробуй ещё раз.")
        return

    # ПИН верный — забираем pending и выполняем
    pending_revokes.pop(key, None)
    target_user_id = pending["target_user_id"]
    operator_id = message.from_user.id
    operator_name = (
        message.from_user.username
        and f"@{message.from_user.username}"
        or message.from_user.full_name
    )

    status_msg = await message.reply(
        f"⏳ Выполняю сброс подписки для <code>{target_user_id}</code>…"
    )

    logger.warning(
        "REVOKE_SUBSCRIPTION: оператор={} ({}) клиент={}",
        operator_name, operator_id, target_user_id,
    )

    try:
        success, result_msg = await admin_panel.get_client().revoke_subscription(target_user_id)
    except Exception as e:
        logger.exception("revoke_subscription исключение для {}: {}", target_user_id, e)
        success = False
        result_msg = f"Неожиданная ошибка: {e}"

    if success:
        await status_msg.edit_text(
            f"✅ <b>Подписка сброшена</b>\n\n"
            f"Клиент: <code>{target_user_id}</code>\n"
            f"Оператор: {operator_name}\n\n"
            f"{result_msg}\n\n"
            f"⚠️ Если у клиента был безлимитный тариф — не забудь "
            f"вручную в админке сменить сквад / обнулить трафик / "
            f"поставить лимит 0 в Remnawave.",
            reply_markup=admin_action_panel(target_user_id, ADMIN_PANEL_URL),
        )
        logger.warning(
            "REVOKE_SUBSCRIPTION OK: оператор={} клиент={}",
            operator_id, target_user_id,
        )
    else:
        await status_msg.edit_text(
            f"❌ <b>Сброс не выполнен</b>\n\n"
            f"Клиент: <code>{target_user_id}</code>\n"
            f"Причина: {result_msg}\n\n"
            f"Можно попробовать ещё раз или сбросить вручную в админке.",
            reply_markup=admin_action_panel(target_user_id, ADMIN_PANEL_URL),
        )
        logger.error(
            "REVOKE_SUBSCRIPTION FAIL: оператор={} клиент={} причина={}",
            operator_id, target_user_id, result_msg,
        )


# ============================================================
#  ПОДМЕНЮ «🛠 Админка» — открытие
# ============================================================

@dp.callback_query(F.data.startswith("admin_panel_open:"))
async def cb_admin_panel_open(callback: CallbackQuery):
    """Открывает подробный блок инфы о клиенте + кнопки изменяющих действий."""
    if not callback.message.message_thread_id:
        await callback.answer("Только в топике тикета", show_alert=True)
        return

    try:
        user_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный ID", show_alert=True)
        return

    await callback.answer("Собираю данные…")

    # Инвалидируем кэш, чтобы показать всегда свежее
    admin_panel.get_client().invalidate(user_id)

    try:
        text = await admin_panel.build_admin_panel_view(user_id)
    except Exception as e:
        logger.exception("admin_panel_open: ошибка для {}: {}", user_id, e)
        text = f"⚠️ Ошибка получения данных: {e}"

    await callback.message.answer(
        text,
        reply_markup=admin_panel_keyboard(user_id, ADMIN_PANEL_URL),
        disable_web_page_preview=True,
    )


# ============================================================
#  ПРОДЛИТЬ / УМЕНЬШИТЬ — запрос ввода дней
# ============================================================

async def _request_days_input(
    callback: CallbackQuery,
    action: str,  # "extend" | "reduce"
    target_user_id: int,
) -> None:
    """Общая логика для extend/reduce: просит у оператора число дней реплаем."""
    if action == "extend":
        title = "➕ <b>ПРОДЛЕНИЕ ПОДПИСКИ</b>"
        instruction = (
            "На сколько дней продлить? Ответь <b>реплаем</b> на это "
            "сообщение целым числом (например: <code>30</code>)."
        )
        extra = "После подтверждения клиент получит уведомление в Telegram."
    else:
        title = "➖ <b>УМЕНЬШЕНИЕ ПОДПИСКИ</b>"
        instruction = (
            "На сколько дней уменьшить? Ответь <b>реплаем</b> на это "
            "сообщение целым числом (например: <code>7</code>)."
        )
        extra = "⚠️ Клиент НЕ получит уведомление."

    operator_name = (
        callback.from_user.username
        and f"@{callback.from_user.username}"
        or callback.from_user.full_name
    )

    prompt = await callback.message.answer(
        f"{title}\n\n"
        f"Оператор: {operator_name}\n"
        f"Клиент: <code>{target_user_id}</code>\n\n"
        f"{instruction}\n\n"
        f"<i>{extra}</i>\n"
        f"<i>Таймаут: {DAYS_INPUT_TTL} сек.</i>"
    )

    pending_days_input[(callback.message.chat.id, callback.message.message_thread_id, callback.from_user.id)] = {
        "action": action,
        "target_user_id": target_user_id,
        "expires_at": _time.time() + DAYS_INPUT_TTL,
        "prompt_message_id": prompt.message_id,
    }

    await callback.answer("Введи число дней реплаем")


@dp.callback_query(F.data.startswith("sub_extend:"))
async def cb_sub_extend(callback: CallbackQuery):
    if not callback.message.message_thread_id:
        await callback.answer("Только в топике тикета", show_alert=True)
        return
    try:
        user_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный ID", show_alert=True)
        return
    await _request_days_input(callback, "extend", user_id)


@dp.callback_query(F.data.startswith("sub_reduce:"))
async def cb_sub_reduce(callback: CallbackQuery):
    if not callback.message.message_thread_id:
        await callback.answer("Только в топике тикета", show_alert=True)
        return
    try:
        user_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный ID", show_alert=True)
        return
    await _request_days_input(callback, "reduce", user_id)


async def _process_days_input(
    message: Message,
    key: tuple,
    pending: dict,
    days: int,
):
    """Оператор ввёл число дней реплаем — показываем подтверждение."""
    # TTL
    if _time.time() > pending["expires_at"]:
        pending_days_input.pop(key, None)
        await message.reply(
            "⏰ Время ввода истекло. Открой «🛠 Админка» и попробуй снова."
        )
        return

    if days < 1 or days > 3650:
        await message.reply(
            "❌ Число должно быть от 1 до 3650. Попробуй ещё раз."
        )
        return

    # Сразу убираем pending — подтверждение пойдёт через отдельную кнопку
    pending_days_input.pop(key, None)

    action = pending["action"]
    target_user_id = pending["target_user_id"]

    if action == "extend":
        await message.reply(
            f"➕ <b>Подтвердить продление</b>\n\n"
            f"Клиент: <code>{target_user_id}</code>\n"
            f"Дней: <b>+{days}</b>\n"
            f"Клиенту придёт уведомление: <b>да</b>\n\n"
            f"Жми кнопку для подтверждения.",
            reply_markup=confirm_extend_keyboard(target_user_id, days),
        )
    else:
        await message.reply(
            f"➖ <b>Подтвердить уменьшение</b>\n\n"
            f"Клиент: <code>{target_user_id}</code>\n"
            f"Дней: <b>−{days}</b>\n"
            f"Клиент уведомление НЕ получит.\n\n"
            f"⚠️ Подписка будет сокращена. Жми кнопку для подтверждения.",
            reply_markup=confirm_reduce_keyboard(target_user_id, days),
        )


# ============================================================
#  ИСПОЛНЕНИЕ extend / reduce (после подтверждения кнопкой)
# ============================================================

@dp.callback_query(F.data.startswith("do_extend:"))
async def cb_do_extend(callback: CallbackQuery):
    if not callback.message.message_thread_id:
        await callback.answer("Только в топике тикета", show_alert=True)
        return
    try:
        _, uid, days = callback.data.split(":")
        target_user_id = int(uid)
        days_n = int(days)
    except (ValueError, IndexError):
        await callback.answer("Некорректные данные", show_alert=True)
        return

    operator_id = callback.from_user.id
    operator_name = (
        callback.from_user.username
        and f"@{callback.from_user.username}"
        or callback.from_user.full_name
    )

    await callback.answer("Выполняю…")

    logger.warning(
        "SUB_EXTEND: оператор={} ({}) клиент={} дней=+{}",
        operator_name, operator_id, target_user_id, days_n,
    )

    try:
        success, msg = await admin_panel.get_client().extend_subscription(
            target_user_id, days_n, notify_user=True,
        )
    except Exception as e:
        logger.exception("extend исключение для {}: {}", target_user_id, e)
        success = False
        msg = f"Неожиданная ошибка: {e}"

    if success:
        await callback.message.edit_text(
            f"✅ <b>Подписка продлена</b>\n\n"
            f"Клиент: <code>{target_user_id}</code>\n"
            f"Оператор: {operator_name}\n"
            f"+{days_n} дн.\n\n"
            f"{msg}\n\n"
            f"📩 Клиенту отправлено уведомление в Telegram.",
            reply_markup=admin_action_panel(target_user_id, ADMIN_PANEL_URL),
        )
        logger.warning(
            "SUB_EXTEND OK: оператор={} клиент={} дней=+{}",
            operator_id, target_user_id, days_n,
        )
    else:
        await callback.message.edit_text(
            f"❌ <b>Продление не выполнено</b>\n\n"
            f"Клиент: <code>{target_user_id}</code>\n"
            f"Причина: {msg}",
            reply_markup=admin_action_panel(target_user_id, ADMIN_PANEL_URL),
        )
        logger.error(
            "SUB_EXTEND FAIL: оператор={} клиент={} причина={}",
            operator_id, target_user_id, msg,
        )


@dp.callback_query(F.data.startswith("do_reduce:"))
async def cb_do_reduce(callback: CallbackQuery):
    if not callback.message.message_thread_id:
        await callback.answer("Только в топике тикета", show_alert=True)
        return
    try:
        _, uid, days = callback.data.split(":")
        target_user_id = int(uid)
        days_n = int(days)
    except (ValueError, IndexError):
        await callback.answer("Некорректные данные", show_alert=True)
        return

    operator_id = callback.from_user.id
    operator_name = (
        callback.from_user.username
        and f"@{callback.from_user.username}"
        or callback.from_user.full_name
    )

    await callback.answer("Выполняю…")

    logger.warning(
        "SUB_REDUCE: оператор={} ({}) клиент={} дней=-{}",
        operator_name, operator_id, target_user_id, days_n,
    )

    try:
        success, msg = await admin_panel.get_client().reduce_subscription(
            target_user_id, days_n,
        )
    except Exception as e:
        logger.exception("reduce исключение для {}: {}", target_user_id, e)
        success = False
        msg = f"Неожиданная ошибка: {e}"

    if success:
        await callback.message.edit_text(
            f"⚠️ <b>Подписка уменьшена</b>\n\n"
            f"Клиент: <code>{target_user_id}</code>\n"
            f"Оператор: {operator_name}\n"
            f"−{days_n} дн.\n\n"
            f"{msg}\n\n"
            f"<i>Клиент уведомление не получил.</i>",
            reply_markup=admin_action_panel(target_user_id, ADMIN_PANEL_URL),
        )
        logger.warning(
            "SUB_REDUCE OK: оператор={} клиент={} дней=-{}",
            operator_id, target_user_id, days_n,
        )
    else:
        await callback.message.edit_text(
            f"❌ <b>Уменьшение не выполнено</b>\n\n"
            f"Клиент: <code>{target_user_id}</code>\n"
            f"Причина: {msg}",
            reply_markup=admin_action_panel(target_user_id, ADMIN_PANEL_URL),
        )
        logger.error(
            "SUB_REDUCE FAIL: оператор={} клиент={} причина={}",
            operator_id, target_user_id, msg,
        )


@dp.callback_query(F.data == "admin_hide")
async def cb_admin_hide(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "admin_main")
async def cb_admin_main(callback: CallbackQuery):
    """Возврат к панели действий оператора: Открыть в админке / Закрыть / Бан / Авто-ответы."""
    topic_id = callback.message.message_thread_id
    if not topic_id:
        await callback.answer("Только в топике тикета", show_alert=True)
        return

    user_id, source = await resolve_user_by_topic(topic_id)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    # Удаляем сообщение, из которого пришли (меню шаблонов / предпросмотр)
    try:
        await callback.message.delete()
    except Exception:
        pass

    await bot.send_message(
        SUPPORT_CHAT_ID,
        "🛠 <b>Панель оператора</b>",
        message_thread_id=topic_id,
        reply_markup=admin_action_panel(user_id, ADMIN_PANEL_URL),
        disable_notification=True,  # [v3.5] тихо
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("qa_"))
async def cb_qa_preview(callback: CallbackQuery):
    """Шаг 1: предпросмотр шаблонного ответа в топике."""
    topic_id = callback.message.message_thread_id
    if not topic_id:
        await callback.answer("Только в топике тикета", show_alert=True)
        return

    # Берём текст ответа: сперва из БД (через admin_quick_answer_text),
    # потом фолбэк на ADMIN_ANSWERS из кода
    from app.keyboards_proxy import admin_quick_answer_text, admin_quick_answer_photos
    answer_text = admin_quick_answer_text(callback.data) or ADMIN_ANSWERS.get(callback.data)
    if not answer_text:
        await callback.answer("Ответ не найден", show_alert=True)
        return

    user_id, source = await resolve_user_by_topic(topic_id)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    # Собираем список существующих файлов фоток
    photo_paths: list[str] = []
    for pname in admin_quick_answer_photos(callback.data):
        candidate = os.path.join(QA_PHOTOS_DIR, pname)
        if os.path.exists(candidate) and os.path.isfile(candidate):
            photo_paths.append(candidate)
        else:
            logger.warning(
                "cb_qa_preview: фото {} указано в БД, но файла нет на диске",
                candidate,
            )

    header = (
        f"📝 <b>Предпросмотр ответа</b>\n"
        f"<b>Получатель:</b> <code>{user_id}</code>"
    )
    if photo_paths:
        header += f"\n📷 <i>С фото: {len(photo_paths)} шт.</i>"
    header += f"\n\n{answer_text}"

    # Несколько фото → шлём media group + текст и кнопку отдельным сообщением
    if len(photo_paths) >= 2:
        try:
            from aiogram.types import InputMediaPhoto
            media = []
            for i, p in enumerate(photo_paths[:10]):
                if i == 0:
                    short = "📷 Превью фото для авто-ответа"
                    media.append(InputMediaPhoto(
                        media=FSInputFile(p),
                        caption=short,
                    ))
                else:
                    media.append(InputMediaPhoto(media=FSInputFile(p)))
            await callback.message.answer_media_group(media=media)
            await callback.message.answer(
                header,
                reply_markup=confirm_send_keyboard(user_id, callback.data),
            )
        except Exception as e:
            logger.warning("cb_qa_preview: media group failed: {}", e)
            await callback.message.answer(
                header,
                reply_markup=confirm_send_keyboard(user_id, callback.data),
            )
    # Одно фото → caption + кнопка под ним
    elif len(photo_paths) == 1:
        try:
            photo_file = FSInputFile(photo_paths[0])
            if len(header) <= 1024:
                await callback.message.answer_photo(
                    photo=photo_file,
                    caption=header,
                    reply_markup=confirm_send_keyboard(user_id, callback.data),
                )
            else:
                await callback.message.answer_photo(
                    photo=photo_file,
                    caption="📷 Превью фото для авто-ответа",
                )
                await callback.message.answer(
                    header,
                    reply_markup=confirm_send_keyboard(user_id, callback.data),
                )
        except Exception as e:
            logger.warning("cb_qa_preview: не смог отправить фото-превью: {}", e)
            await callback.message.answer(
                header,
                reply_markup=confirm_send_keyboard(user_id, callback.data),
            )
    # Без фото
    else:
        await callback.message.answer(
            header,
            reply_markup=confirm_send_keyboard(user_id, callback.data),
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("send|"))
async def cb_qa_send(callback: CallbackQuery):
    """Шаг 2: подтверждение отправки шаблонного ответа пользователю."""
    try:
        _, user_id_str, answer_key = callback.data.split("|", 2)
        user_id = int(user_id_str)
    except ValueError:
        await callback.answer("Ошибка формата", show_alert=True)
        return

    from app.keyboards_proxy import admin_quick_answer_text, admin_quick_answer_photos
    answer_text = admin_quick_answer_text(answer_key) or ADMIN_ANSWERS.get(answer_key)
    if not answer_text:
        await callback.answer("Ответ не найден", show_alert=True)
        return

    # Собираем список существующих файлов фоток (макс. 10 — лимит media group)
    photo_paths: list[str] = []
    for pname in admin_quick_answer_photos(answer_key)[:10]:
        candidate = os.path.join(QA_PHOTOS_DIR, pname)
        if os.path.exists(candidate) and os.path.isfile(candidate):
            photo_paths.append(candidate)
        else:
            logger.warning(
                "cb_qa_send: фото {} указано в БД, но файла нет — пропускаю",
                candidate,
            )

    # Определяем источник топика — TG-тикет или веб-чат с сайта
    topic_id = callback.message.message_thread_id
    is_webchat = False
    web_visitor = None
    if topic_id:
        try:
            from app import web_chat_db
            web_visitor = await web_chat_db.get_visitor_by_topic(topic_id)
            if web_visitor:
                is_webchat = True
        except Exception as e:
            logger.warning("cb_qa_send: webchat lookup failed: {}", e)

    try:
        if is_webchat and web_visitor:
            # === Веб-чат: отправляем в виджет, а не в личку TG ===
            visitor_id = web_visitor["visitor_id"]
            operator_name = (
                callback.from_user.full_name
                if callback.from_user else "Оператор"
            )

            # Копируем все фото из QA в директорию визитёра и записываем
            # отдельные сообщения с attachment_url. Первое — с текстом.
            photo_urls: list[str] = []
            if photo_paths:
                try:
                    import secrets
                    import shutil
                    from pathlib import Path
                    uploads_dir = Path("/app/data/web_uploads") / visitor_id
                    uploads_dir.mkdir(parents=True, exist_ok=True)
                    for src_path in photo_paths:
                        src_name = os.path.basename(src_path)
                        ext = (src_name.rsplit(".", 1)[-1].lower()
                               if "." in src_name else "jpg")
                        fname = f"{secrets.token_hex(8)}.{ext}"
                        fpath = uploads_dir / fname
                        shutil.copyfile(src_path, str(fpath))
                        photo_urls.append(f"/api/chat/file/{visitor_id}/{fname}")
                    logger.info(
                        "WEB_CHAT auto-answer with {} photos: visitor={} key={}",
                        len(photo_urls), visitor_id, answer_key,
                    )
                except Exception as e:
                    logger.warning(
                        "cb_qa_send: не смог скопировать QA-фото: {}", e,
                    )

            # Записываем в БД: первое фото с текстом, остальные без текста.
            # Если фото нет — текст одним сообщением.
            if photo_urls:
                first = True
                for purl in photo_urls:
                    await web_chat_db.add_message(
                        visitor_id, "out",
                        answer_text if first else "",
                        sender=operator_name,
                        attachment_url=purl,
                        attachment_kind="photo",
                    )
                    first = False
            else:
                await web_chat_db.add_message(
                    visitor_id, "out", answer_text,
                    sender=operator_name,
                )

            logger.info(
                "WEB_CHAT auto-answer to visitor {}: {} (photos={})",
                visitor_id, answer_key, len(photo_urls),
            )
            confirm_text = (
                f"✅ Авто-ответ + {len(photo_urls)} фото отправлены клиенту на сайт"
                if photo_urls else
                "✅ Авто-ответ отправлен клиенту на сайт"
            )
            await callback.message.answer(confirm_text)
            await bot.send_message(
                SUPPORT_CHAT_ID,
                confirm_text,
                message_thread_id=topic_id,
                reply_markup=admin_action_panel(user_id, ADMIN_PANEL_URL),
                disable_notification=True,  # [v3.5] тихо
            )
            await callback.answer("Отправлено в виджет")
            return

        # === Обычный TG-тикет: отправляем в личку клиенту ===
        # Заголовок «Ответ поддержки» — если ещё не слали в этой сессии
        if user_id not in support_reply_cache:
            await bot.send_message(user_id, T.SUPPORT_REPLY)
            support_reply_cache.add(user_id)

        # Несколько фото → media group, одно → sendPhoto, без → sendMessage
        if len(photo_paths) >= 2:
            from aiogram.types import InputMediaPhoto
            media = []
            for i, p in enumerate(photo_paths):
                if i == 0 and len(answer_text) <= 1024:
                    media.append(InputMediaPhoto(
                        media=FSInputFile(p),
                        caption=answer_text,
                    ))
                else:
                    media.append(InputMediaPhoto(media=FSInputFile(p)))
            await bot.send_media_group(user_id, media=media)
            # Если caption не помещался — отправим текст отдельно
            if len(answer_text) > 1024:
                await bot.send_message(user_id, answer_text)
        elif len(photo_paths) == 1:
            photo_file = FSInputFile(photo_paths[0])
            if len(answer_text) <= 1024:
                await bot.send_photo(
                    user_id, photo=photo_file, caption=answer_text,
                )
            else:
                await bot.send_photo(user_id, photo=photo_file)
                await bot.send_message(user_id, answer_text)
        else:
            await bot.send_message(user_id, answer_text)

        photo_suffix = ""
        if len(photo_paths) == 1:
            photo_suffix = " (с фото)"
        elif len(photo_paths) >= 2:
            photo_suffix = f" (с {len(photo_paths)} фото)"
        confirm_msg = f"✅ Отправлено пользователю <code>{user_id}</code>" + photo_suffix
        await callback.message.answer(confirm_msg)
        # Плавающая панель действий
        if topic_id:
            topic_suffix = ""
            if len(photo_paths) == 1:
                topic_suffix = " с фото"
            elif len(photo_paths) >= 2:
                topic_suffix = f" с {len(photo_paths)} фото"
            await bot.send_message(
                SUPPORT_CHAT_ID,
                "✅ Авто-ответ отправлен клиенту" + topic_suffix,
                message_thread_id=topic_id,
                reply_markup=admin_action_panel(user_id, ADMIN_PANEL_URL),
                disable_notification=True,  # [v3.5] тихо
            )

        # Сохраняем авто-ответ в историю
        try:
            ticket = await get_user_open_ticket(user_id)
            operator_id = callback.from_user.id if callback.from_user else None
            operator_name = (
                callback.from_user.username
                and f"@{callback.from_user.username}"
                or (callback.from_user.full_name if callback.from_user else None)
            )
            await save_message(
                ticket_id=ticket["id"] if ticket else None,
                user_id=user_id,
                topic_id=topic_id,
                direction="out", kind="text",
                text=f"[авто-ответ: {answer_key}]\n{answer_text}",
                operator_id=operator_id, operator_name=operator_name,
            )
        except Exception as e:
            logger.warning("save_message (qa) failed: {}", e)

        await callback.answer("Отправлено")
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {e}")
        await callback.answer("Ошибка", show_alert=True)


# ============================================================
#  [v3.5] УНИВЕРСАЛЬНЫЙ HANDLER ДЛЯ ДИНАМИЧЕСКИХ КНОПОК
#  Регистрируется ПОСЛЕДНИМ — ловит callback, который не обработали
#  статические handlers выше. Смотрит в content_buttons:
#   - submenu_name → открывает указанное меню (даже кастомное, динамически)
#   - response_text → шлёт этот текст клиенту
#  Если оба пусты — игнор (можно показать заглушку).
# ============================================================

@dp.callback_query()
async def cb_dynamic_button(callback: CallbackQuery, state: FSMContext):
    """[v3.5] Catch-all: динамические кнопки, созданные через админку.

    Срабатывает только если ни один static handler выше не обработал
    callback (aiogram использует first-match роутинг).
    """
    cb_data = callback.data or ""
    # Без панического падения — просто ищем в content_buttons
    try:
        import aiosqlite as _aio
        from app.content_db import DB_PATH as _CDB
        async with _aio.connect(_CDB) as db:
            db.row_factory = _aio.Row
            try:
                cur = await db.execute(
                    "SELECT response_text, submenu_name FROM content_buttons "
                    "WHERE value = ? LIMIT 1",
                    (cb_data,),
                )
                row = await cur.fetchone()
            except Exception:
                row = None
    except Exception as e:
        logger.warning("cb_dynamic_button: DB read failed: {}", e)
        await callback.answer()
        return

    if not row:
        # Кнопка не найдена в БД — значит callback статический, но handler
        # для него не зарегистрирован. Тихо отвечаем (не ругаемся).
        logger.debug("cb_dynamic_button: callback {!r} нет в БД", cb_data)
        await callback.answer()
        return

    submenu = row["submenu_name"]
    text_resp = row["response_text"]

    # Если есть подменю — открываем его (приоритет выше чем текст)
    if submenu:
        from app import content_cache
        sub_buttons = content_cache.get_menu(submenu)
        if not sub_buttons:
            await callback.answer(
                f"Подменю «{submenu}» не настроено", show_alert=True,
            )
            return
        # Строим клавиатуру для подменю
        from app.keyboards_proxy import _build_markup
        kb = _build_markup(sub_buttons)
        # Заголовок подменю — пробуем найти текст для меню в текстах,
        # иначе используем имя меню
        sub_title = None
        try:
            from app import texts_proxy
            sub_title = await texts_proxy.get_async(f"{submenu.upper()}_TITLE")
        except Exception:
            pass
        if not sub_title:
            sub_title = f"📁 {submenu.replace('_', ' ').title()}"
        try:
            await callback.message.edit_text(
                sub_title, parse_mode="HTML", reply_markup=kb,
            )
        except Exception:
            # Если edit не получился (например сообщение с фото) — отправим новое
            await callback.message.answer(
                sub_title, parse_mode="HTML", reply_markup=kb,
            )
        await callback.answer()
        return

    # Иначе — шлём текст-ответ
    if text_resp:
        try:
            await callback.message.answer(text_resp, parse_mode="HTML")
        except Exception:
            # Если HTML невалиден — отправим без parse_mode
            await callback.message.answer(text_resp)
        await callback.answer()
        return

    # Ни submenu ни text — кнопка пустая (наверное забыли настроить)
    await callback.answer(
        "Эта кнопка ещё не настроена", show_alert=True,
    )


# ============================================================
#  ПОЛЬЗОВАТЕЛЬ → ОПЕРАТОР (продолжение диалога)
# ============================================================

@dp.message(F.chat.type == "private")
async def user_to_support(message: Message, state: FSMContext):
    user_id = message.from_user.id

    # [v3.5] CATCH-ALL ДИАГНОСТИКА — пишем в лог КАЖДОЕ сообщение клиента
    # которое сюда дошло. Если этой строки нет в логах при обращении
    # клиента — значит сообщение перехватывает другой хэндлер или есть
    # проблема со state.
    current_state = await state.get_state() if state else None
    logger.warning(
        "📩 USER_MSG user={} state={!r} text={!r}",
        user_id, current_state,
        (message.text or message.caption or f"<{message.content_type}>")[:80],
    )

    if await is_banned(user_id):
        await message.answer(T.USER_BANNED)
        return

    ticket = await get_user_open_ticket(user_id)
    # [v3.5] ДИАГНОСТИКА: видим что произошло с тикетом клиента
    logger.info(
        "user_to_support: user={} ticket={!r}",
        user_id, ticket,
    )
    if not ticket:
        # Нет открытого тикета — пробуем AI.
        ai_answer = await try_ai_for_user(message)
        if ai_answer:
            return  # AI справился, тикет создавать не нужно

        # [v3.5] AI эскалировал (или AI выключен). Создаём тикет автоматически —
        # клиент уже получил текст от AI «перевожу на техника», нет смысла
        # заставлять его нажимать «Связаться» отдельно.
        logger.info("user_to_support: вызываю _create_support_ticket для user={}", user_id)
        new_ticket = await _create_support_ticket(message)
        if new_ticket is None:
            # Rate-limit или ошибка БД — даём меню как fallback
            await message.answer(
                "🤖 Чтобы получить помощь — воспользуйтесь меню или "
                "нажмите «Связаться с поддержкой».",
                reply_markup=main_menu(),
            )
            return
        # Клиент уже видел текст AI про эскалацию (или ему ничего не пришло).
        # Подтверждение что тикет создан добавлять не будем — техник скоро ответит.
        return

    # У клиента уже есть открытый тикет.
    # Если оператор хоть раз ответил — AI не вмешивается, идём в форвард.
    operator_active = False
    try:
        operator_active = await is_ticket_operator_joined(user_id)
    except Exception as e:
        logger.warning("is_ticket_operator_joined failed: {}", e)

    if not operator_active:
        # Оператор ещё не подключался — пробуем AI ответить ДО форварда.
        ai_answer = await try_ai_for_user(message)
        if ai_answer:
            # AI ответил. Зеркалим разговор в топик группы (без forward,
            # чтобы оператор видел диалог) — но НЕ беспокоим его, тикет
            # пока что обслуживается AI.
            try:
                kind, text_for_log = _extract_message_kind_and_text(message)
                await save_message(
                    ticket_id=ticket["id"], user_id=user_id,
                    topic_id=ticket["topic_id"],
                    direction="in", kind=kind, text=text_for_log,
                )
                # Покажем диалог в топике для контекста
                _client_mirror = await bot.send_message(
                    SUPPORT_CHAT_ID,
                    f"💬 <b>Клиент:</b>\n{(text_for_log or '')[:1000]}",
                    message_thread_id=ticket["topic_id"],
                    parse_mode="HTML",
                    disable_notification=True,  # [v3.5] тихо
                )
                # [v3.5] Маппинг: оператор сможет ответить reply'ем
                try:
                    await save_tg_message_map(
                        _client_mirror.message_id, user_id, message.message_id,
                    )
                except Exception as e:
                    logger.debug("save_tg_message_map (AI mirror) failed: {}", e)
                await bot.send_message(
                    SUPPORT_CHAT_ID,
                    f"🤖 <b>AI:</b>\n{ai_answer[:1000]}",
                    message_thread_id=ticket["topic_id"],
                    parse_mode="HTML",
                    disable_notification=True,  # [v3.5] тихо
                )
                # Сохраним ответ AI в messages с пометкой
                await save_message(
                    ticket_id=ticket["id"], user_id=user_id,
                    topic_id=ticket["topic_id"],
                    direction="out", kind="text", text=ai_answer,
                    operator_name="AI",
                )
            except Exception as e:
                logger.warning("user_to_support: зеркалирование AI в топик не удалось: {}", e)
            return

    # Иначе (оператор активен или AI эскалировал) — форвардим как раньше
    # [v3.5] Используем _safe_send_to_topic — если топик удалили вручную,
    # пересоздаём и шлём в новый (а не в General группы).
    # ВАЖНО: forward СО ЗВУКОМ — мы здесь либо потому что оператор уже
    # работает (operator_active), либо AI не справился (escalation).
    # В обоих случаях оператор должен заметить сообщение клиента.

    # [v3.5] Если клиент делает reply на ответ оператора — шлём перед
    # forward'ом короткую плашку с привязкой к оригинальному сообщению
    # оператора в группе. Оператор увидит "↪️ Клиент отвечает на ваше
    # сообщение" с цитатой своего же текста.
    # Управляется sound_operator_reply_to_client (по умолчанию тихо).
    if message.reply_to_message:
        try:
            op_group_id = await get_operator_group_msg_id(
                user_id, message.reply_to_message.message_id,
            )
            if op_group_id:
                try:
                    from app import bot_settings as _bs
                    reply_sound = _bs.get_bool("sound_operator_reply_to_client")
                    await bot.send_message(
                        chat_id=SUPPORT_CHAT_ID,
                        message_thread_id=ticket["topic_id"],
                        text="↪️ <i>Клиент отвечает на ваше сообщение:</i>",
                        parse_mode="HTML",
                        reply_to_message_id=op_group_id,
                        allow_sending_without_reply=True,
                        disable_notification=not reply_sound,
                    )
                    logger.info(
                        "user_to_support: reply-плашка для user={} "
                        "op_group_id={}",
                        user_id, op_group_id,
                    )
                except Exception as e:
                    logger.debug("reply-плашка не отправилась: {}", e)
        except Exception as e:
            logger.debug("get_operator_group_msg_id failed: {}", e)
    try:
        # forward_message → copy_message через safe-helper (copy не теряет thread)
        # Но нам нужен именно forward, чтобы оператор видел кто прислал.
        # Делаем сами: пробуем forward, при topic-ошибке — пересоздаём.
        try:
            _fwd = await bot.forward_message(
                chat_id=SUPPORT_CHAT_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                message_thread_id=ticket["topic_id"],
                disable_notification=False,  # [v3.5] СО ЗВУКОМ
            )
            # [v3.5] Маппинг для reply оператора
            try:
                await save_tg_message_map(
                    _fwd.message_id, user_id, message.message_id,
                )
            except Exception as e:
                logger.debug("save_tg_message_map (forward) failed: {}", e)
        except Exception as fe:
            err = str(fe).lower()
            if any(m in err for m in _TOPIC_INVALID_MARKERS) or "topic" in err:
                logger.warning(
                    "user_to_support: topic {} удалён, пересоздаю",
                    ticket["topic_id"],
                )
                new_tid = await _ensure_topic_exists(
                    user_id, user_obj=message.from_user,
                )
                await save_topic(ticket["id"], new_tid)
                # Шапка восстановления — со звуком (оператор должен заметить)
                try:
                    admin_info = await admin_panel.build_ticket_info_block(user_id)
                except Exception:
                    admin_info = ""
                try:
                    await bot.send_message(
                        chat_id=SUPPORT_CHAT_ID,
                        message_thread_id=new_tid,
                        text=(
                            "♻️ <b>Топик пересоздан</b>\n"
                            "<i>(старый был удалён, история сохранена в БД)</i>\n\n"
                            f"👤 <b>User ID:</b> <code>{user_id}</code>\n"
                            f"🛠 {ADMIN_PANEL_URL}{user_id}"
                            f"{admin_info}"
                        ),
                        parse_mode="HTML",
                        reply_markup=ticket_admin_keyboard(user_id, ADMIN_PANEL_URL),
                        disable_notification=False,  # [v3.5] СО ЗВУКОМ
                    )
                except Exception as he:
                    logger.warning("user_to_support: recover header: {}", he)
                # Повтор forward в новый топик — со звуком
                _fwd2 = await bot.forward_message(
                    chat_id=SUPPORT_CHAT_ID,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    message_thread_id=new_tid,
                    disable_notification=False,  # [v3.5] СО ЗВУКОМ
                )
                try:
                    await save_tg_message_map(
                        _fwd2.message_id, user_id, message.message_id,
                    )
                except Exception as e:
                    logger.debug("save_tg_message_map (recover forward) failed: {}", e)
                ticket["topic_id"] = new_tid
            else:
                raise
    except Exception as e:
        logger.warning(f"Forward to support failed: {e}")
        await message.answer(
            "⚠️ Не удалось переслать сообщение поддержке. "
            "Попробуйте ещё раз через минуту."
        )
        return

    # Сохраняем входящее сообщение в БД
    try:
        kind, text_for_log = _extract_message_kind_and_text(message)
        await save_message(
            ticket_id=ticket["id"], user_id=user_id,
            topic_id=ticket["topic_id"],
            direction="in", kind=kind, text=text_for_log,
        )
    except Exception as e:
        logger.warning("save_message (in) failed: {}", e)


# ============================================================
#  MY_CHAT_MEMBER — клиент удалил бота / запретил сообщения
# ============================================================

@dp.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated):
    """
    Telegram присылает этот update когда:
    - Клиент удалил бот (status = 'kicked')
    - Клиент заблокировал бота (тоже 'kicked')
    - Клиент разблокировал/запустил снова (status = 'member')

    Реагируем только на личные чаты (не группы) и только на 'kicked'.
    """
    # Только личные чаты
    if event.chat.type != "private":
        return

    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status if event.old_chat_member else None
    user = event.from_user

    if not user:
        return

    user_id = user.id
    username = f"@{user.username}" if user.username else None
    full_name = user.full_name or "—"

    # 'kicked' = пользователь нажал «Заблокировать» или удалил бота
    if new_status == "kicked" and old_status != "kicked":
        logger.info(
            "USER_LEFT: user_id={} ({}) удалил/заблокировал бота",
            user_id, username or full_name,
        )

        # Уведомление в группу операторов (если есть открытый тикет —
        # уведомление идёт в этот же топик; иначе общим сообщением)
        notify_text = (
            f"🚫 <b>Клиент удалил бота</b>\n\n"
            f"👤 {full_name}"
            + (f" · {username}" if username else "")
            + f"\n🆔 <code>{user_id}</code>\n\n"
            f"<i>Бот больше не может писать этому пользователю. "
            f"Если тикет был открыт — отвечать в нём бесполезно.</i>"
        )

        try:
            # Ищем ЛЮБОЙ топик клиента (даже если тикет закрыт), чтобы написать
            # уведомление туда — а не в общий чат группы.
            # Проверяем И TG-тикеты, И веб-чаты — клиент мог быть только
            # в одном из каналов.
            from app import database as _db_mod
            from app import web_chat_db as _web_db
            topic_id = None
            # 1) TG-тикеты
            try:
                topic_id = await _db_mod.get_user_topic(user_id)
            except Exception as e:
                logger.debug("get_user_topic failed: {}", e)
            # 2) Если в TG не нашли — пробуем веб-чаты
            if not topic_id:
                try:
                    visitor = await _web_db.get_visitor_by_user_id(user_id)
                    if visitor:
                        topic_id = visitor.get("topic_id")
                except Exception as e:
                    logger.debug("get_visitor_by_user_id failed: {}", e)

            if topic_id and SUPPORT_CHAT_ID:
                # Если топик был закрыт оператором — переоткрываем чтобы
                # уведомление туда попало и было видно в шапке
                try:
                    await bot.reopen_forum_topic(
                        chat_id=SUPPORT_CHAT_ID,
                        message_thread_id=topic_id,
                    )
                except Exception:
                    pass  # уже открыт или другая ошибка

                await bot.send_message(
                    chat_id=SUPPORT_CHAT_ID,
                    message_thread_id=topic_id,
                    text=notify_text,
                    disable_notification=True,  # [v3.5] тихо
                )
                # Меняем иконку топика на 🚫
                await _set_topic_icon(topic_id, TOPIC_ICON_BANNED_EMOJI)
            elif SUPPORT_CHAT_ID:
                # У клиента вообще нет топика — общим сообщением в группу
                await bot.send_message(
                    chat_id=SUPPORT_CHAT_ID,
                    text=notify_text,
                    disable_notification=True,  # [v3.5] тихо
                )
        except Exception as e:
            logger.warning("on_my_chat_member: notify failed: {}", e)

        # Уведомление в личку админам — отключено, чтобы не дублировать.
        # Если оператор сидит в группе поддержки — он увидит в топике клиента.
        # Если нужно — можно включить обратно раскомментировав блок ниже.
        # for admin_id in ADMIN_IDS:
        #     try:
        #         await bot.send_message(admin_id, notify_text)
        #     except Exception:
        #         pass

    # 'member' = клиент вернулся (разблокировал)
    elif new_status == "member" and old_status == "kicked":
        logger.info(
            "USER_RETURNED: user_id={} ({}) вернулся в бот",
            user_id, username or full_name,
        )
        notify = (
            f"✅ <b>Клиент вернулся в бот</b>\n\n"
            f"👤 {full_name}"
            + (f" · {username}" if username else "")
            + f"\n🆔 <code>{user_id}</code>"
        )
        try:
            from app import database as _db_mod
            from app import web_chat_db as _web_db
            topic_id = None
            try:
                topic_id = await _db_mod.get_user_topic(user_id)
            except Exception:
                pass
            # Если в TG-тикетах нет — пробуем веб-чаты
            if not topic_id:
                try:
                    visitor = await _web_db.get_visitor_by_user_id(user_id)
                    if visitor:
                        topic_id = visitor.get("topic_id")
                except Exception:
                    pass
        except Exception:
            topic_id = None

        if topic_id and SUPPORT_CHAT_ID:
            try:
                await bot.send_message(
                    chat_id=SUPPORT_CHAT_ID,
                    message_thread_id=topic_id,
                    text=notify,
                    disable_notification=True,  # [v3.5] тихо
                )
            except Exception:
                pass
            # Восстанавливаем иконку — если был открытый тикет → 💬,
            # иначе ✅ (последний был закрыт)
            try:
                open_ticket = await _db_mod.get_user_open_ticket(user_id)
                if open_ticket:
                    await _set_topic_icon(topic_id, TOPIC_ICON_TG_EMOJI)
                else:
                    await _set_topic_icon(topic_id, TOPIC_ICON_CLOSED_EMOJI)
            except Exception:
                pass


# ============================================================
#  MAIN
# ============================================================

async def main():
    # [v3.5] МАРКЕР НОВОЙ ВЕРСИИ — если этой строки нет в логах,
    # значит контейнер использует СТАРЫЙ файл (docker cp не применился).
    logger.warning(
        "🚀 BOT_START v3.5 — main.py md5_marker=FIX_TICKET_SOUND_v2"
    )
    await init_db()
    # Инициализируем контент-БД и заливаем дефолты из texts.py/keyboards.py
    # для НОВЫХ ключей, которых ещё нет в БД. Существующие правки остаются.
    await _content_cache.init_cache_with_migration()
    # Запускаем watcher — следит за сигнальным файлом и перечитывает кеш
    # когда админка что-то меняет в БД.
    _content_cache.start_watcher()

    # AI: создаём таблицы настроек, истории и статистики.
    # Идемпотентно — если таблицы уже есть, ничего не делает.
    try:
        from admin_web import ai_settings
        from app import ai_assistant
        await ai_settings.init_db()
        await ai_assistant.init_history_table()
        logger.info(
            "AI tables OK (enabled={}, model={}, key={})",
            ai_settings.is_enabled(),
            ai_settings.get_model(),
            "set" if ai_settings.get_api_key() else "not set",
        )
    except Exception as e:
        logger.warning("AI init failed (продолжаю без AI): {}", e)

    # [v3.5] Инициализация таблицы security_settings
    try:
        from app import security
        await security.init_db()
        logger.info("Security tables OK")
    except Exception as e:
        logger.warning("Security init failed: {}", e)

    # [v3.5] Инициализация настроек поведения бота
    try:
        from app import bot_settings
        await bot_settings.init_settings_db()
        # Watcher — следит за signal-файлом, перечитывает кеш когда
        # админка что-то меняет.
        bot_settings.start_watcher(interval=2.0)
    except Exception as e:
        logger.warning("Bot settings init failed: {}", e)

    # Запускаем HTTP API для веб-чата на отдельном порту.
    # Caddy проксирует его на https://your-domain.com/api/chat/*
    from app import web_chat_api
    web_chat_runner = await web_chat_api.start_web_chat_server(bot)

    await admin_panel.get_client().start()
    # Загружаем доступные эмодзи для иконок топиков (один раз, при старте)
    await _load_topic_icons()
    logger.info("Bot started")
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        # Явно указываем какие update'ы слушаем, включая my_chat_member
        # для отслеживания удаления бота клиентом
        await dp.start_polling(
            bot,
            allowed_updates=[
                "message", "edited_message", "callback_query",
                "my_chat_member", "chat_member",
            ],
        )
    finally:
        await admin_panel.get_client().close()
        await web_chat_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())