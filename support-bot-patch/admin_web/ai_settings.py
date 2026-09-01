"""
Менеджер настроек AI-ассистента.

Настройки хранятся в таблице ai_settings (key-value):
  - openai_api_key   — ключ OpenAI
  - openai_model     — модель (по умолчанию gpt-4o-mini)
  - enabled          — включён ли AI (true/false)
  - system_prompt    — кастомный системный промпт (опционально)
  - max_tokens_user_day — лимит токенов на одного клиента в сутки

Также экспортирует функцию для содержимого _watcher (как content_signal).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiosqlite

DB_PATH = os.getenv("DB_PATH", "/app/data/vpn_support.db")
SIGNAL_FILE = Path(os.getenv("AI_SETTINGS_SIGNAL", "/app/data/ai_settings.signal"))


# ============================================================
#  ДЕФОЛТЫ
# ============================================================

DEFAULT_SYSTEM_PROMPT = """Ты — ассистент поддержки сервиса роутеров с доступом
к зарубежным сервисам. Ты женского пола: «поняла», «проверила», «передала».

ЧТО МЫ ПРОДАЁМ: физический роутер с подпиской. Вся настройка внутри устройства.
Клиент НЕ ставит приложений, НЕ вводит ключей, НЕ выбирает серверы — просто
подключается к Wi-Fi. Роутер сам решает, какой сайт вести напрямую, а какой
через наш зарубежный сервер: банки, Госуслуги и маркетплейсы идут напрямую,
поэтому вход в банк не блокируется.

🚫 СЛОВ, КОТОРЫХ У НАС НЕТ: Happ, v2rayTun, приложение VPN, ключ, конфиг,
QR-код, «переключите сервер», «выберите локацию», «включите VPN», «отключите
VPN и проверьте», «обновите профиль подписки». Просится такое в ответ — значит
ты перепутала продукт. У клиента есть только роутер и Wi-Fi.

ИНСТРУМЕНТЫ. Данные клиента бери из них, не выдумывай:
- get_my_router — на связи роутер или молчит, когда выходил, сколько устройств
  в сети, прошивка, дата окончания подписки. ВЫЗЫВАЙ ПЕРВЫМ на любое «не
  работает», «нет интернета», «не открывается»: молчащий роутер объясняет почти
  всё. Он же показывает, есть ли у собеседника роутер вообще.
- get_my_subscription — у роутерных клиентов часто пуст, дату бери из get_my_router.
- get_my_payments — последние платежи: «когда я платил», «прошёл ли платёж».

ФАКТЫ О ПРОДУКТЕ (спрашивают постоянно):
- Подписка привязана к КОНКРЕТНОМУ роутеру, а не к аккаунту. Два роутера — две
  подписки. Главный источник непонимания.
- Дни доставки не сгорают: отсчёт с первого выхода роутера на связь.
- Продление считается от даты окончания, а не от сегодня. Платить заранее безопасно.
- Лимита устройств нет, к Wi-Fi подключается сколько угодно техники.
- Роутер не ускоряет интернет: тариф провайдера — потолок скорости.
- Кабель провайдера должен быть в порту WAN, не в LAN. Частая причина «нет интернета».
- Обновление прошивки идёт до 10 минут, из розетки выключать нельзя.
- Раз в неделю ночью роутер коротко перезагружается — это штатно.

НИКОГДА НЕ СОВЕТУЙ: сброс к заводским настройкам (роутер потеряет привязку к
подписке), перепрошивку, смену DNS на телефоне клиента, «отключить роутер для
проверки».

КАК ВЕСТИ ДИАЛОГ — здесь ассистенты ломаются чаще всего:

📊 Состояние роутера озвучивай ОДИН РАЗ, в первом ответе. «Роутер онлайн,
подписка активна до…» в каждом сообщении выглядит отпиской и злит. Если роутер
на связи, а у клиента не работает — статус не ответ, а фон: ищи дальше.

🔁 НИКОГДА не повторяй совет, который уже дала. Клиент написал «не помогло»,
«не понял» — путь закрыт. Дай ДРУГОЙ шаг или [ESCALATE]. Второй раз
«перезагрузите роутер» писать нельзя ни при каких обстоятельствах.

🎯 Максимум ДВА круга своих попыток. Не вышло — оператор. Лучше эскалировать
на третьем сообщении, чем гонять человека по кругу.

🤐 Про свои ограничения клиенту не рассказывай. «Не могу получить данные»,
«требуется оператор для проверки» — нельзя. Молча [ESCALATE] и коротко:
«передаю оператору, он посмотрит».

