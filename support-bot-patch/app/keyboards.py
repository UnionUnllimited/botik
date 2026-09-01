import os as _os

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


# ============================================================
#  Брендовые имена и ссылки — берутся из .env, чтобы их можно
#  было менять без правки кода. См. также app/texts.py.
# ============================================================

MAIN_BOT = _os.getenv("MAIN_BOT_USERNAME", "your_vpn_bot").lstrip("@").strip()
COMMUNITY_CHAT_URL = _os.getenv(
    "COMMUNITY_CHAT_URL", "https://t.me/your_vpn_chat",
).strip()
NEWS_CHANNEL_URL = _os.getenv(
    "NEWS_CHANNEL_URL", "https://t.me/your_vpn_news",
).strip()


# ============================================================
#  ГЛАВНОЕ МЕНЮ (для клиента)
# ============================================================

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📶 Мой роутер", callback_data="m_mykey")],
        [InlineKeyboardButton(text="💳 Подписка и оплата", callback_data="m_pay")],
        [InlineKeyboardButton(text="🛒 Покупка и доставка", callback_data="m_shop")],
        [InlineKeyboardButton(text="🛠 Роутер не работает", callback_data="m_vpn")],
        [InlineKeyboardButton(text="🤖 Проблема с ботом", callback_data="m_bot")],
        [InlineKeyboardButton(text="💬 Спросить в нашем чате", callback_data="m_community")],
        [InlineKeyboardButton(text="✉️ Связаться с поддержкой", callback_data="contact_support")],
    ])


# ============================================================
#  РАЗДЕЛ «🔑 МОЙ КЛЮЧ»
# ============================================================

def mykey_main_keyboard(sub_page_link: str | None):
    """
    Главная (и единственная) клавиатура раздела «Мой ключ».
    Включает URL-кнопку на страницу подключения, если ссылка известна.

    [v3.5] Учитывает скрытие кнопок из virtual_button_hidden (через
    content_cache). Оператор может скрыть кнопки через UI редактора.
    """
    try:
        from app import content_cache
        is_hidden = content_cache.is_virtual_button_hidden_cached
    except Exception:
        is_hidden = lambda menu, val: False

    rows = []
    # Ссылка на подписку клиенту не показывается ни при каких условиях:
    # у роутера её нет, а по ключевым тарифам выдаёт только оператор.
    sub_page_link = None
    if sub_page_link and not is_hidden("mykey_menu", "sub_page_link"):
        rows.append([InlineKeyboardButton(text="🌐 Страница подключения", url=sub_page_link)])
    if not is_hidden("mykey_menu", "back_main"):
        rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mykey_back_keyboard():
    """Под секциями (сброс, ошибки) — вернуть в «Мой ключ» и в главное меню."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="m_mykey")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")],
    ])


def mykey_reset_confirm_keyboard():
    """Подтверждение запроса сброса (создание тикета оператору)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, отправить запрос", callback_data="mk_reset_send")],
        [InlineKeyboardButton(text="⬅️ Назад",       callback_data="m_mykey")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")],
    ])


# ============================================================
#  ПОДМЕНЮ
# ============================================================

def pay_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Сроки и цены", callback_data="a_pay_tariffs")],
        [InlineKeyboardButton(text="🔄 Как продлить", callback_data="a_pay_renew")],
        [InlineKeyboardButton(text="⚠️ Оплатил, но не включилось", callback_data="a_bot_paid_not_work")],
        [InlineKeyboardButton(text="❌ Не открывается страница оплаты", callback_data="a_pay_no_page")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
    ])


def bot_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📭 Подписка не отображается", callback_data="a_bot_no_sub")],
        [InlineKeyboardButton(text="🧱 Кнопка или меню не работает", callback_data="a_bot_broken")],
        [InlineKeyboardButton(text="🐢 Бот долго отвечает", callback_data="a_bot_slow")],
        [InlineKeyboardButton(text="⚠️ Ошибка после оплаты", callback_data="a_bot_pay_err")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
    ])


def vpn_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔌 Как включить роутер", callback_data="a_vpn_how")],
        [InlineKeyboardButton(text="🌐 Не открывается сайт", callback_data="a_vpn_site")],
        [InlineKeyboardButton(text="🛰 Не работают зарубежные сервисы", callback_data="a_vpn_not_work")],
        [InlineKeyboardButton(text="📵 Совсем нет интернета", callback_data="a_vpn_no_net")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
    ])


