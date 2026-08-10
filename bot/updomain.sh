#!/bin/bash
set -e

# --- ПРЕДУПРЕЖДЕНИЕ ---
echo "ВАЖНО: Перед запуском этого скрипта убедитесь, что DNS A-запись"
echo "для вашего НОВОГО домена уже указывает на IP-адрес этого сервера."
echo "В противном случае Certbot не сможет выпустить сертификат."
read -p "Нажмите Enter, чтобы продолжить, или CTRL+C для отмены..."

# Проверка запуска из /root/bot/
if [[ "$PWD" != "/root/bot" ]]; then
  echo "Ошибка: Скрипт должен запускаться из /root/bot/! Текущая папка: $PWD"
  exit 1
fi

# --- Запрос нового домена ---
read -p "Введите НОВЫЙ домен (или текущий для обновления сертификата): " NEW_DOMAIN

# Проверка, что домен не пустой
if [ -z "$NEW_DOMAIN" ]; then
  echo "Домен не введён. Операция отменена."
  exit 1
fi

echo "\nНачинаю процесс для домена $NEW_DOMAIN..."

# --- Шаг 1: Проверка и открытие порта 80 ---
echo "1. Проверяю доступность порта 80 в брандмауэре (UFW)..."

# Проверяем, установлен ли UFW и активен ли он
if ! command -v ufw >/dev/null 2>&1 || ufw status | grep -q "Status: inactive"; then
  echo "   UFW неактивен или не установлен. Предполагается, что порт 80 открыт."
else
  # UFW активен, проверяем правило для порта 80
  if ufw status | grep -qw '80/tcp'; then
    echo "   Порт 80/tcp уже открыт в UFW. Все в порядке."
  else
    echo "   Порт 80/tcp закрыт. Открываю..."
    ufw allow 80/tcp
    echo "   Порт 80/tcp успешно открыт."
  fi
fi

# --- Шаг 2: Остановка Nginx ---
echo "2. Останавливаю nginx, чтобы освободить порт для Certbot..."
systemctl stop nginx

# --- Шаг 3: Получение нового SSL-сертификата ---
echo "3. Запрашиваю SSL-сертификат для $NEW_DOMAIN..."
# Используем certbot для получения нового сертификата. Добавлены флаги для автоматизации.
certbot certonly --standalone -d "$NEW_DOMAIN" --non-interactive --agree-tos -m admin@$NEW_DOMAIN --force-renewal

# --- Шаг 4: Обновление сертификатов в /root/cert/ ---
echo "4. Копирую новые сертификаты в /root/cert/..."
CERT_PATH="/etc/letsencrypt/live/$NEW_DOMAIN"

if [ -f "$CERT_PATH/fullchain.pem" ] && [ -f "$CERT_PATH/privkey.pem" ]; then
  # Копируем с заменой старых файлов
  cp "$CERT_PATH/fullchain.pem" /root/cert/fullchain.pem
  cp "$CERT_PATH/privkey.pem" /root/cert/privkey.pem
  echo "   Сертификаты успешно обновлены."
else
  echo "Ошибка: Новые сертификаты не найдены по пути $CERT_PATH."
  echo "Убедитесь, что certbot отработал без ошибок."
  echo "Запускаю nginx обратно и выхожу..."
  systemctl start nginx
  exit 1
fi

# --- Шаг 5: Обновление конфигурации Nginx ---
# Это важный шаг, который нужно сделать, чтобы nginx "знал" о новом домене.
NGINX_CONF="/etc/nginx/conf.d/xuiweb.conf"
if [ -f "$NGINX_CONF" ]; then
  echo "5. Обновляю домен в конфигурации nginx ($NGINX_CONF)..."
  # Заменяем старое значение server_name на новое
  sed -i "s/server_name .*;$/server_name $NEW_DOMAIN;/g" "$NGINX_CONF"
  echo "   Конфигурация nginx обновлена."
else
  echo "Предупреждение: Файл конфигурации $NGINX_CONF не найден. Пропускаю этот шаг."
fi

# --- Шаг 6: Перезапуск сервисов ---
echo "6. Перезапускаю nginx и связанные сервисы..."
systemctl restart nginx
# На всякий случай перезапустим и веб-сервисы, которые работают за nginx
systemctl restart xuiweb.service
systemctl restart vpn-webadmin.service

echo -e "\nГотово! Домен успешно изменен на $NEW_DOMAIN."
echo "Проверьте работу вашего сайта по новому адресу."