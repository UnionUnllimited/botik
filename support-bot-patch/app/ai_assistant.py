"""
AI-ассистент для автоматических ответов клиентам.

Использует OpenAI Chat Completion API напрямую через aiohttp
(без зависимости от openai-python SDK, чтобы контролировать таймауты
и не тянуть лишнее).

Архитектура:
  - build_system_prompt() — собирает большой системный промпт
    из настроек + QA-автоответов + FAQ-текстов бота
  - get_history() / append_history() — короткий контекст диалога
  - ask() — основная функция: принимает сообщение клиента,
    возвращает ответ AI + признак эскалации к оператору

История диалога хранится в таблице ai_history (последние ~10 сообщений
на каждого клиента).
"""

from __future__ import annotations

import asyncio
import os
import re

import aiohttp
import aiosqlite
from loguru import logger

DB_PATH = os.getenv("DB_PATH", "/app/data/vpn_support.db")

# URL и модель теперь приходят из ai_settings (зависят от выбранного провайдера).
# OPENAI_URL оставлен как backward-compat для test_api_key.
REQUEST_TIMEOUT = 30  # секунд на ответ от API
MAX_HISTORY_TURNS = 8  # последних N пар «вопрос-ответ» в контексте
ESCALATE_MARKER = "[ESCALATE]"

# [v3.5] Ключевые слова, по которым принудительно эскалируем тикет —
# даже если AI ответил без [ESCALATE]. Защита от случаев когда модель
# забыла эскалировать или решила «направить клиента в другой канал».
# Каждое слово/фраза — нижний регистр, проверяется как подстрока.
_FORCE_ESCALATE_KEYWORDS = [
    # Возврат денег — ВСЕГДА к оператору
    "верните деньги", "верни деньги", "вернуть деньги",
    "хочу деньги назад", "хочу деньги обратно", "хочу обратно деньги",
    "хочу вернуть деньги",
    "возврат денег", "возврат средств", "возврат платежа",
    "refund", "money back", "give me my money",
    "отказ от подписки", "отмените подписку и верните",
    # Угрозы — критично
    "буду жаловаться", "пожалуюсь", "напишу жалобу",
    "обращусь в суд", "подам в суд", "в суд подам",
    "чарджбек", "чардж-бек", "chargeback",
    "обращусь в банк", "напишу в банк",
    "роспотребнадзор", "прокуратура", "юрист",
    # Финансовые проблемы
    "двойное списание", "два раза списали", "дважды списали",
    "списали лишнее", "неверная сумма", "ошибка списания",
    # Прямое требование человека
    "позовите оператора", "позови оператора",
    "дайте оператора", "дайте мне оператора", "хочу оператора",
    "хочу с человеком", "хочу живого человека",
    "только с человеком", "не с ботом", "не хочу с ботом",
    "дайте менеджера", "соедините с менеджером",
    "соедини с оператором",
]


def _check_force_escalate(user_message: str) -> str | None:
    """
    [v3.5] Проверяет — содержит ли сообщение клиента слова, по которым
    нужно ПРИНУДИТЕЛЬНО эскалировать (даже если AI не поставил [ESCALATE]).

    Возвращает совпавшее ключевое слово или None.
    """
    if not user_message:
        return None
    text_lower = user_message.lower()
    for keyword in _FORCE_ESCALATE_KEYWORDS:
        if keyword in text_lower:
            return keyword
    return None


# ============================================================
#  СХЕМА ИСТОРИИ ДИАЛОГА
# ============================================================

_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,  -- tg user_id или visitor_id
    role        TEXT NOT NULL,  -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ai_history_user_time
ON ai_history(user_id, created_at);
"""


async def init_history_table() -> None:
    """Создаёт таблицы истории и событий. Безопасно вызывать многократно."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_HISTORY_SCHEMA)
        # [v3.5] Таблица событий AI: каждый ответ AI с исходом
        # (resolved — AI справился, escalated — передал оператору).
        # Используется для статистики в дашборде.
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS ai_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                date TEXT NOT NULL,
                outcome TEXT NOT NULL,
                source TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_ai_events_date
                ON ai_events(date);
            CREATE INDEX IF NOT EXISTS idx_ai_events_outcome
                ON ai_events(outcome);
        """)
        await db.commit()


async def record_event(
    user_id: str,
    outcome: str,
    source: str | None = None,
) -> None:
    """
    [v3.5] Сохраняет событие AI для статистики.

    Args:
        user_id: ID клиента (visitor_id или str(tg_user_id))
        outcome: 'resolved' (AI ответил без эскалации) или 'escalated'
        source: 'telegram' | 'webchat' | None
    """
    if outcome not in ("resolved", "escalated"):
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO ai_events(user_id, date, outcome, source) "
                "VALUES (?, date('now'), ?, ?)",
                (str(user_id), outcome, source),
            )
            await db.commit()
    except Exception as e:
        logger.warning("ai.record_event failed: {}", e)


async def get_history(user_id: str, limit: int = MAX_HISTORY_TURNS * 2) -> list[dict]:
    """
    Возвращает последние N сообщений (упорядоченные по времени, старые → новые).
    Формат: [{"role": "user", "content": "..."}, ...]
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT role, content FROM ai_history "
            "WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (str(user_id), limit),
        )
        rows = await cur.fetchall()
    # Возвращаем в хронологическом порядке (старые → новые)
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