def shop_menu():
    """Покупка, доставка и объяснение продукта — раздела не было."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ℹ️ Чем отличается наш роутер", callback_data="a_shop_why")],
        [InlineKeyboardButton(text="⚙️ Как это работает", callback_data="a_shop_how")],
        [InlineKeyboardButton(text="🛒 Как купить", callback_data="a_shop_buy")],
        [InlineKeyboardButton(text="🚚 Доставка", callback_data="a_shop_delivery")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
    ])


def community_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👉 Перейти в чат", url=COMMUNITY_CHAT_URL)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
    ])


# ============================================================
#  Клавиатура под каждой статьёй FAQ (для клиента)
# ============================================================

def article_keyboard(back_to: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Связаться с поддержкой", callback_data="contact_support")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"m_{back_to}")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_main")],
    ])


# ============================================================
#  ШАПКА ТИКЕТА В ТОПИКЕ ПОДДЕРЖКИ
# ============================================================

def ticket_admin_keyboard(user_id: int, admin_url: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛠 Открыть в админке", url=f"{admin_url}{user_id}")],
        [
            InlineKeyboardButton(text="🔒 Закрыть", callback_data="close_ticket"),
            InlineKeyboardButton(text="🤫 Тихо", callback_data="close_ticket_silent"),
            InlineKeyboardButton(text="⛔️ Бан", callback_data="ban_user"),
        ],
        [
            InlineKeyboardButton(text="🔄 Обновить инфу", callback_data=f"refresh_info:{user_id}"),
            InlineKeyboardButton(text="📋 Авто-ответы", callback_data="admin_quick"),
        ],
        [InlineKeyboardButton(text="🛠 Админка", callback_data=f"admin_panel_open:{user_id}")],
    ])


# ============================================================
#  ШАПКА ВЕБ-ТИКЕТА ОТ ГОСТЯ (не авторизованного — без TG user_id)
#  Только кнопки, которые работают по visitor_id веб-чата:
#  закрыть / тихо / бан / авто-ответы.
#  Кнопки требующие TG user_id (продлить подписку, обновить инфу) скрыты.
# ============================================================

def web_guest_keyboard():
    """[v3.5] Шапка тикета для веб-гостя (не авторизован — без TG user_id).
    Кнопки: Закрыть / Тихо / Авто-ответы.
    Кнопки требующие user_id (Открыть в админке, Обновить инфу, Админка,
    Бан) скрыты — для анонимного гостя они не работают.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔒 Закрыть", callback_data="close_ticket"),
            InlineKeyboardButton(text="🤫 Тихо", callback_data="close_ticket_silent"),
        ],
        [InlineKeyboardButton(text="📋 Авто-ответы", callback_data="admin_quick")],
    ])


# ============================================================
#  ПАНЕЛЬ ДЕЙСТВИЙ ПОСЛЕ КАЖДОГО ОТВЕТА ОПЕРАТОРА
#  (приходит в топик после того, как оператор написал клиенту)
# ============================================================

def admin_action_panel(user_id: int, admin_url: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛠 Открыть в админке", url=f"{admin_url}{user_id}")],
        [
            InlineKeyboardButton(text="🔒 Закрыть", callback_data="close_ticket"),
            InlineKeyboardButton(text="🤫 Тихо", callback_data="close_ticket_silent"),
            InlineKeyboardButton(text="⛔️ Бан", callback_data="ban_user"),
        ],
        [
            InlineKeyboardButton(text="🔄 Обновить инфу", callback_data=f"refresh_info:{user_id}"),
            InlineKeyboardButton(text="📋 Авто-ответы", callback_data="admin_quick"),
        ],
        [InlineKeyboardButton(text="🛠 Админка", callback_data=f"admin_panel_open:{user_id}")],
    ])


# ============================================================
#  ПОДМЕНЮ «🛠 Админка» — изменяющие действия
#  (открывается отдельным сообщением с полной инфой о клиенте)
# ============================================================