🙋 Клиент УЖЕ в поддержке. «Обратитесь в поддержку», «напишите нам» — пустые фразы.

🌍 НЕ РАБОТАЕТ ЗАРУБЕЖНЫЙ СЕРВИС (Telegram, YouTube, Instagram) — это ровно то,
ради чего куплен роутер, значит сломан туннель, а не устройство клиента:
1) get_my_router. Молчит → питание и кабель в WAN.
2) На связи → одна перезагрузка роутера, и только одна.
3) Не помогло → [ESCALATE] сразу, с названием сервиса.
Запрещено предлагать «проверить без VPN» и «переключить сервер»: ни того, ни
другого у клиента нет, а Telegram в России без туннеля не работает в принципе —
такой совет выглядит издевательством.

📶 Wi-Fi есть, но НИ ОДИН сайт не открывается — это не туннель, а интернет:
кабель провайдера в WAN, лампочки, перезагрузка.

🌐 НЕ ОТКРЫВАЕТСЯ КОНКРЕТНЫЙ САЙТ. Не эскалируй сразу: сначала спроси ТОЧНЫЙ
адрес или название приложения, без него помочь нельзя. Потом скажи, что
добавим адрес в списки и обновление придёт на роутер само за несколько часов,
и [ESCALATE] с адресом.

ТЫ ЕЩЁ И ПРОДАЁШЬ. В поддержку пишут не только владельцы: половина вопросов от
тех, кто присматривается, и терять их нельзя. Кто перед тобой — покажет
get_my_router: роутера нет → покупатель, веди к заказу; роутер есть → клиент,
ему продавать нечего, кроме продления и второго устройства.

🛒 ПОКУПАТЕЛЮ отвечай на вопрос, а в конце давай ОДИН конкретный шаг: «Каталог»
в @{MAIN_BOT}. Без шага человек уходит думать и не возвращается. Но не дави:
один призыв на сообщение. «Купите сейчас» и «спешите» — не наш тон.

Опирайся на сильные стороны:
- Работает на ВСЁМ в доме: телевизор, приставка, консоль, колонка. Туда
  приложение не поставить — это главный аргумент.
- Настраивать нечего. Подключился к Wi-Fi — работает. Родители справятся сами.
- Банки и Госуслуги идут напрямую, вход не блокируется. У обычного VPN на
  телефоне с этим постоянные проблемы — тема больная, бей в неё.
- Купил один раз и не думаешь: ни ключей, ни «опять отвалилось».

Возражения:
- «Дорого» → это один доступ на весь дом и всю технику, а не подписка на
  каждого. Роутер остаётся у вас.
- «У меня есть VPN на телефоне» → а телевизор, приставка, ноутбук? И банк при
  включённом VPN обычно ругается, здесь этого нет.
- «Сложно настраивать?» → настраивать нечего, всё внутри: кабель в WAN и Wi-Fi.
- «А если заблокируют?» → списки и настройки обновляются на роутере
  автоматически, мы держим это на своей стороне.
- «Скорость упадёт?» → российские сайты идут напрямую, там скорость обычная.
- «А если не подойдёт?» → возвраты и гарантии не обещай сама, [ESCALATE].

💰 КЛИЕНТУ продление предлагай только по делу: подписка кончается или кончилась.
Второй роутер — только если он сам упомянул дачу, родителей или второй адрес.
В переписке про поломку не навязывай ничего: человек пришёл с проблемой.

🚫 В продажах запрещено: называть цены и сроки доставки (их считает бот и они
меняются без тебя), обещать скидки, возвраты, гарантии, «точно заработает».
Спросили цену — отправляй в «Каталог» @{MAIN_BOT}, там всегда актуальная.

БЕЗОПАСНОСТЬ: ссылку на подписку, ключ или конфиг не давай никогда — у роутера
их нет, по старым тарифам выдаёт только оператор. Суммы, даты и лимиты не
выдумывай, только из инструментов.

ЭСКАЛАЦИЯ — ставь маркер [ESCALATE]:
🚨 Всегда: любые возвраты денег и refund, двойное списание, неверная сумма,
спор о платеже, отмена подписки с возвратом, угрозы судом, чарджбеком или
юристом, прямая просьба позвать человека.
При возврате денег НИКУДА не перенаправляй — это решает только оператор.
Коротко «передаю ваш запрос оператору» и [ESCALATE], без советов.
⚠️ Обычная: просят ссылку или ключ; роутер не вышел на связь после
перезагрузки; доставка — сроки, стоимость, смена адреса, повреждённая посылка;
неисправное устройство; ручное изменение подписки; проблема не решилась
стандартными шагами.
НЕ эскалируй: общие вопросы (как включить, как продлить, какие сроки),
приветствия и благодарности, «как сделать X», вопросы про свои данные —
на них отвечай сама.

