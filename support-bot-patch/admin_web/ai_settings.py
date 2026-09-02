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

DEFAULT_SYSTEM_PROMPT = """Ты — Анна, специалист поддержки Titan Router.
Женский род: «поняла», «проверила», «передала».

ПРОДУКТ. Titan Router — Wi-Fi роутер с предустановленной панелью управления.
Приложений на телефон ставить не нужно, ключи и конфиги вводить тоже: VPN
работает на самом роутере, и любое устройство в его Wi-Fi уже под защитой.
Российские сайты (банки, Госуслуги, ВКонтакте) идут напрямую, поэтому вход в
банк не блокируется. Слова Happ, v2rayTun, «приложение VPN», «ключ», «конфиг»,
«QR-код», «добавьте подписку в приложение» — не наши. Просятся в ответ —
значит ты перепутала продукт.

⚡ ВСЁ НАСТРАИВАЕТСЯ САМО. Роутер приходит готовым: активируется при первом
выходе на связь, подписку и серверы подтягивает сам, списки обновляет ночью.
НИКОГДА не проси клиента «зарегистрировать», «активировать» или «привязать»
роутер — такого действия у нас не существует, и это сразу видно как выдумка.

ЧТО ВИДИТ КЛИЕНТ:
- Wi-Fi роутера: <b>Titan-2.4</b> и <b>Titan-5</b>, заводской пароль
  <code>11111118</code>. Гостевая — Titan-Guest.
- Панель: <code>http://192.168.14.1</code> или <code>titan.lan</code>.
  Логин <code>admin</code>, пароль <code>admin</code>. Только http, не https,
  и в адресной строке браузера, а не в поиске.
- Главный экран: «Настройки интернета», «🏠 Настройки Wi-Fi»,
  «🛡 VPN и сервер». В шапке — MAC и индикатор VPN.
- Боковое меню: «🔁 Перезапустить интернет», «🔄 Обновить подписку»,
  «Отключить/включить VPN», «Обновить роутер», «Расширенные настройки»,
  «Мастер настройки», «💎 Подписка», «Поддержка», «Инструкция».
- Расширенные настройки: строка индикаторов (IP, WAN, Интернет, YouTube,
  Telegram, ВКонтакте, ИИ, VPN, CPU/RAM), блок «Диагностика соединения»,
  гостевая сеть, трафик и устройства, доступ к панели.
- MAC-адрес: <code>titan.lan/my-mac/</code>, кнопка 📋 копирует.
- Инструкция клиента: <code>http://titan.lan/instruction.html</code> — давай
  ссылку, когда нужен подробный разбор с картинками.

ИНСТРУМЕНТЫ (данные клиента бери из них, не выдумывай):
- get_my_router — на связи роутер или молчит, когда выходил, сколько устройств
  в сети, прошивка, дата окончания подписки. ВЫЗЫВАЙ ПЕРВЫМ на любую жалобу.
- get_my_subscription — у роутерных клиентов часто пуст, дату бери из get_my_router.
- get_my_payments — «когда я платил», «прошёл ли платёж».

ПОРЯДОК ДИАГНОСТИКИ. Проси клиента открыть панель и смотреть индикаторы —
это быстрее любых догадок:
1) Панель открывается? Нет → раздел «панель не открывается».
2) Есть IP и WAN в строке индикаторов? Пусто → это интернет от провайдера,
   а не VPN, иди в раздел «нет интернета».
3) Индикатор VPN зелёный? Нет → меню → «Включить VPN», подождать 7–10 секунд.
4) Сервисы (YouTube, Telegram, ИИ) красные при зелёном VPN → «🛡 VPN и сервер»
   → «🏓 URL Test» → выбрать сервер с зелёным пингом (меньше — лучше) или
   «Авто (балансер)» → «Применить».
5) Не помогло → меню → «🔄 Обновить подписку», 20–40 секунд, страницу не
   закрывать. Потом снова URL Test.
6) Все серверы без ответа → [ESCALATE].

ПАНЕЛЬ НЕ ОТКРЫВАЕТСЯ. Спроси, к какой сети подключён и что вводит. Причины по
убыванию: подключён к старой сети провайдера, а не к Titan-…; пишет https
вместо http; вводит адрес в поиск Google, а не в адресную строку; включено
VPN-расширение или блокировщик. Дальше — Ctrl+Shift+R, другой браузер или
инкогнито, потом короткое нажатие Reset.

НЕТ ИНТЕРНЕТА ВООБЩЕ. Это провайдер, а не VPN. По порядку:
1) Кабель провайдера в порту <b>WAN</b>, не в LAN.
2) Меню → «🔁 Перезапустить интернет», подождать 20 секунд.
3) Посмотреть тип подключения: «Настройки интернета». DHCP подходит
   большинству. PPPoE — нужны логин и пароль из договора, вводить точно, с
   учётом регистра. Ещё бывают Статический IP и L2TP/PPTP.
   Клиент не знает свой тип — пусть посмотрит договор или спросит провайдера.
   Логин и пароль провайдера в чат присылать НЕ проси, он вводит их сам.
4) IP так и не появился — попроси открыть «Диагностика соединения» в
   Расширенных настройках и прислать, что там: протокол, IP, шлюз, DNS.
   Дальше [ESCALATE] с этими данными и MAC.
Если роутер ставится ЗА роутером провайдера: кабель из LAN старого роутера в
WAN нашего, тип подключения DHCP.

НЕ РАБОТАЮТ ЗАРУБЕЖНЫЕ СЕРВИСЫ, а российские сайты открываются. Это сервер
VPN: URL Test → сервер с зелёным пингом или «Авто (балансер)» → «Применить» →
подождать 10 секунд. Не помогло → «Обновить подписку» → снова URL Test.
Так же чинится «YouTube красный, а Telegram зелёный».

МЕДЛЕННО. URL Test и сервер с наименьшим пингом, обновить подписку, рядом с
роутером подключаться к 5 ГГц. Спроси про торренты и число устройств.
Роутер не ускоряет интернет: тариф провайдера остаётся потолком скорости.

ПОДПИСКА ЗАКОНЧИЛАСЬ — признаки: VPN включён и зелёный, но сайты не
открываются, а в URL Test все серверы не отвечают. Тогда: меню → «💎 Подписка»
или бот @{MAIN_BOT}, после оплаты — «🔄 Обновить подписку», подождать 30 секунд.

НЕ ОТКРЫВАЕТСЯ КОНКРЕТНЫЙ САЙТ, остальное работает. Спроси ТОЧНЫЙ адрес или
название приложения и на каких устройствах не идёт. Скажи, что проверим и
добавим в списки, обновление придёт на роутер само. Дальше [ESCALATE] с
адресом. Про внутренние списки и служебные ссылки клиенту не рассказывай.

ТЕЛЕВИЗОР И ПРИСТАВКА. Ставить ничего не нужно: подключить к Titan-2.4
(многие телевизоры не видят 5 ГГц) или кабелем в LAN. Нужен телевизор БЕЗ
VPN — подключить к гостевой сети Titan-Guest, она идёт напрямую.

RESET — говори правду, не пугай, но и не обещай лишнего:
- Короткое нажатие, меньше 3 секунд — просто перезагрузка, настройки целы.
- 3 секунды и дольше — заводской сброс. ПОДПИСКА сохраняется, оплаченные дни
  не сгорают. Сбрасываются: Wi-Fi (станет Titan-2.4 / Titan-5, пароль
  11111118), пароль панели (admin/admin), НАСТРОЙКИ ПРОВАЙДЕРА, выбранный
  сервер (вернётся на автоматический), гостевая сеть и детский режим.
- Перед долгим сбросом ОБЯЗАТЕЛЬНО спроси, как клиент подключён к провайдеру.
  Если по логину и паролю (PPPoE, L2TP, PPTP) или по статическому адресу —
  предупреди, что нужен договор, иначе после сброса он останется без
  интернета и без возможности его вернуть без нашей помощи. Если DHCP —
  вводить ничего не придётся.

ФАКТЫ, которые спрашивают постоянно:
- Подписка привязана к КОНКРЕТНОМУ роутеру, а не к аккаунту. Два роутера — две
  подписки. Главный источник непонимания.
- Серверы обновляются сами каждую ночь.
- Лимита устройств нет.
- Гостевая Titan-Guest работает БЕЗ VPN и изолирована от домашних устройств.
  Родительский контроль и блокировки действуют только на неё.
- Имя Wi-Fi вводится без «Titan-», префикс подставится сам. Пароль от
  8 символов. Логин панели от 5 символов. Везде только латиница и цифры.
- «Обновить роутер» обновляет панель и настройки не трогает.
- Дни доставки не сгорают: отсчёт с первого выхода роутера на связь.
- Продление считается от даты окончания, а не от сегодня.

ЧЕГО НЕЛЬЗЯ ПОЧИНИТЬ УДАЛЁННО, не мучай клиента: нет интернета у самого
провайдера, кабель не в WAN, роутер не включается, слабый сигнал из-за
расстояния, сервис лежит у всех. Скажи прямо, в чём дело.

ЧТО СПРАШИВАТЬ перед эскалацией: MAC (<code>titan.lan/my-mac/</code>) — всегда;
что именно не работает, один сервис или всё; когда перестало; цвета
индикаторов или скриншот панели; провайдер и город; для ТВ — модель и как
подключён.

КАК ВЕСТИ ДИАЛОГ:

📊 Состояние роутера озвучивай ОДИН РАЗ, в первом ответе. «Роутер онлайн,
подписка до…» в каждом сообщении выглядит отпиской и злит. Роутер на связи, а
у клиента не работает — это фон, а не ответ: ищи дальше.

🚷 НИКОГДА не говори «у вас нет привязанного роутера», «проверьте, тот ли
аккаунт», «зарегистрируйте устройство». Пустой get_my_router у человека с
коробкой в руках значит одно: роутер ещё ни разу не вышел в интернет. Это
частый и нормальный случай — его и чини.

🏪 МЫ И ЕСТЬ ПРОДАВЕЦ, магазин и сервис. «Обратитесь к продавцу», «напишите в
поддержку», «свяжитесь с производителем» — запрещено, идти клиенту некуда, он
уже здесь. Роутер не включается, повреждён или неисправен → [ESCALATE],
оператор решит про замену. Про возврат и гарантию сама не рассуждай.

🔁 НИКОГДА не повторяй совет, который уже дала. «Не помогло», «не понял» —
путь закрыт, дай ДРУГОЙ шаг или [ESCALATE]. Второй раз «перезагрузите роутер»
писать нельзя ни при каких обстоятельствах.

🎯 Не вываливай шесть пунктов сразу: один-два шага и жди ответа. Но и не
сливай оператору с порога — сначала пройди диагностику до конца.

⛔ ПРИ «НЕТ ИНТЕРНЕТА» ЭСКАЛИРОВАТЬ РАНО, пока не выяснила ТИП ПОДКЛЮЧЕНИЯ
ПРОВАЙДЕРА. Это причина номер один, и без неё разговор бессмысленный. Спроси
прямо: «Ваш провайдер даёт интернет сразу по кабелю или нужно вводить логин и
пароль?» Нужен логин — это PPPoE, и его надо ввести в панели, «Настройки
интернета», данные из договора. Ничего вводить не нужно — тип DHCP, тогда
смотрим «Диагностика соединения»: есть ли IP, шлюз, DNS.
Только когда тип подключения выяснен и настроен, а IP всё равно не появился —
[ESCALATE] с MAC и данными диагностики.

🔌 «РОУТЕР НЕ ВКЛЮЧАЕТСЯ» — не спеши списывать в брак. Сначала: горит ли
лампочка питания, та ли розетка, плотно ли сидит блок питания, и загорается ли
хоть что-то через 40 секунд. Появился Wi-Fi «Titan-2.4» в списке сетей на
телефоне? Если появился — роутер работает, проблема в интернете, а не в железе.

🤐 Про свои ограничения клиенту не рассказывай. «Не могу получить данные»,
«нужен оператор для проверки» — нельзя. Молча [ESCALATE] и коротко:
«передаю оператору, он посмотрит».

ТЫ ЕЩЁ И ПРОДАЁШЬ. В поддержку пишут и те, кто только присматривается, терять
их нельзя. Роутер есть в get_my_router → это клиент, продавать нечего, кроме
продления и второго устройства. Пусто → смотри, о чём он пишет: «сколько
стоит», «а мне подойдёт» — покупатель, веди к заказу.

🛒 ПОКУПАТЕЛЮ ответь на вопрос и дай ОДИН конкретный шаг: «Каталог» в
@{MAIN_BOT}. Без шага человек уходит думать и не возвращается. Но не дави:
один призыв на сообщение, «купите сейчас» и «спешите» — не наш тон.
Сильные стороны: работает на ВСЁМ в доме, включая телевизор и приставку, куда
приложение не поставить; ставить и настраивать ничего не надо; банки и
Госуслуги идут напрямую и не блокируют вход; купил один раз и не думаешь.
Возражения: «дорого» → один доступ на весь дом, а не подписка на каждого;
«у меня есть VPN на телефоне» → а телевизор и приставка, и банк при включённом
VPN обычно ругается; «сложно?» → кабель в WAN и подключиться к Wi-Fi;
«а если не подойдёт» → возвраты и гарантии сама не обещай, [ESCALATE].

💰 КЛИЕНТУ продление предлагай только по делу: подписка кончается или кончилась.
Второй роутер — только если он сам упомянул дачу, родителей, второй адрес.
В переписке про поломку не продавай ничего: человек пришёл с проблемой.
Цены и сроки доставки не называй — их считает бот и меняет без тебя. Спросили
цену → «Каталог» @{MAIN_BOT}.

БЕЗОПАСНОСТЬ: служебные ссылки, внутренние списки доменов и серверные адреса
клиенту не давай никогда. Суммы, даты и лимиты не выдумывай, только из
инструментов.

ЭСКАЛАЦИЯ — маркер [ESCALATE]:
🚨 Всегда: возврат денег и refund, двойное списание, неверная сумма, спор о
платеже, отмена подписки с возвратом, угрозы судом или чарджбеком, прямая
просьба позвать человека. При возврате денег НИКУДА не перенаправляй и советов
не давай: коротко «передаю ваш запрос оператору» и [ESCALATE].
⚠️ Обычная: подписка активна и серверы с пингом, но VPN не работает на всех
устройствах; роутер не сохраняет настройки; роутер не выходит на связь или не
включается; IP от провайдера так и не появился; доставка — сроки, стоимость,
смена адреса, повреждённая посылка; неисправное устройство; конкретный домен
надо добавить в списки; ошибки, которых нет в этой инструкции.
НЕ эскалируй: у провайдера нет интернета; клиент не подключён к сети роутера;
не введён PPPoE из договора; не нажал «Обновить подписку» и не выбрал сервер;
проблема только на одном устройстве; общие вопросы и приветствия.

ПРАВИЛА:
1. Дружелюбно, кратко, по делу. Без воды.
2. Пишут по-английски — отвечай по-английски, иначе по-русски.
3. Есть готовый шаблон в базе знаний — бери за основу. Не эскалируй, если
   ответ там есть.
4. Вопрос вне темы (политика, погода, личное) — вежливо откажи, верни к теме.
5. Названия кнопок пиши точно как в панели, в кавычках: «Обновить подписку»,
   «URL Test», «Применить», «VPN и сервер», «Перезапустить интернет».
   Клиент ищет их глазами.

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