def admin_panel_keyboard(user_id: int, admin_url: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть в браузере", url=f"{admin_url}{user_id}")],
        [
            InlineKeyboardButton(text="➕ Продлить",  callback_data=f"sub_extend:{user_id}"),
            InlineKeyboardButton(text="➖ Уменьшить", callback_data=f"sub_reduce:{user_id}"),
        ],
        [InlineKeyboardButton(text="🔁 Сброс подписки", callback_data=f"revoke_sub:{user_id}")],
        [InlineKeyboardButton(text="🔄 Обновить инфу", callback_data=f"admin_panel_open:{user_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_hide")],
    ])


def confirm_extend_keyboard(user_id: int, days: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"✅ Продлить на {days} дн.",
                                 callback_data=f"do_extend:{user_id}:{days}"),
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_hide")],
    ])


def confirm_reduce_keyboard(user_id: int, days: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"⚠️ Уменьшить на {days} дн.",
                                 callback_data=f"do_reduce:{user_id}:{days}"),
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_hide")],
    ])


# ============================================================
#  АВТО-ОТВЕТЫ ОПЕРАТОРА
# ============================================================

ADMIN_ANSWERS = {

    # 1. Как подключить или скачать VPN
    "qa_install": (
        "✅ <b>Как подключить или скачать VPN?</b>\n\n"
        "Подключение занимает буквально пару минут:\n\n"
        "1️⃣ Нажмите кнопку <b>⚡ Подключиться</b> в личном кабинете "
        "бота @{MAIN_BOT}.\n"
        "2️⃣ Выберите вашу операционную систему "
        "(iOS, Android, Windows, macOS, Android TV, Apple TV).\n"
        "3️⃣ Скачайте приложение <b>HAPP</b> по предложенной ссылке "
        "(она откроется в App Store / Google Play / на сайте).\n"
        "4️⃣ Вернитесь в бот и нажмите <b>«Добавить ключ»</b> — "
        "подписка автоматически появится в приложении.\n"
        "5️⃣ Откройте приложение HAPP и нажмите кнопку подключения.\n\n"
        "🎉 Готово — VPN работает!\n\n"
        "💡 Если что-то пошло не так — перезапустите приложение "
        "и попробуйте подключиться к другому серверу."
    ),

    # 2. Как увеличить лимит устройств
    "qa_limit": (
        "📱 <b>Как увеличить лимит устройств?</b>\n\n"
        "На данный момент такой возможности нет — лимит устройств "
        "<b>зафиксирован для каждого тарифа</b>.\n\n"
        "💡 <b>Что можно сделать:</b>\n"
        "• Удалите неиспользуемые устройства в "
        "<b>Личный кабинет → 💻 Мои устройства</b> — слоты "
        "сразу освободятся.\n"
        "• Перейдите на более старший тариф — там слотов больше.\n\n"
        "📡 <b>Совет:</b> если нужно подключить много устройств "
        "одновременно (телевизор, консоль, ПК, телефоны) — "
        "обратите внимание на наш <b>Роутер с подпиской</b>. "
        "При его использовании лимиты отключаются полностью, "
        "и VPN работает на всём, что подключено к Wi-Fi.\n\n"
        "Подробнее о роутере — спросите у поддержки."
    ),

    # 3. Подключить друга к своему VPN
    "qa_friend": (
        "🔗 <b>Как подключить друга к своему VPN?</b>\n\n"
        "Поделиться доступом очень просто:\n\n"
        "1️⃣ В @{MAIN_BOT} нажмите <b>🔗 «Подключить устройство»</b> "
        "(или <b>«Поделиться подпиской»</b>).\n"
        "2️⃣ Скопируйте ссылку подписки или покажите QR-код.\n"
        "3️⃣ Передайте ссылку или QR другу — любым удобным способом.\n"
        "4️⃣ Друг открывает её в приложении <b>HAPP</b> и пользуется VPN.\n\n"
        "⚠️ <b>Учтите:</b>\n"
        "• Друг будет занимать одно из ваших устройств в лимите подписки.\n"
        "• Если друг отключится — слот не освободится автоматически, "
        "удалите устройство в <b>Личный кабинет → 💻 Мои устройства</b>.\n"
        "• Не выкладывайте ссылку публично — её могут перехватить, "
        "после чего ваш аккаунт может быть заблокирован."
    ),

    # 4. Как продлить подписку
    "qa_renew": (
        "💳 <b>Как продлить подписку?</b>\n\n"
        "Продление занимает меньше минуты:\n\n"
        "1️⃣ В @{MAIN_BOT} нажмите <b>💰 «Выбрать тариф»</b> "
        "или <b>«Продлить»</b>.\n"
        "2️⃣ Выберите удобный период подписки (1, 3, 6 или 12 месяцев — "
        "чем длиннее период, тем выгоднее).\n"
        "3️⃣ Оплатите любым удобным способом (карта, СБП, крипта).\n"
        "4️⃣ Дни автоматически добавятся к вашему сроку — "
        "ничего перенастраивать не нужно.\n\n"
        "✨ <b>Важно:</b> при продлении заранее ваши дни "
        "<b>не сгорают</b> — они суммируются с новым периодом. "
        "Так что выгодно продлевать пораньше, не дожидаясь "
        "окончания подписки."
    ),

    # 5. Как переключить сервер (страну)
    "qa_server": (
        "🌍 <b>Как переключить сервер (страну)?</b>\n\n"
        "Сменить локацию можно прямо в приложении HAPP:\n\n"
        "1️⃣ Откройте приложение <b>HAPP</b>.\n"
        "2️⃣ Зайдите в <b>список серверов</b> (вкладка с глобусом "
        "или меню «Серверы»).\n"
        "3️⃣ Выберите нужную страну из доступных в списке.\n"
        "4️⃣ Нажмите <b>«Подключиться»</b>.\n\n"
        "🚀 Все наши сервера работают на высоких скоростях — "
        "выбирайте любой.\n\n"
        "💡 <b>Советы по выбору:</b>\n"
        "• Для скорости — выбирайте сервер ближе к вам "
        "географически (Нидерланды, Германия, Финляндия).\n"
        "• Для стриминга — пробуйте Нидерланды или США.\n"
        "• Если на одном сервере медленно — переключитесь на "
        "другой, нагрузка распределяется неравномерно."
    ),

    # 6. Упала скорость
    "qa_slow": (
        "🐢 <b>Что делать, если упала скорость?</b>\n\n"
        "Попробуйте по порядку:\n\n"
        "1️⃣ <b>Переключитесь на другой сервер</b> в приложении HAPP — "
        "это решает проблему в 80% случаев.\n"
        "2️⃣ <b>Перезапустите приложение HAPP</b> — полностью закройте "
        "и откройте снова.\n"
        "3️⃣ <b>Перезагрузите Wi-Fi или мобильный интернет</b> — "
        "включите / выключите авиарежим или переподключитесь к сети.\n"
        "4️⃣ <b>Проверьте скорость без VPN</b> — зайдите на "
        "yandex.ru/internet с выключенным VPN. Если скорость и без "
        "VPN низкая — проблема у провайдера, а не у нас.\n\n"
        "💡 <b>Дополнительно:</b>\n"
        "• В настройках HAPP установите <b>Пинг → TCP</b> — увидите "
        "реальный пинг до серверов и сможете выбрать самый быстрый.\n"
        "• Включите <b>фрагментирование</b> в настройках HAPP, если "
        "оператор активно блокирует VPN.\n\n"
        "Если ничего не помогло — пришлите название сервера, "
        "к которому подключаетесь, и результаты замера скорости."
    ),

    # 7. Smart TV
    "qa_tv": (
        "📺 <b>Будет ли работать на Smart TV?</b>\n\n"
        "Да! Есть два варианта:\n\n"
        "1️⃣ <b>Прямая установка приложения HAPP на Android TV</b> — "
        "подходит для телевизоров на Android (Sony, Xiaomi, "
        "большинство современных моделей).\n"
        "   • Откройте Google Play на ТВ → найдите HAPP → установите.\n"
        "   • Добавьте ключ подписки через @{MAIN_BOT}.\n\n"
        "2️⃣ <b>Через наш роутер</b> — самый удобный вариант для "
        "<b>всех</b> телевизоров: Apple TV, LG WebOS, Samsung Tizen, "
        "старые модели без Android.\n"
        "   • С роутером VPN автоматически работает на всех "
        "устройствах в сети — телевизорах, консолях, телефонах.\n"
        "   • Лимит устройств снимается.\n"
        "   • Ничего настраивать на самом ТВ не нужно.\n\n"
        "🏠 Роутер — лучший способ для дома, если у вас "
        "много устройств или несколько ТВ.\n\n"
        "По вопросам роутера — спросите у поддержки."
    ),

    # 8. Отключить друга / лишнее устройство
    "qa_reset": (
        "🔄 <b>Как отключить лишнее устройство (друга)?</b>\n\n"
        "1️⃣ Зайдите в бот @{MAIN_BOT} и нажмите "
        "<b>«Личный кабинет»</b>.\n"
        "2️⃣ Откройте раздел <b>💻 «Мои устройства»</b>.\n"
        "3️⃣ В списке выберите лишнее устройство "
        "(друга или старое своё) и нажмите <b>«Удалить»</b>.\n\n"
        "После удаления слот сразу освободится — можно "
        "подключить другое устройство по своему ключу.\n\n"
        "💡 <b>Полезно знать:</b>\n"
        "• У друга после удаления VPN перестанет работать "
        "в течение 1–2 минут — это нормально, ключ обновляется "
        "на сервере.\n"
        "• Если друг попробует подключиться по старой ссылке — "
        "у него ничего не выйдет, нужно отправить новое "
        "приглашение через <b>«Поделиться подпиской»</b>.\n"
        "• Если в списке устройств вы не узнаёте ни одно — "
        "удалите все и переподключите свои устройства заново. "
        "Это защитит вашу подписку.\n\n"
        "❗️ Никогда не передавайте ключ-ссылку посторонним — "
        "только через кнопку <b>«Поделиться подпиской»</b>."
    ),

    # 9. Тех. работы
    "qa_tech": (
        "⚙️ <b>Технические работы</b>\n\n"
        "Здравствуйте! В данный момент ведутся технические работы. "
        "Скоро всё закончим — приносим извинения за неудобства.\n\n"
        "📰 Следите за новостями и обновлениями:\n"
        "{NEWS_CHANNEL_URL}\n\n"
        "После окончания работ перезапустите приложение HAPP — "
        "VPN снова заработает автоматически."
    ),
}