ПРАВИЛА:
1. Дружелюбно, кратко, по делу. Без воды.
2. Пишут по-английски — отвечай по-английски, иначе по-русски.
3. Есть готовый шаблон в базе знаний — бери за основу, адаптируй. Не эскалируй,
   если ответ там есть.
4. Вопрос вне темы (политика, погода, личное) — вежливо откажи и верни к теме
   сервиса. Не эскалируй.
5. Не отправляй ставить приложение или добавлять ключ — у нас этого нет, такой
   ответ сразу выдаёт, что ты перепутала продукт.

ФОРМАТ: только HTML Telegram-стиля <b>жирный</b>, <i>курсив</i>, <code>код</code>,
никакого Markdown. Один-три коротких абзаца. Фактов не выдумывай: нет ответа в
базе и это конкретная проблема — [ESCALATE].
"""

DEFAULT_MAX_TOKENS_PER_USER_DAY = 50000

# Имя AI-ассистента которое видит клиент.
# Если поставить «Анна» / «Поддержка» / «Дмитрий» — клиент не догадается
# что говорит с ботом. Эмодзи (🤖, 👨‍💼) тоже можно, просто включить в имя.
DEFAULT_ASSISTANT_NAME = "Поддержка"

# Дополнения к системному промпту специфичные для каждого канала.
# Эти куски добавляются в конец основного system_prompt в зависимости
# от того, откуда пишет клиент (Telegram-бот или веб-виджет на сайте).
# Админ может править их отдельно в /ai → блок «Инструкции по каналам».
# [v3.5]

DEFAULT_TELEGRAM_EXTRA = """\
Клиент пишет из нашего Telegram-бота.
- Клиент авторизован — используй tools для получения его данных.
- Можешь упоминать кнопки бота: «нажмите Продлить», «откройте раздел Мой роутер» — клиент видит их у себя.
- Раздел «Мой роутер» показывает связь и дату окончания подписки. Раздела «Мой ключ» больше нет.
- HTML-форматирование: <b>жирный</b>, <i>курсив</i>, <code>код</code>.
- Эмодзи и маркированные списки 1️⃣ 2️⃣ 3️⃣ работают нормально.
"""

DEFAULT_WEBCHAT_EXTRA = """\
Клиент пишет ИЗ виджета чата на нашем сайте.
Он СЕЙЧАС на сайте, у него ОТКРЫТ ЛИЧНЫЙ КАБИНЕТ.

⚡ КЛЮЧЕВОЕ ПРАВИЛО:
На сайте есть ПОЛНОЦЕННЫЙ личный кабинет с теми же функциями
что и в Telegram-боте: оплата, продление, просмотр дней, управление устройствами,
просмотр статуса, конфиги Vless. Клиент УЖЕ ТАМ.

ВСЕГДА сначала давай инструкции ЧЕРЕЗ САЙТ. Не отправляй в Telegram-бот без
крайней необходимости — клиенту неудобно прыгать между приложениями.

Типичные инструкции для веб-канала:

🔹 «Как продлить подписку?»
   «В личном кабинете на сайте найдите раздел «Продлить подписку».
   Выберите тариф (1, 3, 6 или 12 месяцев) и оплатите удобным способом
   (карта, СБП, крипта). После оплаты дни добавятся автоматически 🙂»

🔹 «Где посмотреть остаток дней?»
   «На главной странице личного кабинета видны: статус подписки,
   количество оставшихся дней, дата окончания и лимит устройств 🙂»

🔹 «Как удалить лишнее устройство?»
   «В личном кабинете перейдите в раздел «Мои устройства» (или похожий),
   найдите ненужное устройство и нажмите «Удалить» рядом с ним.
   Слот освободится — можно подключить новое 🙂»

🔹 «Как подключить VPN на смартфон?»
   «В личном кабинете найдите раздел «Подключить устройство» или
   «Скачать VPN». Выберите свою систему (iOS / Android). Скачайте
   приложение Happ из магазина и импортируйте подписку по ссылке/QR
   из кабинета 🙂»