async def append_history(user_id: str, role: str, content: str) -> None:
    """Добавляет одно сообщение в историю."""
    if role not in ("user", "assistant"):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO ai_history(user_id, role, content) VALUES (?, ?, ?)",
            (str(user_id), role, content),
        )
        await db.commit()


async def clear_history(user_id: str) -> None:
    """Очищает историю диалога (например когда оператор закрыл тикет)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM ai_history WHERE user_id = ?", (str(user_id),),
        )
        await db.commit()


async def get_qa_photo_paths(qa_key: str) -> list[str]:
    """
    [v3.5] Возвращает пути к фото из QA по ключу.
    Используется когда AI указал маркер [PHOTO:qa_key] — после этого
    бот достаёт пути и шлёт клиенту вместе с текстовым ответом AI.

    photo_path в БД может быть:
    - JSON-массив:  '["file1.jpg", "file2.jpg"]'  (новый формат qa_manager)
    - Одна строка:  'file1.jpg'                    (старый формат)
    - Несколько через '|':  'a.jpg|b.jpg'          (на всякий случай)

    Также резолвим относительные пути в абсолютные (через QA_PHOTOS_DIR
    если он определён в qa_manager).
    """
    import json as _json
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT photo_path FROM content_qa "
                "WHERE key = ? AND COALESCE(hidden, 0) = 0",
                (qa_key,),
            )
            row = await cur.fetchone()
    except Exception as e:
        logger.warning("get_qa_photo_paths({}): {}", qa_key, e)
        return []
    if not row or not row[0]:
        return []
    raw = str(row[0]).strip()
    if not raw:
        return []

    # Парсим в порядке вероятности форматов
    parts: list[str] = []
    if raw.startswith("["):
        # JSON-массив (основной формат qa_manager)
        try:
            data = _json.loads(raw)
            if isinstance(data, list):
                parts = [str(x).strip() for x in data if x and str(x).strip()]
        except (_json.JSONDecodeError, ValueError):
            pass
    if not parts:
        # Разделитель '|' (запасной)
        if "|" in raw:
            parts = [p.strip() for p in raw.split("|") if p.strip()]
        else:
            # Одна строка — старый формат
            parts = [raw]

    # Резолв относительных путей: ищем файл в QA_PHOTOS_DIR
    # (qa_manager сохраняет имена файлов, а не полные пути)
    resolved: list[str] = []
    try:
        from admin_web.qa_manager import QA_PHOTOS_DIR
        qa_dir = str(QA_PHOTOS_DIR)
    except Exception:
        qa_dir = None

    for p in parts:
        if not p:
            continue
        # Уже абсолютный путь и файл существует — используем как есть
        if os.path.isabs(p) and os.path.isfile(p):
            resolved.append(p)
            continue
        # Относительный — пробуем склеить с QA_PHOTOS_DIR
        if qa_dir:
            candidate = os.path.join(qa_dir, p)
            if os.path.isfile(candidate):
                resolved.append(candidate)
                continue
        # Файл может уже быть валидным относительно текущей директории
        if os.path.isfile(p):
            resolved.append(os.path.abspath(p))
            continue
        logger.warning(
            "get_qa_photo_paths({}): файл не найден: {!r} (qa_dir={})",
            qa_key, p, qa_dir,
        )

    return resolved


async def format_history_for_operator(
    user_id: str,
    max_messages: int = 20,
    max_text_len: int = 500,
) -> str:
    """
    [v3.5] Форматирует историю диалога клиент↔AI для отправки оператору
    при создании тикета. Возвращает текст для TG-сообщения (или пустую строку
    если истории нет).

    Args:
        user_id: ключ истории (str(tg_user_id) или visitor_id)
        max_messages: максимум сообщений из конца истории
        max_text_len: обрезка каждого сообщения до N символов
    """
    history = await get_history(str(user_id), limit=max_messages)
    if not history:
        return ""

    lines = ["📋 <b>Переписка клиента с AI:</b>", ""]
    for h in history:
        role = h.get("role", "")
        content = (h.get("content") or "").strip()
        if not content:
            continue
        # Обрезаем длинные сообщения
        if len(content) > max_text_len:
            content = content[:max_text_len].rstrip() + "..."
        # Экранируем HTML
        import html as _html
        content = _html.escape(content)
        if role == "user":
            lines.append(f"💬 <b>Клиент:</b>\n{content}")
        elif role == "assistant":
            lines.append(f"🤖 <b>AI:</b>\n{content}")
        lines.append("")  # пустая строка между сообщениями

    return "\n".join(lines).strip()


# ============================================================
#  СБОРКА СИСТЕМНОГО ПРОМПТА
# ============================================================

async def _gather_qa_summary() -> str:
    """Сводка по QA-автоответам как часть базы знаний.
    [v3.5] Помечаем QA с фото и указываем ключ — AI может добавить
    маркер [PHOTO:qa_key] чтобы фото отправилось вместе с текстом.
    Скрытые ответы (hidden=1) не показываем модели.
    """
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT key, label, text, photo_path, COALESCE(hidden, 0) "
                "FROM content_qa "
                "WHERE COALESCE(hidden, 0) = 0 "
                "ORDER BY position, key"
            )
            rows = await cur.fetchall()
    except Exception as e:
        logger.warning("ai: не смог прочитать content_qa: {}", e)
        return ""
    if not rows:
        return ""
    chunks = ["=== ТИПОВЫЕ ОТВЕТЫ НА ВОПРОСЫ КЛИЕНТОВ ===\n"]
    qa_with_photos = []
    for key, label, text, photo_path, _hidden in rows:
        text_clean = _strip_html_tags(text)
        has_photo = bool(photo_path and photo_path.strip())
        if has_photo:
            qa_with_photos.append(key)
        photo_mark = " [📷 содержит фото]" if has_photo else ""
        # Указываем ключ (для маркера [PHOTO:key])
        chunks.append(
            f"\n--- {label} (ключ: {key}){photo_mark} ---\n"
            f"{text_clean[:600]}"
        )

    # [v3.5] Инструкция для AI как использовать маркер фото
    if qa_with_photos:
        examples = ", ".join(qa_with_photos[:3])
        chunks.append(
            "\n\n=== 📷 КАК ОТПРАВИТЬ ФОТО КЛИЕНТУ (важно!) ===\n"
            "У некоторых QA выше есть пометка [📷 содержит фото]. Если ответ "
            "на вопрос клиента основан на таком QA — ОБЯЗАТЕЛЬНО добавь "
            "в конец своего ответа маркер [PHOTO:ключ_qa]. Это автоматически "
            "пришлёт фото клиенту вместе с твоим текстом — клиент увидит "
            "и текст, и картинку.\n\n"
            f"QA с фото (используй их ключи в маркере): {examples}\n\n"
            "ПРИМЕРЫ правильного использования:\n"
            f"  «Вот как включить роутер:\\n1. Кабель провайдера в порт WAN...\\n"
            f"  2. Импортируйте подписку. [PHOTO:{qa_with_photos[0]}]»\n\n"
            f"  «Инструкция по подключению — см. фото. [PHOTO:{qa_with_photos[0]}]»\n\n"
            "ВАЖНО:\n"
            "- Маркер должен быть в КОНЦЕ ответа (не в середине)\n"
            "- Точный формат: [PHOTO:ключ_qa] квадратные скобки, без пробелов\n"
            "- Маркер автоматически удалится из видимого текста — клиент его не увидит\n"
            "- Можно несколько: [PHOTO:qa_a,qa_b]\n"
            "- Используй ТОЛЬКО для QA с пометкой [📷] — иначе фото не существует\n"
            "- Если у клиента вопрос про установку/подключение — почти всегда "
            "ему помогут фото из инструкций"
        )

    return "\n".join(chunks)


async def _gather_faq_texts() -> str:
    """FAQ-тексты бота (PAY_NO_PAGE, VPN_HOW_CONNECT и т.д.) как доп. справочник."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT key, value FROM content_texts "
                "WHERE key LIKE 'PAY_%' OR key LIKE 'BOT_%' OR key LIKE 'VPN_%' "
                "OR key = 'COMMUNITY_INFO'"
            )
            rows = await cur.fetchall()
    except Exception as e:
        logger.warning("ai: не смог прочитать content_texts: {}", e)
        return ""
    if not rows:
        return ""
    chunks = ["\n\n=== СПРАВОЧНИК ЧАСТЫХ ВОПРОСОВ ===\n"]
    for key, value in rows:
        value_clean = _strip_html_tags(value)
        chunks.append(f"\n--- {key} ---\n{value_clean[:500]}")
    return "\n".join(chunks)


