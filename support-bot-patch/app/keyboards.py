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
    """Под ошибками раздела «Мой роутер».

    Раньше «Назад» вело в m_mykey — то есть в тот же экран, который и
    выдал ошибку. Сообщение не менялось, Telegram отклонял правку, и
    кнопка выглядела нерабочей. Теперь: повторить попытку или уйти.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="m_mykey")],
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

    # 1. Как включить роутер
    "qa_install": (
        "✅ <b>Как включить роутер</b>\n\n"
        "1️⃣ Вставьте кабель провайдера в порт <b>WAN</b> — "
        "он отдельного цвета и подписан на корпусе.\n"
        "2️⃣ Подключите блок питания и включите в розетку.\n"
        "3️⃣ Подождите 30–40 секунд, пока роутер загрузится.\n"
        "4️⃣ Подключитесь к Wi-Fi <b>Titan-2.4</b> или "
        "<b>Titan-5</b>.\n\n"
        "🔑 Панель роутера: <code>http://192.168.14.1</code>, "
        "логин и пароль <code>admin</code> / <code>admin</code>. "
        "Вводить в адресной строке браузера и через <b>http</b>, "
        "не https.\n\n"
        "❗️ <b>Wi-Fi есть, а интернета нет?</b> Кабель должен "
        "идти в <b>WAN</b>, а не в LAN. И если провайдер даёт "
        "интернет по логину с паролем (PPPoE), их надо ввести в "
        "панели → «Настройки интернета».\n\n"
        "📖 Подробнее: <code>http://titan.lan/instruction</code>"
    ),

    # 2. Сколько устройств можно подключить
    "qa_limit": (
        "📱 <b>Сколько устройств можно подключить</b>\n\n"
        "Лимита нет. К Wi-Fi роутера подключается сколько угодно "
        "техники: телефоны, телевизор, приставка, консоль, "
        "компьютеры — доступ работает на всём сразу.\n\n"
        "Настраивать каждое устройство отдельно не нужно."
    ),

    # 3. Второй роутер / доступ для близких
    "qa_friend": (
        "🔗 <b>Доступ для второго дома или близких</b>\n\n"
        "Подписка привязана к <b>конкретному роутеру</b>, а не к "
        "аккаунту. Передать или разделить её нельзя.\n\n"
        "Нужен доступ по другому адресу — потребуется второй "
        "роутер со своей подпиской, заказать можно в "
        "@{MAIN_BOT} → <b>Каталог</b>.\n\n"
        "💡 Внутри одной квартиры делиться не нужно: все, кто "
        "подключён к Wi-Fi роутера, уже пользуются доступом."
    ),

    # 4. Как продлить
    "qa_renew": (
        "🔄 <b>Как продлить подписку</b>\n\n"
        "В боте @{MAIN_BOT}: <b>Мой роутер</b> → <b>Продлить</b> → "
        "выберите срок → оплатите.\n\n"
        "Продление считается <b>от даты окончания</b>, а не от "
        "сегодняшнего дня — платить заранее можно спокойно, "
        "оставшиеся дни не сгорят.\n\n"
        "Подписка включается за несколько минут, роутер подхватит "
        "её сам. Перенастраивать ничего не нужно."
    ),

    # 5. Почему часть сайтов идёт напрямую
    "qa_server": (
        "🌍 <b>Почему российские сайты работают напрямую</b>\n\n"
        "Роутер разделяет трафик сам. Банки, Госуслуги, "
        "маркетплейсы и российские кинотеатры идут напрямую — "
        "поэтому банк не блокирует вход, а скорость на них "
        "остаётся вашей обычной.\n\n"
        "Через зарубежный сервер уходит только то, что иначе не "
        "открывается. Решение принимается для каждого сайта "
        "отдельно, переключать ничего не надо.\n\n"
        "Списки обновляются автоматически."
    ),

    # 6. Медленно работает
    "qa_slow": (
        "🐢 <b>Медленно работает</b>\n\n"
        "Сначала проверьте скорость на российском сайте, например "
        "на Яндексе: туда трафик идёт напрямую, мимо нашего "
        "сервера.\n\n"
        "• Медленно и там — дело в провайдере или Wi-Fi. Роутер "
        "<b>не ускоряет</b> интернет: тариф провайдера остаётся "
        "потолком скорости.\n"
        "• Российские сайты быстрые, а зарубежные медленные — "
        "напишите нам, посмотрим нагрузку сервера со своей "
        "стороны.\n\n"
        "💡 Wi-Fi на 5 ГГц заметно быстрее, чем на 2,4 ГГц, если "
        "устройство недалеко от роутера."
    ),

    # 7. Телевизор и приставка
    "qa_tv": (
        "\U0001F4FA <b>Телевизор, приставка, консоль</b>\n\n"
        "Устанавливать ничего не нужно. Подключите телевизор к "
        "Wi-Fi <b>Titan-2.4</b> или кабелем в порт <b>LAN</b>.\n\n"
        "\U0001F4A1 Именно <b>2.4</b>: многие телевизоры не видят "
        "сеть 5 ГГц.\n\n"
        "В этом и смысл роутера — он закрывает технику, куда "
        "приложение поставить нельзя.\n\n"
        "❗️ <b>Не работает YouTube на телевизоре?</b> В панели "
        "<code>http://192.168.14.1</code> → «🛡 VPN и сервер» → "
        "<b>«🏓 URL Test»</b> → выберите сервер с зелёным пингом → "
        "<b>«Применить»</b>. Затем выключите телевизор из розетки "
        "на 10 секунд.\n\n"
        "Нужен телевизор <b>без</b> VPN — подключите его к "
        "гостевой сети <b>Titan-Guest</b>, она идёт напрямую."
    ),

    # 8. Просьба сбросить роутер
    "qa_reset": (
        "\U0001F518 <b>Кнопка Reset — что она делает</b>\n\n"
        "• <b>Короткое нажатие</b> (меньше 3 секунд) — просто "
        "перезагрузка, все настройки сохраняются.\n"
        "• <b>Долгое</b> (3 секунды и дольше) — заводской сброс.\n\n"
        "✅ <b>Подписка сохраняется</b> — оплаченные дни никуда "
        "не денутся.\n\n"
        "⚠️ <b>А вот это сбросится:</b>\n"
        "• имя и пароль Wi-Fi → <b>Titan-2.4</b> / <b>Titan-5</b>, "
        "пароль <code>11111118</code>\n"
        "• пароль панели → <code>admin</code> / <code>admin</code>\n"
        "• <b>настройки подключения к провайдеру</b>\n"
        "• выбранный сервер — вернётся на автоматический\n"
        "• гостевая сеть и детский режим — удалятся\n\n"
        "❗️ Если интернет у вас по логину и паролю (PPPoE, L2TP, "
        "PPTP) или по статическому адресу — <b>приготовьте договор</b>, "
        "эти данные придётся ввести заново. Если провайдер выдаёт "
        "интернет сразу по кабелю, вводить ничего не нужно.\n\n"
        "Забыли пароль от панели — это как раз тот случай, когда "
        "долгое нажатие уместно."
    ),

    # 9. Технические работы
    "qa_tech": (
        "🛠 <b>Технические работы</b>\n\n"
        "Сейчас идут работы на сервере, поэтому зарубежные "
        "сервисы могут открываться с перебоями. Российские сайты "
        "работают как обычно — они идут напрямую.\n\n"
        "Делать ничего не нужно: роутер восстановит соединение "
        "сам. Не перезагружайте и не сбрасывайте его.\n\n"
        "Приносим извинения за неудобства."
    ),
}


def admin_quick_keyboard():
    """Меню авто-ответов оператора."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Как включить роутер", callback_data="qa_install")],
        [InlineKeyboardButton(text="💳 Как продлить подписку", callback_data="qa_renew")],
        [InlineKeyboardButton(text="📱 Сколько устройств", callback_data="qa_limit")],
        [InlineKeyboardButton(text="🔗 Второй роутер", callback_data="qa_friend")],
        [InlineKeyboardButton(text="⚠️ Не сбрасывайте роутер", callback_data="qa_reset")],
        [InlineKeyboardButton(text="🌍 Почему РФ-сайты напрямую", callback_data="qa_server")],
        [InlineKeyboardButton(text="🐢 Упала скорость", callback_data="qa_slow")],
        [InlineKeyboardButton(text="📺 Телевизор и приставка", callback_data="qa_tv")],
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