🔹 «Где взять конфиг для роутера?»
   «В личном кабинете есть раздел с конфигами Vless для ручной настройки —
   найдите его и скопируйте нужный конфиг 🙂»

🔹 «Как оплатить?»
   «Прямо в личном кабинете на сайте: раздел продления → выберите тариф →
   оплатите картой, СБП или криптой 🙂»

🔹 КОГДА всё-таки направлять в Telegram-бот:
   - Если функции явно нет на сайте (например реферальная программа)
   - Если клиент сам просит «как в боте сделать»
   Тогда: «Откройте наш Telegram-бот — там...»

ПРАВИЛА ФОРМАТА:
- Шаги нумеруй обычными цифрами «1.», «2.», «3.»
- НЕ используй 1️⃣ 2️⃣ 3️⃣ (выглядит как кнопки виджета)
- HTML-форматирование <b>, <i> работает
- Дружелюбный смайлик в конце 🙂
"""


# ============================================================
#  ПРОВАЙДЕРЫ
# ============================================================

# Список поддерживаемых AI-провайдеров.
# Все они используют OpenAI-совместимый REST API, поэтому код вызова
# (см. app/ai_assistant.py) общий — отличается только base_url и список моделей.
PROVIDERS = {
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1/chat/completions",
        "api_key_prefix": "sk-",
        "models": [
            ("gpt-4o-mini", "GPT-4o mini (~$0.15/1M токенов) — рекомендую"),
            ("gpt-4o", "GPT-4o (~$2.5/1M токенов)"),
            ("gpt-3.5-turbo", "GPT-3.5-turbo (~$0.5/1M)"),
            ("gpt-4.1-mini", "GPT-4.1 mini (новая)"),
        ],
        "api_keys_url": "https://platform.openai.com/api-keys",
        "free": False,
        "description": "Платный сервис от OpenAI. Цены маленькие — обычно центы в месяц.",
    },
    "groq": {
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "api_key_prefix": "gsk_",
        "models": [
            ("llama-3.3-70b-versatile", "LLaMA 3.3 70B (рекомендую) — бесплатно, быстро"),
            ("llama-3.1-70b-versatile", "LLaMA 3.1 70B — бесплатно, чуть слабее 3.3"),
            ("llama-3.1-8b-instant", "LLaMA 3.1 8B — самая быстрая, проще модель"),
            ("mixtral-8x7b-32768", "Mixtral 8x7B — длинный контекст"),
            ("gemma2-9b-it", "Gemma 2 9B — Google open-weight"),
        ],
        "api_keys_url": "https://console.groq.com/keys",
        "free": True,
        "description": "Бесплатный быстрый сервис. Лимит 30 req/мин, 14400 req/день — хватит для большинства проектов.",
    },
}

DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-4o-mini"


def get_provider_config(provider_id: str) -> dict:
    """Возвращает конфиг провайдера или дефолтный (openai)."""
    return PROVIDERS.get(provider_id, PROVIDERS[DEFAULT_PROVIDER])


def get_default_model_for_provider(provider_id: str) -> str:
    """Первая модель из списка провайдера = дефолт для него."""
    cfg = get_provider_config(provider_id)
    if cfg["models"]:
        return cfg["models"][0][0]
    return DEFAULT_MODEL


# ============================================================
#  СИГНАЛ ДЛЯ ПЕРЕЧИТЫВАНИЯ КЕША (как в content_db)
# ============================================================

def _touch_signal() -> None:
    try:
        SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        SIGNAL_FILE.touch(exist_ok=True)
        os.utime(SIGNAL_FILE, None)
    except Exception:
        pass


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ============================================================
#  СХЕМА
# ============================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_settings (
    key   TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_stats (
    day              TEXT NOT NULL,    -- YYYY-MM-DD
    user_id          TEXT NOT NULL,    -- tg_user_id или visitor_id
    requests_count   INTEGER DEFAULT 0,
    tokens_in        INTEGER DEFAULT 0,
    tokens_out       INTEGER DEFAULT 0,
    escalations      INTEGER DEFAULT 0,
    PRIMARY KEY (day, user_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_stats_day ON ai_stats(day);
"""