def _strip_html_tags(s: str) -> str:
    """Убирает HTML-теги из строки (для системного промпта)."""
    return re.sub(r'<[^>]+>', '', s or '')


async def build_system_prompt(
    source: str | None = None,
    has_image: bool = False,
) -> str:
    """
    Собирает полный системный промпт:
        [vision-блок?] + [маркер канала] + базовая инструкция + QA + FAQ + канал-extra + [напоминание канала]

    [v3.5] Канал маркер добавляется ДВА раза: в самое начало и в конец промпта.
    Это нужно потому что AI часто «забывает» инструкции из середины промпта
    к моменту генерации ответа. Двойной маркер фиксирует канал жёстко.

    Args:
        source: 'telegram' | 'webchat' — откуда пишет клиент.
        has_image: True если в текущем сообщении клиент прислал фото.
                   Тогда в самое начало промпта добавляется явная инструкция
                   что модель должна посмотреть на фото и не отказываться. [v3.5]
    """
    from admin_web import ai_settings
    base = ai_settings.get_system_prompt()
    qa = await _gather_qa_summary()
    faq = await _gather_faq_texts()

    # [v3.5] Vision-блок — добавляется в САМОЕ начало если клиент прислал фото.
    # Без этого блока gpt-4o-mini может отказаться («я не могу просматривать
    # изображения»), даже если фото фактически передано в запрос.
    vision_header = ""
    if has_image:
        vision_header = (
            "🖼️🖼️🖼️ ВАЖНО! ВАЖНО! ВАЖНО! 🖼️🖼️🖼️\n"
            "В ЭТОМ сообщении клиент прислал ФОТО (image_url в content).\n"
            "Ты — gpt-4o-mini, МУЛЬТИМОДАЛЬНАЯ модель. У тебя ЕСТЬ зрение.\n\n"
            "ЧТО ТЫ ДОЛЖЕН СДЕЛАТЬ ПЕРЕД ОТВЕТОМ:\n"
            "1. Внимательно посмотри на фото в content[1].image_url\n"
            "2. В первом предложении ответа ОПИШИ ЧТО ВИДИШЬ на фото\n"
            "   («На скриншоте я вижу...», «На фото — задняя панель роутера...»)\n"
            "3. Затем уже отвечай на вопрос клиента, опираясь на то что увидел\n\n"
            "ЗАПРЕЩЕНО:\n"
            "- Игнорировать фото и отвечать только на текст подписи\n"
            "- Говорить «я не могу анализировать изображения» — это неправда\n"
            "- Давать общий ответ не глядя на конкретное изображение\n"
            "- Использовать Markdown (** ##) — только HTML-теги\n\n"
            "Если на фото скриншот приложения — назови приложение, какие "
            "элементы видны, какой текст. Если ошибка — процитируй текст ошибки.\n\n"
            "Пример правильного начала ответа:\n"
            "«На фото вижу заднюю панель роутера: кабель провайдера в порту LAN, "
            "а порт WAN пустой. Интернета из-за этого и нет — "
            "переставьте кабель в WAN...»\n\n"
        )

    # [v3.5] Канал-маркер в САМОМ НАЧАЛЕ — это первое что увидит модель
    channel_header = ""
    channel_extra = ""
    channel_footer = ""

    if source == "telegram":
        channel_header = (
            "╔════════════════════════════════════════════╗\n"
            "║  📱 КАНАЛ КЛИЕНТА: TELEGRAM-БОТ            ║\n"
            "╚════════════════════════════════════════════╝\n"
            "Клиент пишет ИЗ нашего Telegram-бота.\n"
            "У него ОТКРЫТ Telegram, видны кнопки бота.\n"
            "→ Используй TG-стиль: «нажмите кнопку Х», «откройте раздел Y»\n"
            "→ Эмодзи-нумерация 1️⃣ 2️⃣ 3️⃣ работает\n"
            "→ HTML-форматирование (<b>, <i>, <code>) работает\n\n"
        )
        channel_extra = ai_settings.get_telegram_extra()
        channel_footer = (
            "\n\n📱 НАПОМИНАНИЕ: клиент в TELEGRAM. "
            "Можешь упоминать кнопки и команды бота."
        )
    elif source == "webchat":
        channel_header = (
            "╔════════════════════════════════════════════╗\n"
            "║  🌐 КАНАЛ КЛИЕНТА: ВЕБ-ВИДЖЕТ НА САЙТЕ     ║\n"
            "╚════════════════════════════════════════════╝\n"
            "Клиент СЕЙЧАС НА НАШЕМ САЙТЕ в личном кабинете.\n"
            "Виджет чата встроен в сам сайт.\n\n"
            "⚡ ВАЖНО: на нашем сайте есть ПОЛНОЦЕННЫЙ личный\n"
            "кабинет — там можно: оплатить, продлить, посмотреть дни,\n"
            "управлять устройствами, скачать конфиг. КЛИЕНТ УЖЕ ТАМ.\n\n"
            "🎯 ПРАВИЛЬНАЯ ЛОГИКА ОТВЕТОВ:\n"
            "1. СНАЧАЛА предлагай вариант через САЙТ — клиент уже там\n"
            "2. Если на сайте функции нет — ТОГДА упоминай Telegram-бот\n"
            "3. НЕ отправляй в Telegram «по умолчанию» — это плохой UX\n\n"
            "🚫 ИЗБЕГАЙ:\n"
            "  - «Откройте Telegram-бот...» если\n"
            "    действие можно сделать на сайте\n"
            "  - «Нажмите кнопку X в боте» — клиент не в боте\n"
            "  - 1️⃣ 2️⃣ 3️⃣ нумерация (выглядит как кнопки)\n\n"
            "✅ ПРИМЕР ПРАВИЛЬНОГО ОТВЕТА:\n"
            "Вопрос: «Как удалить устройство?»\n"
            "Ответ: «В личном кабинете найдите раздел "
            "«Мои устройства», нажмите «Удалить» рядом с ненужным "
            "устройством. Слот освободится 🙂»\n\n"
            "❌ НЕПРАВИЛЬНО (хотя технически адаптировано):\n"
            "«Откройте наш Telegram-бот, там...» — \n"
            "клиент СЕЙЧАС на сайте, не отправляй его в TG!\n\n"
        )
        channel_extra = ai_settings.get_webchat_extra()
        channel_footer = (
            "\n\n🌐 НАПОМИНАНИЕ: клиент УЖЕ на нашем сайте в "
            "ЛИЧНОМ КАБИНЕТЕ. Давай инструкции ЧЕРЕЗ САЙТ. Telegram-бот "
            "упоминай ТОЛЬКО если на сайте функции нет."
        )

    parts = []
    # [v3.5] Vision-блок — в самое начало, если есть фото
    if vision_header:
        parts.append(vision_header)
    if channel_header:
        parts.append(channel_header)
    parts.append(base)
    if qa:
        parts.append("\n\n" + qa)
    if faq:
        parts.append(faq)

    # Дополнительный канал-блок из админки
    if channel_extra and channel_extra.strip():
        parts.append("\n\n=== ИНСТРУКЦИИ ДЛЯ КАНАЛА ===\n" + channel_extra.strip())

    # Финальное напоминание о канале (после всей базы знаний)
    if channel_footer:
        parts.append(channel_footer)

    parts.append(
        f"\n\nЕсли ты не можешь помочь — закончи свой ответ маркером "
        f"{ESCALATE_MARKER} (он будет удалён, клиент его не увидит). "
        f"Это сигнал передать диалог живому оператору."
    )
    return "".join(parts)


