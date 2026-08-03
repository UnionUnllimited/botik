#!/usr/bin/env bash
# Создаёт .env из .env.example и генерирует секреты.
# Никаких зависимостей: только coreutils, которые есть на любом сервере.
# Python не требуется — весь код проекта живёт в контейнерах.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -f .env ]; then
  echo ".env уже существует — оставляю как есть."
  exit 0
fi
if [ ! -f .env.example ]; then
  echo "Не найден .env.example" >&2
  exit 1
fi

# base64 от N случайных байт — для ключа шифрования нужны ровно 32 байта.
rand_b64() { head -c "$1" /dev/urandom | base64 | tr -d '\n'; }
# URL-safe строка без спецсимволов — для паролей и токенов.
rand_urlsafe() { head -c "$1" /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_'; }

cp .env.example .env
chmod 600 .env

fill() {
  local key="$1" value="$2"
  # Подставляем только в пустое значение: заполненное руками не трогаем.
  sed -i "s|^${key}=[[:space:]]*$|${key}=${value}|" .env
}

fill POSTGRES_PASSWORD "$(rand_urlsafe 18)"
fill SECURITY_SECRET_KEY "$(rand_urlsafe 36)"
fill SECURITY_ENCRYPTION_KEY "$(rand_b64 32)"
fill BOT_WEBHOOK_SECRET "$(rand_urlsafe 24)"

echo "Создан .env, сгенерированы: POSTGRES_PASSWORD, SECURITY_SECRET_KEY,"
echo "SECURITY_ENCRYPTION_KEY, BOT_WEBHOOK_SECRET."
echo
echo "Заполните вручную: BOT_TOKEN, APP_BOT_USERNAME, BOT_OWNER_ID,"
echo "домены и реквизиты платёжного провайдера."
