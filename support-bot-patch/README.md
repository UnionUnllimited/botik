# Патч бота поддержки: с ключевого VPN на роутеры

Здесь лежат изменённые файлы стороннего бота поддержки
(`~/vpn_support_bot`), переведённого с продажи ключей на роутеры.
Каталог временный — после выката его можно удалить.

Кода магазина патч не касается: он тут только чтобы забрать файлы на
сервер через `git pull`, без `scp`.

## Что изменено

**`app/keyboards.py`** — меню перестроено:

    📶 Мой роутер           было 🔑 Мой ключ
    💳 Подписка и оплата
    🛒 Покупка и доставка   новый раздел
    🛠 Роутер не работает   было 🛜 Работа VPN
    🤖 Проблема с ботом

Ссылка на подписку клиенту не показывается: в `mykey_main_keyboard`
стоит `sub_page_link = None`, кнопка «Страница подключения» не
собирается. Кнопка «Сбросить подписку» убрана.

**`app/main.py`** — переписана карта статей `FAQ_ARTICLES`, добавлен
раздел `m_shop`, из карточки убрана строка «Лимит устройств»
(у роутера его нет), заголовок стал «Мой роутер».

**`app/texts.py`** — девять текстов добавлено, четыре переписано,
девять удалено. Упоминаний Happ, v2rayTun, RabbitHole, Koala Clash и
«ключ-ссылки» не осталось.

**`app/ai_tools.py`** — добавлен инструмент `get_my_router`: ходит в
fleet API магазина и отдаёт состояние роутера клиента (на связи или
молчит, когда выходил, сколько устройств на Wi-Fi, прошивка, подписка).
Убран `get_my_devices` — он отдавал лимит устройств, которого у роутера
нет, и модель на этом врала бы.

**`admin_web/ai_settings.py`** — переписан системный промпт. Убран блок
про выдачу ссылки на подписку (выдавать нечего). Добавлены факты,
которые ассистент обязан знать: подписка на устройство, дни доставки не
сгорают, продление от даты окончания, кабель в WAN, обновление до
10 минут. Отдельно — запрет советовать сброс, перепрошивку и смену DNS.

**`env.example`** — две новые переменные (в боте это `.env.example`,
переименован, чтобы не прятался под точкой).

## Как применить

Забрать на сервере:

    cd /opt/router-shop && git pull --ff-only

Бэкап бота и базы:

    cd ~/vpn_support_bot && mkdir -p ../vpn_support_bot_backup_$(date +%F) \
      && cp -r app admin_web .env ../vpn_support_bot_backup_$(date +%F)/ \
      && cp data/vpn_support.db data/vpn_support.db.bak

Положить файлы:

    cd ~/vpn_support_bot \
      && cp /opt/router-shop/support-bot-patch/app/*.py app/ \
      && cp /opt/router-shop/support-bot-patch/admin_web/ai_settings.py admin_web/

Убрать перекрытия из базы. **Это обязательно**: все тексты засеяны в
`content_texts`, а прокси берёт базу раньше кода — без удаления новые
значения не появятся. Тombstones не создаём, поэтому на старте тексты
пересеются из нового `texts.py`:

    cd ~/vpn_support_bot && sqlite3 data/vpn_support.db \
      "DELETE FROM content_texts WHERE key IN ('WELCOME','PAY_TARIFFS',
       'BOT_PAID_NOT_WORK','VPN_MENU','VPN_HOW_CONNECT','VPN_NOT_WORKING',
       'VPN_HAPP_NA','VPN_WHERE_SUB','VPN_OTHER_DEVICE','VPN_PING',
       'VPN_ROUTER','BOT_NO_KEY','PAY_AUTORENEW');"

Проверить, не перекрыты ли ещё и меню:

    sqlite3 data/vpn_support.db "SELECT menu_name, count(*) FROM content_buttons GROUP BY menu_name;"
    sqlite3 data/vpn_support.db "SELECT kind, key FROM content_tombstones;"

Есть `main_menu`, `vpn_menu` или `pay_menu` — удалить, иначе старые
кнопки перекроют новое меню так же, как тексты.

Переменные и запуск:

    cd ~/vpn_support_bot \
      && printf 'FLEET_API_URL=<база fleet API>\nFLEET_API_TOKEN=<FLEET_TOKEN из /opt/router-shop/.env>\n' >> .env \
      && docker compose up -d --build

## Проверка

    sleep 20 && sqlite3 data/vpn_support.db "SELECT count(*) FROM content_texts;"
    sqlite3 data/vpn_support.db "SELECT substr(value,1,60) FROM content_texts WHERE key='WELCOME';"

Записей должно стать **30** вместо 34, а в `WELCOME` — текст про
роутеры. В боте: пять пунктов главного меню, четыре в разделе поломок,
и никакой кнопки «Страница подключения» в «Моём роутере».

Инструмент проверяется так:

    curl -s -H "Authorization: Bearer $FLEET_TOKEN" \
      "<база fleet API>/routers?tg_id=<ваш tg id>&per_page=1" | head -c 400

Вернулся JSON с роутером — связка работает. `401` — токен не тот,
`404` — токен в магазине не задан.

## Что осталось не сделано

`get_my_traffic` оставлен в схеме инструментов. Если на роутерных
тарифах лимита трафика нет — убрать его так же, как убран
`get_my_devices`, иначе ассистент будет отвечать на несуществующий
вопрос.

Инструменты `get_my_subscription` и `get_my_payments` по-прежнему ходят
в старую панель через `app/admin_panel.py`. В fleet API их не
переводил: там другая модель данных, и вслепую это проверить нельзя.