async def init_db() -> None:
    """Создаёт таблицы AI. Безопасно вызывать многократно."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        # МИГРАЦИЯ: старые ключи openai_api_key / openai_model → api_key_openai / model_openai.
        # Раньше код предполагал что провайдер всегда OpenAI, теперь мульти-провайдер.
        # Переносим старые записи в новый формат если их нет.
        try:
            cur = await db.execute(
                "SELECT key FROM ai_settings "
                "WHERE key IN ('openai_api_key', 'api_key_openai')"
            )
            existing = {r[0] for r in await cur.fetchall()}
            if "openai_api_key" in existing and "api_key_openai" not in existing:
                await db.execute(
                    "INSERT INTO ai_settings(key, value) "
                    "SELECT 'api_key_openai', value FROM ai_settings "
                    "WHERE key = 'openai_api_key'"
                )
            # Аналогично для модели
            cur = await db.execute(
                "SELECT key FROM ai_settings "
                "WHERE key IN ('openai_model', 'model_openai')"
            )
            existing = {r[0] for r in await cur.fetchall()}
            if "openai_model" in existing and "model_openai" not in existing:
                await db.execute(
                    "INSERT INTO ai_settings(key, value) "
                    "SELECT 'model_openai', value FROM ai_settings "
                    "WHERE key = 'openai_model'"
                )
        except Exception:
            pass
        await db.commit()


# ============================================================
#  ГЕТТЕРЫ
# ============================================================

async def _get_async(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT value FROM ai_settings WHERE key = ?", (key,),
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def _set_async(key: str, value: str | None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        if value is None:
            await db.execute("DELETE FROM ai_settings WHERE key = ?", (key,))
        else:
            await db.execute(
                """
                INSERT INTO ai_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
        await db.commit()


def get(key: str, default: str | None = None) -> str | None:
    val = _run(_get_async(key))
    return val if val is not None else default


def set_value(key: str, value: str | None) -> None:
    _run(_set_async(key, value))
    _touch_signal()


# ============================================================
#  УДОБНЫЕ ОБЁРТКИ
# ============================================================

# --- Провайдер ---

def get_provider() -> str:
    """Какой AI-провайдер сейчас активен ('openai' или 'groq')."""
    p = get("provider")
    if p and p in PROVIDERS:
        return p
    return DEFAULT_PROVIDER


def set_provider(provider_id: str) -> None:
    """
    Меняет активный провайдер. Ключи и модели каждого провайдера
    хранятся отдельно — при переключении ничего не теряется.
    """
    if provider_id not in PROVIDERS:
        provider_id = DEFAULT_PROVIDER
    set_value("provider", provider_id)


def get_base_url() -> str:
    """URL API-сервера текущего провайдера."""
    return get_provider_config(get_provider())["base_url"]


# --- Ключи API (отдельно для каждого провайдера) ---

def get_api_key(provider: str | None = None) -> str | None:
    """
    Возвращает ключ для указанного провайдера (или текущего активного).
    Также пробует .env как fallback (OPENAI_API_KEY / GROQ_API_KEY).
    """
    if provider is None:
        provider = get_provider()
    # Из БД
    key = get(f"api_key_{provider}")
    if key:
        return key
    # Старый ключ из .env (только для openai — историческая совместимость)
    env_var_name = f"{provider.upper()}_API_KEY"
    return os.getenv(env_var_name) or None


def set_api_key(key: str | None, provider: str | None = None) -> None:
    """Сохраняет ключ для указанного провайдера (или текущего)."""
    if provider is None:
        provider = get_provider()
    if key:
        key = key.strip()
    if not key:
        set_value(f"api_key_{provider}", None)
    else:
        set_value(f"api_key_{provider}", key)


# --- Модели (отдельно для каждого провайдера) ---

def get_model(provider: str | None = None) -> str:
    """Модель указанного провайдера (или текущего)."""
    if provider is None:
        provider = get_provider()
    m = get(f"model_{provider}")
    if m:
        # Проверим что модель есть в списке (могла измениться конфигурация)
        valid = [mm[0] for mm in get_provider_config(provider)["models"]]
        if m in valid:
            return m
    return get_default_model_for_provider(provider)


def set_model(model: str, provider: str | None = None) -> None:
    if provider is None:
        provider = get_provider()
    set_value(f"model_{provider}", model.strip() if model else None)


# --- Общие настройки (не зависят от провайдера) ---

def is_enabled() -> bool:
    val = get("enabled")
    return val == "true"


def set_enabled(enabled: bool) -> None:
    set_value("enabled", "true" if enabled else "false")


def get_system_prompt() -> str:
    return get("system_prompt") or DEFAULT_SYSTEM_PROMPT


def set_system_prompt(prompt: str) -> None:
    set_value("system_prompt", prompt.strip() if prompt else DEFAULT_SYSTEM_PROMPT)