# ============================================================
#  ОСНОВНАЯ ФУНКЦИЯ — ПОЛУЧИТЬ ОТВЕТ AI
# ============================================================

class AIResult:
    """Результат вызова AI."""
    def __init__(self, text: str, escalate: bool,
                 tokens_in: int = 0, tokens_out: int = 0,
                 error: str | None = None,
                 photo_keys: list[str] | None = None):
        self.text = text
        self.escalate = escalate
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.error = error
        # [v3.5] Ключи QA-ответов чьи фото нужно отправить вместе с текстом.
        # Заполняется парсингом маркера [PHOTO:qa_key] из ответа модели.
        self.photo_keys: list[str] = photo_keys or []

    @property
    def ok(self) -> bool:
        return self.error is None


async def ask(
    user_id: str,
    user_message: str,
    tg_user_id: int | None = None,
    source: str | None = None,
    image_data_url: str | None = None,
) -> AIResult:
    """
    Главный метод: спросить AI.

    Args:
        user_id: ключ для истории и лимитов токенов (visitor_id для веб-чата,
                 или просто str(tg_user_id) для Telegram).
        user_message: текст вопроса клиента.
        tg_user_id: реальный Telegram user_id клиента (число) для вызова tools.
                    Если None — AI не сможет получить персональные данные
                    клиента через tools (но всё равно ответит из общей базы знаний).
        source: 'telegram' | 'webchat' — откуда пишет клиент. Влияет на
                канал-специфичные инструкции AI (см. build_system_prompt). [v3.5]
        image_data_url: data URL (base64) с фото которое прислал клиент.
                        Формат: 'data:image/jpeg;base64,...'. Только для
                        моделей с vision (gpt-4o-mini, gpt-4o). [v3.5]

    Returns:
        AIResult — ответ с признаком эскалации к оператору.
    """
    from admin_web import ai_settings

    # Защита: AI выключен — даже не ходим в API
    if not ai_settings.is_enabled():
        return AIResult("", escalate=True, error="AI disabled")

    api_key = ai_settings.get_api_key()
    if not api_key:
        logger.warning("ai.ask: ключ OpenAI не задан")
        return AIResult("", escalate=True, error="No API key")

    # Лимит на пользователя
    limit = ai_settings.get_max_tokens_per_user_day()
    used = await ai_settings.get_user_tokens_today(str(user_id))
    if used >= limit:
        logger.info("ai.ask: лимит токенов исчерпан для {}: {}/{}",
                    user_id, used, limit)
        return AIResult(
            "Слишком много вопросов сегодня. Подключаю оператора.",
            escalate=True, error="quota_exceeded",
        )

    # Собираем сообщения: system + история + новый user
    # [v3.5] has_image=True добавляет в начало промпта явное указание что
    # модель vision и может смотреть фото (фикс для gpt-4o-mini quirk
    # «не могу анализировать изображения»).
    has_image = bool(image_data_url and image_data_url.strip())
    if has_image:
        # [v3.5] Информативный лог чтобы видеть в реальном времени
        img_size_kb = len(image_data_url) // 1024
        logger.info(
            "ai.ask: 🖼️ VISION запрос (img ~{} КБ base64, user={})",
            img_size_kb, user_id,
        )
    system_prompt = await build_system_prompt(source=source, has_image=has_image)
    # [v3.5] При vision-запросе НЕ загружаем историю. Если в ней есть
    # старый ответ AI «не могу просматривать изображения» — модель будет
    # повторять этот отказ. Vision-сообщение обрабатываем как одиночный запрос.
    if has_image:
        history = []
        logger.info("ai.ask: история отключена (vision-запрос)")
    else:
        history = await get_history(str(user_id))

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)

    # [v3.5] Если клиент прислал фото — формируем multimodal content
    # (text + image_url). Работает с моделями vision (gpt-4o, gpt-4o-mini).
    # Для не-vision моделей фото игнорируется — отвечаем только по тексту.
    if image_data_url and image_data_url.strip():
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": user_message or "(посмотри что на фото)"},
                {"type": "image_url",
                 "image_url": {"url": image_data_url, "detail": "low"}},
            ],
        })
        logger.info("ai.ask: используется vision (image_data_url передан)")
    else:
        messages.append({"role": "user", "content": user_message})

    model = ai_settings.get_model()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    api_url = ai_settings.get_base_url()
    provider_name = ai_settings.get_provider_config(
        ai_settings.get_provider()
    )["name"]

    # Подключаем инструменты ТОЛЬКО если у нас есть реальный tg_user_id
    # (иначе модель попыталась бы получить данные но не смогла бы их найти).
    # Для веб-чата tg_user_id извлекается из web_visitors.user_id.
    # [v3.5] При vision-запросе (есть фото) tools НЕ подключаем — gpt-4o-mini
    # иногда отказывается анализировать фото когда одновременно есть tools.
    from app import ai_tools
    if has_image:
        tools_spec = None
        logger.info("ai.ask: tools отключены (vision-запрос)")
    elif tg_user_id is not None and tg_user_id > 0:
        tools_spec = ai_tools.AI_TOOLS_SPEC
    else:
        tools_spec = None

    # ============================================================
    #  Цикл tool-calls: модель может несколько раз попросить tool,
    #  пока не сформирует финальный ответ текстом.
    # ============================================================
    MAX_TOOL_ITERATIONS = 5  # защита от зацикливания
    total_tokens_in = 0
    total_tokens_out = 0
    text: str = ""
    tool_calls_log: list[str] = []  # для логов какие tools вызывались

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        ) as session:

            for iteration in range(MAX_TOOL_ITERATIONS):
                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.4,
                    "max_tokens": 600,
                }
                # Tools добавляем только если они доступны
                if tools_spec:
                    payload["tools"] = tools_spec
                    payload["tool_choice"] = "auto"
                async with session.post(api_url, json=payload, headers=headers) as r:
                    if r.status != 200:
                        body = await r.text()
                        logger.warning(
                            "ai.ask: HTTP {} from {}: {}",
                            r.status, provider_name, body[:500],
                        )
                        # [v3.5] Специфичная ошибка vision — нет поддержки в модели
                        body_lower = body.lower()
                        if image_data_url and (
                            "image" in body_lower or "vision" in body_lower
                            or "multimodal" in body_lower
                        ):
                            logger.warning(
                                "ai.ask: модель {} НЕ поддерживает vision. "
                                "Используйте gpt-4o-mini или gpt-4o.",
                                model,
                            )
                            return AIResult(
                                "", escalate=True,
                                error=f"vision_not_supported_by_{model}",
                            )
                        return AIResult(
                            "", escalate=True,
                            error=f"{provider_name} HTTP {r.status}",
                        )
                    data = await r.json()

                # Считаем токены этой итерации
                usage = data.get("usage", {})
                total_tokens_in += int(usage.get("prompt_tokens", 0))
                total_tokens_out += int(usage.get("completion_tokens", 0))

                try:
                    msg = data["choices"][0]["message"]
                except (KeyError, IndexError):
                    logger.warning("ai.ask: bad response format: {}", str(data)[:200])
                    return AIResult("", escalate=True, error="bad_response")

                # Если модель попросила tool — выполним и вернёмся в цикл
                tool_calls = msg.get("tool_calls") or []
                if tool_calls and tools_spec:
                    # Добавим assistant-сообщение с tool_calls в историю запроса
                    messages.append({
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": tool_calls,
                    })
                    for tc in tool_calls:
                        fn = tc.get("function") or {}
                        tool_name = fn.get("name", "")
                        tool_args = fn.get("arguments", "")
                        tool_call_id = tc.get("id", "")
                        tool_calls_log.append(tool_name)
                        # Выполняем — user_id фиксированный, не из tool_args
                        result_json = await ai_tools.execute_tool(
                            tool_name, int(tg_user_id), tool_args,
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": result_json,
                        })
                    # Лимит токенов проверяем после каждой итерации
                    if total_tokens_in + total_tokens_out > limit * 2:
                        # Аварийный выход — диалог слишком жирный
                        logger.warning(
                            "ai.ask: превышен 2x-лимит на одну сессию для {}",
                            user_id,
                        )
                        break
                    continue  # → следующая итерация

                # Иначе — финальный текстовый ответ
                text = (msg.get("content") or "").strip()
                break
            else:
                # MAX_TOOL_ITERATIONS исчерпаны — берём что есть
                logger.warning(
                    "ai.ask: достигнут лимит {} итераций tool-calls для {}",
                    MAX_TOOL_ITERATIONS, user_id,
                )

    except asyncio.TimeoutError:
        logger.warning("ai.ask: таймаут запроса к {}", provider_name)
        return AIResult("", escalate=True, error="timeout")
    except Exception as e:
        logger.exception("ai.ask: ошибка запроса к {}: {}", provider_name, e)
        return AIResult("", escalate=True, error=str(e))

    # Алиасы для совместимости с дальнейшим кодом
    tokens_in = total_tokens_in
    tokens_out = total_tokens_out

    if not text:
        text = "Не получилось сформировать ответ. Подключаю оператора."
        escalate = True
        photo_keys: list[str] = []
    else:
        # Проверяем маркер эскалации (и убираем его из ответа)
        escalate = ESCALATE_MARKER in text
        if escalate:
            text = text.replace(ESCALATE_MARKER, "").strip()
            if not text:
                text = "Подключаю оператора, который поможет вам с этим вопросом."

        # [v3.5] Парсим маркер [PHOTO:qa_key] или [PHOTO:qa_key1,qa_key2]
        # AI указывает в конце ответа какое фото из QA отправить вместе с текстом.
        # После парсинга маркер удаляется из видимого ответа.
        photo_keys = []
        import re as _re
        for m in _re.finditer(r'\[PHOTO:\s*([a-zA-Z0-9_,\-\s]+)\s*\]', text):
            keys_str = m.group(1)
            for k in keys_str.split(","):
                k = k.strip()
                if k and k not in photo_keys:
                    photo_keys.append(k)
        text = _re.sub(r'\s*\[PHOTO:[^\]]+\]\s*', ' ', text).strip()

    # [v3.5] Принудительная эскалация по ключевым словам в сообщении клиента.
    # Если клиент написал «верните деньги», «обращусь в суд», «хочу оператора» —
    # эскалируем даже если AI «не понял» и не поставил [ESCALATE].
    # Это защита от случаев когда модель решает «направить клиента в TG/сайт»
    # вместо передачи реального запроса оператору.
    escalate_reason = None  # для подробного лога
    if escalate:
        # Определяем точную причину эскалации
        if not text or text == "Не получилось сформировать ответ. Подключаю оператора.":
            escalate_reason = "empty_ai_response"
        elif ESCALATE_MARKER in (text + ESCALATE_MARKER):
            # Маркер был в ответе и мы его убрали — значит AI сам решил
            escalate_reason = "ai_marker_ESCALATE"
        else:
            escalate_reason = "unknown"

    if not escalate:
        forced_keyword = _check_force_escalate(user_message)
        if forced_keyword:
            escalate = True
            escalate_reason = f"force_keyword:{forced_keyword!r}"
            logger.info(
                "ai.ask: ПРИНУДИТЕЛЬНАЯ эскалация по слову {!r} (user={})",
                forced_keyword, user_id,
            )
            # Заменяем ответ AI на короткое подтверждение
            # (если AI сказал «обратитесь в TG-бот» — клиенту это не нужно)
            text = (
                "Понял ваш запрос. Передаю оператору — он свяжется с вами "
                "в этом чате в ближайшее время 🙂"
            )

    # [v3.5] Подробный лог если эскалировали — для отладки ложных эскалаций
    if escalate:
        logger.info(
            "ai.ask: ESCALATE user={} reason={} user_message={!r:.80} "
            "ai_text={!r:.80}",
            user_id, escalate_reason or "unknown",
            user_message, text,
        )

    # Сохраняем в историю и статистику
    try:
        await append_history(str(user_id), "user", user_message)
        await append_history(str(user_id), "assistant", text)
        await ai_settings.record_request(
            str(user_id), tokens_in, tokens_out, escalated=escalate,
        )
    except Exception as e:
        logger.warning("ai.ask: не смог сохранить историю/статистику: {}", e)

    logger.info(
        "ai.ask user={} provider={} model={} tokens={}+{}={} escalate={} tools={} photos={}",
        user_id, provider_name, model,
        tokens_in, tokens_out, tokens_in + tokens_out, escalate,
        ",".join(tool_calls_log) if tool_calls_log else "—",
        ",".join(photo_keys) if photo_keys else "—",
    )

    # [v3.5] Сохраняем событие для статистики:
    # 'resolved' — AI сам ответил, 'escalated' — передал оператору
    try:
        await record_event(
            user_id,
            outcome="escalated" if escalate else "resolved",
            source=source,
        )
    except Exception as e:
        logger.warning("ai.ask: не смог записать событие: {}", e)

    return AIResult(text, escalate=escalate,
                    tokens_in=tokens_in, tokens_out=tokens_out,
                    photo_keys=photo_keys)


