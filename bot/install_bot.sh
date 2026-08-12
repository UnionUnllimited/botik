#!/bin/bash
set -e

# Установщик бота и веб-админки.
#
# Публикацией наружу он не занимается: сервер отдаёт домен обратным прокси
# (у нас Caddy, порядок в README, раздел 5в). Прежняя версия ставила nginx
# и просила сертификат через `certbot --standalone`, а тот занимает порт 80 —
# на сервере с работающим прокси это гасит сайт целиком. Вместе с nginx ушли
# и три сервиса, которых больше нет: xuiweb, subpage, website.

# Проверка запуска из /root/bot/
if [[ "$PWD" != "/root/bot" ]]; then
  echo "Скрипт должен запускаться из /root/bot/! Текущая папка: $PWD"
  exit 1
fi

# Ждём освобождения dpkg/apt lock (unattended-upgrades и др.)
wait_apt_lock() {
  local i=0
  while fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock /var/cache/apt/archives/lock >/dev/null 2>&1; do
    if [ $i -eq 0 ]; then
      echo "Ожидание освобождения apt lock (фоновые обновления)..."
    fi
    sleep 3
    i=$((i+1))
    if [ $i -ge 40 ]; then
      echo "apt lock занят слишком долго, завершаем unattended-upgrades..."
      systemctl stop unattended-upgrades 2>/dev/null || true
      killall unattended-upgrades 2>/dev/null || true
      sleep 2
      break
    fi
  done
}

# Установка Python, pip и sqlite3
wait_apt_lock
apt update
wait_apt_lock
apt install -y python3 python3-venv python3-pip sqlite3

# ── Секретный путь веб-админки ────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Секретный путь веб-админки"
echo "  (используется в URL: https://домен/<путь>/)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

ADMIN_PATH_CURRENT=""
if [ -f "/root/bot/vpn_bot.db" ]; then
  ADMIN_PATH_CURRENT=$(sqlite3 /root/bot/vpn_bot.db "SELECT value FROM settings WHERE key='admin_secret_path' LIMIT 1;" 2>/dev/null || true)
fi
ADMIN_PATH_DEFAULT="${ADMIN_PATH_CURRENT:-admin123}"

read -rp "  Введите секретный путь [по умолчанию: $ADMIN_PATH_DEFAULT]: " ADMIN_PATH_INPUT
ADMIN_SECRET_PATH="${ADMIN_PATH_INPUT:-$ADMIN_PATH_DEFAULT}"
# Убираем слэши по краям
ADMIN_SECRET_PATH="${ADMIN_SECRET_PATH#/}"
ADMIN_SECRET_PATH="${ADMIN_SECRET_PATH%/}"
echo "  Секретный путь: /$ADMIN_SECRET_PATH/"

# ── Домен ─────────────────────────────────────────────────────────────────────
# Нужен только для настроек в БД: сертификат и публикацию делает прокси.
echo ""
read -rp "Введите домен, по которому сервис виден снаружи (или Enter чтобы пропустить): " DOMAIN

if [ -n "$DOMAIN" ]; then
  if [ -f "/root/bot/vpn_bot.db" ]; then
    sqlite3 /root/bot/vpn_bot.db \
      "INSERT INTO settings (key, value) VALUES ('connect_page_url', 'https://$DOMAIN')
       ON CONFLICT(key) DO UPDATE SET value = excluded.value;
       INSERT INTO settings (key, value) VALUES ('admin_secret_path', '$ADMIN_SECRET_PATH')
       ON CONFLICT(key) DO UPDATE SET value = excluded.value;"
    echo "connect_page_url → https://$DOMAIN"
    echo "admin_secret_path → /$ADMIN_SECRET_PATH/"
  else
    echo "БД не найдена, пропускаю обновление настроек домена."
  fi
fi

echo "\nУстановка зависимостей Python..."
# Создание venv для бота
if [ ! -d "venv" ]; then
  python3 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip
pip install -r service/requirements.txt
deactivate

# Создание venv для web_admin
if [ ! -d "web_admin/venv" ]; then
  python3 -m venv web_admin/venv
fi
source web_admin/venv/bin/activate
pip install --upgrade pip
pip install -r service/requirements.txt
deactivate

echo "\nУстановка завершена!"

# ── Настройка токена бота ──────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Настройка токена Telegram-бота"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

BOT_TOKEN_SET=""
if [ -f "/root/bot/vpn_bot.db" ]; then
  BOT_TOKEN_SET=$(sqlite3 /root/bot/vpn_bot.db "SELECT value FROM settings WHERE key='bot_token' LIMIT 1;" 2>/dev/null || true)
fi

if [ -n "$BOT_TOKEN_SET" ]; then
  # Показываем замаскированный токен (первые 10 символов + ***)
  MASKED="${BOT_TOKEN_SET:0:10}***"
  echo "  Токен уже установлен: $MASKED"
  read -rp "  Изменить токен? [y/N]: " change_token
  if [[ "$change_token" =~ ^[Yy]$ ]]; then
    BOT_TOKEN_SET=""
  fi
fi

if [ -z "$BOT_TOKEN_SET" ]; then
  echo ""
  echo "  Получить токен можно у @BotFather в Telegram."
  read -rp "  Введите токен бота (или Enter чтобы пропустить): " BOT_TOKEN_INPUT
  if [ -n "$BOT_TOKEN_INPUT" ]; then
    if [ -f "/root/bot/vpn_bot.db" ]; then
      sqlite3 /root/bot/vpn_bot.db \
        "INSERT INTO settings (key, value) VALUES ('bot_token', '$BOT_TOKEN_INPUT')
         ON CONFLICT(key) DO UPDATE SET value = excluded.value;"
      echo "  Токен бота сохранён."
    else
      echo "  БД не найдена, токен не сохранён. Укажите его в веб-админке после запуска."
    fi
  else
    echo "  Пропущено. Укажите токен бота в веб-админке после запуска."
    echo "  ⚠️  Внимание: без токена сервис бота будет запущен, но работать не сможет"
    echo "      и может создавать лишнюю нагрузку на сервер."
  fi
fi

echo "\nНастраиваю автозапуск сервисов..."
cp service/vpn-bot.service /etc/systemd/system/
cp service/vpn-webadmin.service /etc/systemd/system/
systemctl daemon-reload
# Сначала бот — базу создаёт он. Веб-админка только читает и на пустом месте падает.
systemctl enable --now vpn-bot.service
sleep 5
systemctl enable --now vpn-webadmin.service
echo "\nБот и веб-админка запущены и добавлены в автозагрузку!"

# ── Итоговый вывод ────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅  Установка завершена!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  🌐  Веб-админка слушает 127.0.0.1:8181 по пути /$ADMIN_SECRET_PATH/"
if [ -n "$DOMAIN" ]; then
  echo "      Снаружи: https://$DOMAIN/$ADMIN_SECRET_PATH/ — если домен отдан прокси."
fi
echo ""
echo "  ℹ️   Публикацию наружу настраивает обратный прокси, а не этот скрипт."
echo "      Порядок — в README, раздел 5в."
echo "  ℹ️   Токен бота и остальные настройки — в веб-админке."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