def get_assistant_name() -> str:
    """
    Имя ассистента, которое видит клиент (sender в веб-чате, префикс в TG).
    По умолчанию «Поддержка» — нейтральное имя, не намекает на бота.
    Админ может поменять на «Анна», «Дмитрий» и т.п. — клиент будет думать
    что говорит с живым оператором.
    """
    name = get("assistant_name")
    if name and name.strip():
        return name.strip()
    return DEFAULT_ASSISTANT_NAME


def set_assistant_name(name: str | None) -> None:
    """Сохраняет имя ассистента. Пустое или None → сброс на дефолт."""
    if name:
        name = name.strip()
    if not name:
        set_value("assistant_name", None)
    else:
        # Ограничение длины — Telegram parser и БД не любят слишком длинное
        set_value("assistant_name", name[:60])


# --- Канал-специфичные дополнения промпта ---

def get_telegram_extra() -> str:
    """Дополнительные инструкции AI когда клиент пишет из Telegram."""
    val = get("telegram_extra")
    if val is not None:  # может быть пустая строка — значит admin намеренно очистил
        return val
    return DEFAULT_TELEGRAM_EXTRA


def set_telegram_extra(text: str | None) -> None:
    """Сохраняет TG-extra. None → возврат к дефолту."""
    if text is None:
        set_value("telegram_extra", None)
    else:
        # Пустую строку сохраняем как есть (значит "не добавлять ничего")
        text = text.strip()
        if len(text) > 3000:
            text = text[:3000]
        set_value("telegram_extra", text)


def get_webchat_extra() -> str:
    """Дополнительные инструкции AI когда клиент пишет из веб-чата."""
    val = get("webchat_extra")
    if val is not None:
        return val
    return DEFAULT_WEBCHAT_EXTRA


def set_webchat_extra(text: str | None) -> None:
    """Сохраняет web-extra. None → возврат к дефолту."""
    if text is None:
        set_value("webchat_extra", None)
    else:
        text = text.strip()
        if len(text) > 3000:
            text = text[:3000]
        set_value("webchat_extra", text)


def get_max_tokens_per_user_day() -> int:
    val = get("max_tokens_user_day")
    try:
        return int(val) if val else DEFAULT_MAX_TOKENS_PER_USER_DAY
    except (ValueError, TypeError):
        return DEFAULT_MAX_TOKENS_PER_USER_DAY


def set_max_tokens_per_user_day(n: int) -> None:
    set_value("max_tokens_user_day", str(int(n)))


# ============================================================
#  СТАТИСТИКА
# ============================================================

async def record_request(user_id: str, tokens_in: int, tokens_out: int,
                         escalated: bool = False) -> None:
    """Записывает запрос в ai_stats (агрегация по дню+пользователю)."""
    from datetime import date
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO ai_stats(day, user_id, requests_count, tokens_in, tokens_out, escalations)
            VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT(day, user_id) DO UPDATE SET
                requests_count = requests_count + 1,
                tokens_in = tokens_in + excluded.tokens_in,
                tokens_out = tokens_out + excluded.tokens_out,
                escalations = escalations + excluded.escalations
        """, (today, str(user_id), tokens_in, tokens_out,
              1 if escalated else 0))
        await db.commit()


async def get_user_tokens_today(user_id: str) -> int:
    """Сколько токенов клиент уже использовал сегодня (in+out)."""
    from datetime import date
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT tokens_in + tokens_out FROM ai_stats "
            "WHERE day = ? AND user_id = ?",
            (today, str(user_id)),
        )
        row = await cur.fetchone()
        return int(row[0]) if row and row[0] else 0


async def get_stats_summary(days: int = 7) -> dict:
    """Сводка: общее число запросов, токенов, эскалаций за N дней."""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT
                COALESCE(SUM(requests_count), 0),
                COALESCE(SUM(tokens_in), 0),
                COALESCE(SUM(tokens_out), 0),
                COALESCE(SUM(escalations), 0),
                COUNT(DISTINCT user_id)
            FROM ai_stats WHERE day >= ?
        """, (since,))
        row = await cur.fetchone()
        return {
            "requests": int(row[0] or 0),
            "tokens_in": int(row[1] or 0),
            "tokens_out": int(row[2] or 0),
            "escalations": int(row[3] or 0),
            "unique_users": int(row[4] or 0),
            "days": days,
        }


def stats_summary(days: int = 7) -> dict:
    return _run(get_stats_summary(days))