# ============================================================
#  ТЕСТОВЫЙ ПИНГ (для UI «проверить ключ»)
# ============================================================

async def test_api_key(
    api_key: str,
    model: str | None = None,
    provider: str | None = None,
) -> tuple[bool, str]:
    """
    Делает короткий тестовый запрос для проверки что ключ рабочий.
    Возвращает (ok, message).

    Args:
        api_key: ключ для теста
        model: модель (если не задана — берётся дефолтная для провайдера)
        provider: 'openai' | 'groq' (если не задан — берётся текущий из настроек)
    """
    if not api_key or not api_key.strip():
        return False, "Ключ не указан"

    from admin_web import ai_settings as _ais
    if provider is None:
        provider = _ais.get_provider()
    if model is None:
        model = _ais.get_default_model_for_provider(provider)

    api_url = _ais.get_provider_config(provider)["base_url"]
    provider_name = _ais.get_provider_config(provider)["name"]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            async with session.post(api_url, json=payload, headers=headers) as r:
                if r.status == 200:
                    return True, f"OK — {provider_name} ({model}) отвечает"
                if r.status == 401:
                    return False, "Ключ невалидный (401 Unauthorized)"
                if r.status == 429:
                    return False, "Превышен лимит запросов (429)"
                body = await r.text()
                return False, f"HTTP {r.status}: {body[:200]}"
    except asyncio.TimeoutError:
        return False, f"Таймаут запроса к {provider_name}"
    except Exception as e:
        return False, f"Ошибка: {e}"