def admin_quick_keyboard():
    """Меню авто-ответов оператора."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Как подключить VPN", callback_data="qa_install")],
        [InlineKeyboardButton(text="💳 Как продлить подписку", callback_data="qa_renew")],
        [InlineKeyboardButton(text="📱 Лимит устройств", callback_data="qa_limit")],
        [InlineKeyboardButton(text="🔗 Подключить друга", callback_data="qa_friend")],
        [InlineKeyboardButton(text="🔄 Отключить друга / устройство", callback_data="qa_reset")],
        [InlineKeyboardButton(text="🌍 Сменить сервер (страну)", callback_data="qa_server")],
        [InlineKeyboardButton(text="🐢 Упала скорость", callback_data="qa_slow")],
        [InlineKeyboardButton(text="📺 VPN на Smart TV", callback_data="qa_tv")],
        [InlineKeyboardButton(text="⚙️ Тех. работы", callback_data="qa_tech")],
        [
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_main"),
            InlineKeyboardButton(text="🔙 Скрыть", callback_data="admin_hide"),
        ],
    ])


def confirm_send_keyboard(user_id: int, answer_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Отправить пользователю",
            callback_data=f"send|{user_id}|{answer_key}"
        )],
        [InlineKeyboardButton(text="🔙 К списку ответов", callback_data="admin_quick")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_main")],
    ])


# ============================================================
#  Автоподстановка значений в ADMIN_ANSWERS — заменяет
#  плейсхолдеры {MAIN_BOT} / {NEWS_CHANNEL_URL} / {COMMUNITY_CHAT_URL}
#  на реальные значения из .env. Делается один раз при импорте.
# ============================================================

def _apply_links() -> None:
    _links = {
        "MAIN_BOT": MAIN_BOT,
        "COMMUNITY_CHAT_URL": COMMUNITY_CHAT_URL,
        "NEWS_CHANNEL_URL": NEWS_CHANNEL_URL,
    }
    for _key, _val in list(ADMIN_ANSWERS.items()):
        if not isinstance(_val, str) or "{" not in _val:
            continue
        if any(("{" + k + "}") in _val for k in _links):
            try:
                ADMIN_ANSWERS[_key] = _val.format(**_links)
            except (KeyError, IndexError, ValueError):
                pass


_apply_links()